"""Single-threaded event-loop coordinator for the queue-based pipeline (epic #1809).

## Semantics

The coordinator runs on the process main thread and owns all eight stage
queues, the timer heap, the in-flight registry, all routing, and (through the
:class:`~hephaestus.automation.pipeline_github.PipelineGitHub` accessor) every
GitHub API mutation. A single worker pool executes agent, build/test, and
git/network jobs; the ONLY cross-thread channel is the completion queue, whose
blocking ``get(timeout=...)`` doubles as the loop's idle sleep.

Per tick (epic #1809 "Coordinator event loop"):

1. shutdown check (graceful drain, or immediate teardown after the grace
   window / a second signal);
2. wake expired timers (heapq) back into their stage queues;
3. drain ALL ready completions — interrupted results park the item
   RESUMABLE and never advance (``on_job_done`` is never called for them);
4. drain queues DOWNSTREAM-FIRST (finished → merge_wait → ... → repo; finish
   work before admitting new) with admission control;
5. fully drained: re-seed up to ``--loops`` with a zero-work convergence
   exit; otherwise block on the completion queue.

``_run_item`` drives ``on_enter``/``step`` until a ``JobRequest`` (park +
submit) or a ``StageOutcome`` (route via ROUTES). Per-item ``try/except``: a
poisoned item routes to finished(fail) and never kills the loop.

Admission control: per-repo in-flight cap (= ``max_workers``), and the
implementation queue is additionally gated by dependency topological order
(:func:`~.admission.order_for_implementation`) and file-overlap serialization
(:func:`~.admission._select_non_overlapping`). Pool size =
``parallel_repos x max_workers``.

### ``--phase-timeout`` semantic shift

Under ``--pipeline`` this flag bounds each AGENT JOB, not a whole phase
subprocess: the coordinator maps ``phase_timeout_s`` onto
``AgentJob.timeout_s`` at submit time. The legacy path binds it to the phase
subprocess lifetime.

### Journal-order invariant

GitHub is the journal: every stage performs its durable mutation immediately
BEFORE returning the outcome that causes a queue push, so restart = re-run
(seeding reconstructs the queues) and interrupts leave every item RESUMABLE
at its stage — never FAILED.

### Rate budget gate

The legacy ``_maybe_sleep_for_rate_budget`` SLEEPS its loop thread — fatal
for a single coordinator thread. Its predicate is ported to a non-blocking
check (:func:`~hephaestus.automation.pipeline_github.rate_budget_ok`); a
low-budget AGENT job is timer-parked until the upstream reset instead of
submitted. Git/build jobs are unaffected.

### Dry-run

Stage accessors log-and-skip mutators; when a stage requests a job the
coordinator logs ``[dry-run] would <descr>`` and ADVANCEs the item instead
of submitting. ``_submit`` asserts no job is EVER submitted in dry-run.

### Interrupts and exit codes

SIGINT/SIGTERM/SIGHUP share one shutdown Event: the first signal starts a
graceful drain (grace window, default 30s), the second tears the pool down
immediately and synthesizes interrupted results. Items touched by an
interrupt report ``RESUMABLE at <stage>``, never FAILED. Exit codes: 130
interrupt, 1 any fail/skip/blocked, 0 clean. The summary prints in this
module's ``finally`` — on completion AND interrupt.
"""

from __future__ import annotations

import heapq
import logging
import queue as queue_mod
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from hephaestus.automation.models import IssueInfo
from hephaestus.automation.pipeline import admission as _admission, seeding as _seeding
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue, StageQueue
from hephaestus.automation.pipeline.routing import (
    ROUTES,
    Disposition,
    Route,
    StageName,
    StageOutcome,
)
from hephaestus.automation.pipeline.stages import (
    CiStage,
    Continue,
    FinishedStage,
    ImplementationStage,
    JobRequest,
    MergeWaitStage,
    PlanningStage,
    PlanReviewStage,
    PrReviewStage,
    RepoStage,
    Stage,
    StageContext,
    StageGitHub,
)
from hephaestus.automation.pipeline.stages.repo import product_to_work_item
from hephaestus.automation.pipeline.summary import RunStats, print_summary
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem

logger = logging.getLogger(__name__)

#: Warn when any stage.step() call exceeds this duration (seconds) — the
#: stage protocol promises short (<~5s) main-thread steps.
_STEP_WATCHDOG_S = 5.0

#: Grace period for graceful shutdown (drain in-flight jobs up to this long).
_DEFAULT_GRACE_S = 30.0

#: Upper bound on Continue-transitions per _run_item call (defensive: a stage
#: that never yields a JobRequest/StageOutcome would otherwise spin forever).
_MAX_STEPS_PER_TICK = 100

#: Global safety cap on FAIL_BACK regressions per item: the sum of every
#: budget in ROUTES. Stages enforce the real per-key budgets themselves (the
#: house on_job_done pattern); this cap only guarantees cross-stage regression
#: cycles terminate even if a stage's own bookkeeping has a bug.
_FAIL_BACK_CAP = sum(sum(route.budgets.values()) for route in ROUTES.values())

#: Downstream-first drain order: finish work before admitting new (epic
#: #1809 "drain queues downstream-first (merge_wait -> ... -> repo)"; the
#: finished sink drains first of all so results are recorded promptly).
_DRAIN_ORDER: tuple[StageName, ...] = (
    StageName.FINISHED,
    StageName.MERGE_WAIT,
    StageName.CI,
    StageName.PR_REVIEW,
    StageName.IMPLEMENTATION,
    StageName.PLAN_REVIEW,
    StageName.PLANNING,
    StageName.REPO,
)


def _budget_lookup(name: str) -> int:
    """Look up a budget across all ROUTES rows (conservative default 1)."""
    for route in ROUTES.values():
        if name in route.budgets:
            return route.budgets[name]
    return 1


@dataclass(frozen=True)
class PipelineConfig:
    """Configuration for the pipeline coordinator (built by ``loop_runner``)."""

    org: str
    repos: list[str]
    issues: list[int] = field(default_factory=list)
    prs: list[int] = field(default_factory=list)
    loops: int = 1
    max_workers: int = 1
    parallel_repos: int = 1
    dry_run: bool = False
    grace_s: float = _DEFAULT_GRACE_S
    phase_timeout_s: float | None = None
    agent: str = "claude"
    model: str = ""
    planner_model: str = ""
    reviewer_model: str = ""
    implementer_model: str = ""
    no_advise: bool = False
    nitpick: bool = False
    drive_green_all: bool = False
    projects_dir: Path = field(default_factory=lambda: Path.home() / "Projects")
    json_out: bool = False


@dataclass
class _StageRunConfig:
    """PlannerOptions-like config injected as ``StageContext.config``."""

    enable_advise: bool = True
    enable_learn: bool = True
    enable_follow_up: bool = True
    run_pre_pr_tests: bool = False
    force: bool = False
    agent: str = "claude"
    model: str = ""
    planner_model: str = ""
    reviewer_model: str = ""
    implementer_model: str = ""
    dry_run: bool = False
    nitpick: bool = False
    drive_green_all: bool = False


@dataclass
class _Paths:
    """Coordinator-owned path accessor injected as ``StageContext.paths``."""

    repo_root: Path
    worktree: Path
    projects_dir: Path


class Coordinator:
    """The single-threaded pipeline event loop.

    All collaborators are injectable for tests (FakeWorkerPool /
    FakeStageGitHub / stub stages / fake clock); production wiring happens in
    :func:`run_pipeline`.
    """

    def __init__(
        self,
        config: PipelineConfig,
        *,
        github: StageGitHub,
        pool: Any | None = None,
        stages: dict[StageName, Stage] | None = None,
        github_factory: Callable[[str, Path], StageGitHub] | None = None,
        install_signals: bool = True,
    ) -> None:
        """Initialize coordinator state.

        Args:
            config: Pipeline configuration.
            github: The coordinator-owned StageGitHub accessor.
            pool: Worker pool (a real ``WorkerPool`` is built when omitted;
                tests inject ``FakeWorkerPool``).
            stages: Stage-instance map override (tests inject stubs).
            github_factory: Optional per-repo accessor factory. Production uses
                this so each repo context targets GitHub with an explicit repo.
            install_signals: Install SIGINT/SIGTERM/SIGHUP handlers in
                ``run()`` (disabled in unit tests).

        """
        self.config = config
        self.github = github
        self._github_factory = github_factory
        self.shutdown = threading.Event()
        self.completion_q: CompletionQueue = queue_mod.Queue()
        if pool is None:
            # Imported here, not module-top: WorkerPool is the pipeline's one
            # I/O-capable module and tests never need it.
            from hephaestus.automation.pipeline.worker_pool import WorkerPool

            pool = WorkerPool(
                size=max(1, config.parallel_repos * config.max_workers),
                shutdown=self.shutdown,
                completion_q=self.completion_q,
            )
        else:
            # Share channels with an injected pool when it exposes them.
            if getattr(pool, "completion_q", None) is not None:
                self.completion_q = pool.completion_q
        self.pool: Any = pool

        self.queues: dict[StageName, StageQueue] = {name: StageQueue() for name in StageName}
        self.timers: list[tuple[float, int, WorkItem]] = []
        self.in_flight: dict[JobHandle, WorkItem] = {}
        self.inflight_per_repo: Counter[str] = Counter()
        self.ledger: list[ItemResult] = []
        self.preserved: list[tuple[int, str]] = []
        self.items: list[WorkItem] = []
        self.event_log: list[tuple[Any, ...]] = []
        self.stages: dict[StageName, Stage] = stages or self._default_stages()

        self._install_signals = install_signals
        self._seq = 0
        self._grace_deadline: float | None = None
        self._immediate = False
        self._agent_job_count = 0
        self._agent_job_time_s = 0.0
        self._loops_run = 0
        self._pass_work_count = 0
        self._resumable: list[WorkItem] = []
        self._progress = False
        self._stalled_ticks = 0
        self._fatal = False
        self._seen_item_ids: set[int] = set()
        self._stage_config = _StageRunConfig(
            enable_advise=not config.no_advise,
            agent=config.agent,
            model=config.model,
            planner_model=config.planner_model,
            reviewer_model=config.reviewer_model,
            implementer_model=config.implementer_model,
            dry_run=config.dry_run,
            nitpick=config.nitpick,
            drive_green_all=config.drive_green_all,
        )
        self._ctx_cache: dict[str, StageContext] = {}

    # -- wiring ---------------------------------------------------------------

    def _default_stages(self) -> dict[StageName, Stage]:
        """Build the full production stage map."""
        return {
            StageName.REPO: RepoStage(),
            StageName.PLANNING: PlanningStage(),
            StageName.PLAN_REVIEW: PlanReviewStage(),
            StageName.IMPLEMENTATION: ImplementationStage(),
            StageName.PR_REVIEW: PrReviewStage(),
            StageName.CI: CiStage(),
            StageName.MERGE_WAIT: MergeWaitStage(),
            StageName.FINISHED: FinishedStage(self.ledger, self.preserved),
        }

    def _ctx_for(self, item: WorkItem) -> StageContext:
        """Return the (cached, per-repo) StageContext for *item*."""
        ctx = self._ctx_cache.get(item.repo)
        if ctx is None:
            root = Path(self.config.projects_dir) / item.repo
            ctx = StageContext(
                config=self._stage_config,
                org=self.config.org,
                dry_run=self.config.dry_run,
                github=(
                    self._github_factory(item.repo, root)
                    if self._github_factory is not None
                    else self.github
                ),
                paths=_Paths(
                    repo_root=root,
                    worktree=root,
                    projects_dir=Path(self.config.projects_dir),
                ),
                budget_fn=_budget_lookup,
            )
            self._ctx_cache[item.repo] = ctx
        return ctx

    # -- run loop ---------------------------------------------------------------

    def run(self) -> int:
        """Run the pipeline to quiescence (or interrupt) and return the exit code."""
        started = time.monotonic()
        if self._install_signals:
            self._install_signal_handlers()
        try:
            self._loops_run = 1
            self._seed_pass()
            while True:
                if self._immediate or self._grace_exceeded():
                    self._teardown_immediate()
                    break
                self._wake_timers()
                self._drain_completions()
                if self.shutdown.is_set():
                    # Graceful: stop admitting; drain in-flight to RESUMABLE.
                    if not self.in_flight:
                        break
                    self._wait_for_completion(timeout=0.2)
                    continue
                self._drain_queues()
                if self._all_idle():
                    if not self._reseed_if_converged():
                        break
                    continue
                self._idle_wait()
        except Exception:
            logger.exception("pipeline run failed")
            self._fatal = True
        finally:
            self._finalize_resumable()
            exit_code = self._exit_code()
            stats = RunStats(
                exit_code=exit_code,
                interrupted=self.shutdown.is_set(),
                loops_run=self._loops_run,
                agent_job_count=self._agent_job_count,
                agent_job_time_s=self._agent_job_time_s,
                wall_s=time.monotonic() - started,
            )
            print_summary(self.items, stats, self.preserved, json_out=self.config.json_out)
        return exit_code

    def _exit_code(self) -> int:
        """130 on interrupt; 1 on any fail/skip/blocked (or fatal error); 0 clean."""
        if self.shutdown.is_set():
            return 130
        if self._fatal or any(not result.passed for result in self.ledger):
            return 1
        return 0

    def _all_idle(self) -> bool:
        """Return True when queues, timer heap, and in-flight map are all empty."""
        return (
            all(len(q) == 0 for q in self.queues.values())
            and not self.timers
            and not self.in_flight
        )

    def _grace_exceeded(self) -> bool:
        """Return True when a graceful shutdown has outlived its grace window."""
        return (
            self.shutdown.is_set()
            and self._grace_deadline is not None
            and time.monotonic() >= self._grace_deadline
        )

    def _idle_wait(self) -> None:
        """Block on the completion queue (the loop's only sleep).

        Also breaks a theoretical no-progress stall: if a full tick made no
        progress with nothing in flight and no timers pending, force-run the
        most-downstream queued item ignoring admission (liveness guarantee —
        admission can only defer while something else is running or parked).
        """
        if self._progress:
            self._progress = False
            self._stalled_ticks = 0
        elif not self.in_flight and not self.timers:
            self._stalled_ticks += 1
            if self._stalled_ticks >= 3:
                self._force_run_one()
                return
        timeout = 1.0
        if self.timers:
            timeout = min(timeout, max(0.01, self.timers[0][0] - time.monotonic()))
        self._wait_for_completion(timeout=timeout)

    def _force_run_one(self) -> None:
        """Run the first item of the most-downstream non-empty queue."""
        self._stalled_ticks = 0
        for stage_name in _DRAIN_ORDER:
            q = self.queues[stage_name]
            if len(q):
                item = q.pop()
                logger.error(
                    "pipeline stalled with no in-flight work; force-running %s item %s",
                    stage_name.value,
                    self._item_key(item),
                )
                self._run_item(item)
                return

    # -- timers -------------------------------------------------------------

    def _timer_park(self, item: WorkItem, delay_s: float) -> None:
        """Park *item* on the timer heap for ``delay_s`` seconds."""
        wake = time.monotonic() + max(0.0, delay_s)
        heapq.heappush(self.timers, (wake, self._seq, item))
        self._seq += 1
        item.add_history_event(item.stage, item.state, note=f"timer-parked {delay_s:.1f}s")
        self.event_log.append(("timer_park", item.stage.value, self._item_key(item), delay_s))

    def _wake_timers(self) -> None:
        """Move every expired timer entry back into its stage queue."""
        now = time.monotonic()
        while self.timers and self.timers[0][0] <= now:
            _, _, item = heapq.heappop(self.timers)
            self._progress = True
            self._push_item(item, item.stage, enter=False)

    # -- completions ----------------------------------------------------------

    def _drain_completions(self) -> None:
        """Drain ALL ready completions without blocking."""
        while True:
            try:
                handle, result = self.completion_q.get_nowait()
            except queue_mod.Empty:
                return
            self._handle_completion(handle, result)

    def _wait_for_completion(self, timeout: float) -> None:
        """Block up to *timeout* for one completion; handle it if one arrives."""
        try:
            handle, result = self.completion_q.get(timeout=timeout)
        except queue_mod.Empty:
            return
        self._handle_completion(handle, result)

    def _handle_completion(self, handle: JobHandle, result: JobResult) -> None:
        """Route one completed job back to its item.

        Interrupted results park the item RESUMABLE — they never advance and
        never reach ``on_job_done`` (base-protocol contract).
        """
        self._progress = True
        item = self.in_flight.pop(handle, None)
        if item is None:
            logger.warning("completion for unknown handle (already torn down?): %s", handle)
            return
        self.inflight_per_repo[item.repo] -= 1
        if self.inflight_per_repo[item.repo] <= 0:
            del self.inflight_per_repo[item.repo]
        if isinstance(handle.job, AgentJob):
            self._agent_job_count += 1
            self._agent_job_time_s += result.duration_s

        if result.interrupted:
            self._park_resumable(item)
            return

        stage = self.stages[item.stage]
        ctx = self._ctx_for(item)
        try:
            stage.on_job_done(item, result, ctx)
        except Exception:
            logger.exception(
                "on_job_done poisoned item %s at %s", self._item_key(item), item.stage.value
            )
            self._finish(item, passed=False, reason="poisoned: on_job_done raised")
            return
        # JobHandle.on_done_state is annotated StageName upstream but carries
        # the stage-local state string the JobRequest specified (see
        # JobRequest.on_done_state: str) — runtime values are plain strings.
        item.state = cast(str, handle.on_done_state)
        if self.shutdown.is_set():
            # Graceful shutdown: the durable write for this completion is
            # already journaled by on_job_done's owning stage; do not step
            # further (stepping could submit new work). Park RESUMABLE.
            self._park_resumable(item)
            return
        self._run_item(item)

    def _park_resumable(self, item: WorkItem) -> None:
        """Park *item* as RESUMABLE at its current stage (interrupt semantics).

        Never FAILED: durable writes precede queue pushes, so a restart's
        seeding reconstruction resumes exactly here with no shutdown
        bookkeeping.
        """
        item.result = ItemResult(
            passed=False,
            reason=f"resumable at {item.stage.value}",
            final_stage=item.stage,
        )
        item.add_history_event(item.stage, item.state, note="interrupted; resumable")
        self._resumable.append(item)
        logger.info(
            "interrupt: item %s RESUMABLE at %s (never failed)",
            self._item_key(item),
            item.stage.value,
        )

    # -- queue draining and admission ---------------------------------------

    def _drain_queues(self) -> None:
        """Drain queues downstream-first with admission control."""
        for stage_name in _DRAIN_ORDER:
            if self.shutdown.is_set():
                return
            if stage_name is StageName.IMPLEMENTATION:
                self._drain_implementation()
                continue
            q = self.queues[stage_name]
            for _ in range(len(q)):
                if self.shutdown.is_set():
                    return
                item = q.pop()
                if not self._admit(item):
                    q.push(item)
                    continue
                self.event_log.append(("drain", stage_name.value, self._item_key(item)))
                self._run_item(item)

    def _drain_implementation(self) -> None:
        """Drain the implementation queue under topo order + file-overlap gating.

        REUSES :func:`admission.order_for_implementation` (dependencies
        first) and :func:`admission._select_non_overlapping` (defer plans
        touching the same files while a peer is dispatched, #1623 — only
        engaged when real parallelism is possible, mirroring the legacy
        ``serialize_file_overlap`` gate).
        """
        q = self.queues[StageName.IMPLEMENTATION]
        if not len(q):
            return
        items = [q.pop() for _ in range(len(q))]
        issue_items = {it.issue: it for it in items if it.issue is not None}
        infos = [
            IssueInfo(
                number=it.issue,
                title=str(it.payload.get("issue_title", "")),
                dependencies=list(it.payload.get("dependencies", [])),
            )
            for it in items
            if it.issue is not None
        ]
        ordered = _admission.order_for_implementation(infos)
        dispatch = ordered
        if self.config.max_workers > 1 and len(ordered) > 1:
            dispatch, deferred = _admission._select_non_overlapping(ordered)
            for number in deferred:
                logger.info("implementation #%s deferred (file overlap)", number)
        ran: set[int] = set()
        for number in dispatch:
            item = issue_items[number]
            if self.shutdown.is_set() or not self._admit(item):
                continue  # stays queued (re-pushed below)
            ran.add(id(item))
            self.event_log.append(("drain", StageName.IMPLEMENTATION.value, self._item_key(item)))
            self._run_item(item)
        # Preserve original queue order for deferred / non-admitted / non-issue items.
        for it in items:
            if id(it) not in ran:
                q.push(it)

    def _admit(self, item: WorkItem) -> bool:
        """Admission control: per-repo in-flight cap (O(1) Counter lookup)."""
        return self.inflight_per_repo[item.repo] < max(1, self.config.max_workers)

    # -- item execution -----------------------------------------------------

    def _run_item(self, item: WorkItem) -> None:
        """Drive one item: on_enter, then step until JobRequest or outcome.

        Per-item try/except — a poisoned item routes to finished(fail) and
        never kills the loop.
        """
        self._progress = True
        stage = self.stages[item.stage]
        ctx = self._ctx_for(item)
        try:
            if item.payload.pop("_enter_pending", False):
                outcome = stage.on_enter(item, ctx)
                if outcome is not None:
                    self._route(item, outcome)
                    return
            for _ in range(_MAX_STEPS_PER_TICK):
                result = self._step_with_watchdog(stage, item, ctx)
                if isinstance(result, Continue):
                    item.state = result.next_state
                    item.add_history_event(item.stage, item.state)
                    continue
                if isinstance(result, JobRequest):
                    if self.config.dry_run:
                        descr = getattr(result.job, "descr", "") or type(result.job).__name__
                        logger.info(
                            "[dry-run] would submit %s: %s", type(result.job).__name__, descr
                        )
                        self._route(
                            item, StageOutcome(Disposition.ADVANCE, f"[dry-run] would {descr}")
                        )
                        return
                    self._submit(item, result)
                    return
                self._route(item, result)
                return
            raise RuntimeError(
                f"stage {item.stage.value} exceeded {_MAX_STEPS_PER_TICK} steps in one tick"
            )
        except Exception as exc:
            logger.exception(
                "pipeline item %s poisoned at %s; routing to finished(fail)",
                self._item_key(item),
                item.stage.value,
            )
            self._finish(item, passed=False, reason=f"poisoned: {exc}")

    def _step_with_watchdog(self, stage: Stage, item: WorkItem, ctx: StageContext) -> Any:
        """Run one stage.step, warning when it breaches the <~5s contract."""
        t0 = time.monotonic()
        result = stage.step(item, ctx)
        elapsed = time.monotonic() - t0
        if elapsed > _STEP_WATCHDOG_S:
            logger.warning(
                "stage.step stalled: %s %s took %.1fs (contract: <%.0fs)",
                item.stage.value,
                self._item_key(item),
                elapsed,
                _STEP_WATCHDOG_S,
            )
        return result

    def _submit(self, item: WorkItem, request: JobRequest) -> None:
        """Submit the requested job; register in-flight bookkeeping.

        Rate gate (non-blocking): an AgentJob submitted while the GraphQL
        budget is low is timer-parked until the upstream reset instead
        (``will_submit_agent`` does not exist on the merged stage protocol,
        so the gate lives here at the submit chokepoint — the plan's
        sanctioned fallback).
        """
        assert not self.config.dry_run, "dry-run must never submit jobs"  # noqa: S101
        job = request.job
        if isinstance(job, AgentJob):
            ok, delay = self._rate_budget_ok()
            if not ok:
                logger.info(
                    "rate budget low; timer-parking %s for %.0fs (no sleep)",
                    self._item_key(item),
                    delay,
                )
                self._timer_park(item, delay)
                return
            if self.config.phase_timeout_s and self.config.phase_timeout_s > 0:
                # --phase-timeout semantic shift (M4): under --pipeline it
                # bounds each AGENT JOB, not a phase subprocess.
                job = replace(job, timeout_s=int(self.config.phase_timeout_s))
        handle = self.pool.submit(job, cast(StageName, request.on_done_state))
        self.in_flight[handle] = item
        self.inflight_per_repo[item.repo] += 1
        self.event_log.append(
            ("submit", type(job).__name__, self._item_key(item), request.on_done_state)
        )

    def _rate_budget_ok(self) -> tuple[bool, float]:
        """Non-blocking rate-budget check (``(ok, park_delay_s)``)."""
        # Imported lazily: pipeline_github pulls the full gh helper surface,
        # which unit tests patch at this seam.
        from hephaestus.automation.pipeline_github import rate_budget_ok

        return rate_budget_ok()

    # -- routing --------------------------------------------------------------

    def _route(self, item: WorkItem, outcome: StageOutcome) -> None:
        """Apply the Disposition -> action table (plan #1817)."""
        route = ROUTES[item.stage]
        disposition = outcome.disposition

        if item.stage is StageName.FINISHED:
            # Sink outcomes are terminal: the result is already recorded.
            self.event_log.append(("done", self._item_key(item), outcome.note))
            return

        if disposition is Disposition.ADVANCE:
            self._seed_products(item)
            target = route.next
            if target is StageName.FINISHED:
                self._finish(item, passed=True, reason=outcome.note or "advance")
            else:
                self._push_item(item, target, enter=True)
            return

        if disposition is Disposition.RETRY:
            self._route_retry(item, outcome)
            return

        if disposition is Disposition.FAIL_BACK:
            self._route_fail_back(item, outcome, route)
            return

        if disposition is Disposition.SKIP:
            self._finish(item, passed=False, reason=f"skip: {outcome.note}")
            return
        if disposition is Disposition.BLOCKED:
            self._finish(item, passed=False, reason=f"blocked: {outcome.note}")
            return
        if disposition is Disposition.FINISH_PASS:
            self._seed_products(item)
            self._finish(item, passed=True, reason=outcome.note or "pass")
            return
        # FINISH_FAIL (exhaustive over Disposition)
        self._finish(item, passed=False, reason=outcome.note or "fail")

    def _route_retry(self, item: WorkItem, outcome: StageOutcome) -> None:
        """Apply the RETRY row: heap-park on a recorded delay, else next tick.

        RETRY timer contract (base.py): the stage records its backoff in
        ``payload["retry_delay_s"]`` immediately before returning; a missing
        key means "retry on the next drain tick". Under dry-run a DELAYED
        retry waits on real-world progress (CI runs, PR merges) the preview
        will never make, so the item finishes instead of stalling the heap.
        """
        delay = item.payload.pop("retry_delay_s", None)
        if delay is None:
            self._push_item(item, item.stage, enter=False)
        elif self.config.dry_run:
            self._finish(
                item, passed=False, reason=f"[dry-run] would wait {delay}s: {outcome.note}"
            )
        else:
            self._timer_park(item, float(delay))

    def _route_fail_back(self, item: WorkItem, outcome: StageOutcome, route: Route) -> None:
        """Apply the FAIL_BACK row: reason-keyed regression, globally capped.

        Dry-run mutators never write the gate labels the earlier stage would
        re-check, so a dry-run regression would ping-pong until the safety
        cap while burning live reads — the item finishes with the
        would-regress note instead (its entry classification is the dry-run
        deliverable).
        """
        if self.config.dry_run:
            self._finish(item, passed=False, reason=f"[dry-run] would fail_back: {outcome.note}")
            return
        fail_backs = int(item.payload.get("_fail_backs", 0)) + 1
        item.payload["_fail_backs"] = fail_backs
        if fail_backs > _FAIL_BACK_CAP:
            self._finish(
                item,
                passed=False,
                reason=f"fail_back safety cap ({_FAIL_BACK_CAP}) exceeded: {outcome.note}",
            )
            return
        target = route.fail_routes.get(outcome.note, route.fail_routes.get("*", StageName.FINISHED))
        if target is StageName.FINISHED:
            self._finish(item, passed=False, reason=outcome.note or "fail_back")
        else:
            self._push_item(item, target, enter=True)

    def _finish(self, item: WorkItem, *, passed: bool, reason: str) -> None:
        """Set the item's result and hand it to the finished sink."""
        item.result = ItemResult(passed=passed, reason=reason, final_stage=item.stage)
        if item.stage is StageName.FINISHED:
            # Poisoned inside the sink: record directly, never re-queue.
            if not item.payload.get("_recorded", False):
                self.ledger.append(item.result)
                item.payload["_recorded"] = True
            return
        self._push_item(item, StageName.FINISHED, enter=True)

    def _seed_products(self, item: WorkItem) -> None:
        """Push a terminal repo item's discovered products into entry queues."""
        if item.kind is not ItemKind.REPO:
            return
        for product in item.payload.pop("products", []):
            if product.get("stage") is None:
                logger.info("[%s] excluded: %s", item.repo, product.get("reason", ""))
                continue
            new_item = product_to_work_item(item.repo, product)
            if new_item is None:  # pragma: no cover - guarded by stage check above
                continue
            if new_item.stage is StageName.FINISHED:
                new_item.result = ItemResult(
                    passed=True,
                    reason=product.get("reason", "already finished"),
                    final_stage=StageName.FINISHED,
                )
            elif new_item.stage is not StageName.REPO:
                self._pass_work_count += 1
            self._push_item(new_item, new_item.stage, enter=True)

    def _push_item(self, item: WorkItem, stage: StageName, enter: bool) -> None:
        """Push *item* into *stage*'s queue (the single push chokepoint).

        Every durable GitHub mutation for this transition already happened
        inside the stage, immediately before the outcome that got us here.
        """
        item.stage = stage
        if enter:
            item.state = "ENTER"
            item.payload["_enter_pending"] = True
            item.add_history_event(stage, item.state, note="enqueued")
        if id(item) not in self._seen_item_ids:
            self._seen_item_ids.add(id(item))
            self.items.append(item)
            item.payload.setdefault("entry_stage", stage.value)
        self.queues[stage].push(item)
        self.event_log.append(("push", stage.value, self._item_key(item)))

    @staticmethod
    def _item_key(item: WorkItem) -> str:
        """Human-readable item identity for logs and the event log."""
        if item.kind is ItemKind.REPO:
            return item.repo
        if item.kind is ItemKind.PR:
            return f"{item.repo}!{item.pr}"
        return f"{item.repo}#{item.issue}"

    # -- seeding / convergence ------------------------------------------------

    def _seed_pass(self) -> int:
        """Seed one pass from CLI scope (repos / --issues / --prs).

        Returns:
            The number of items pushed (repo seeds included).

        """
        self._pass_work_count = 0
        entries = _seeding.seed_from_cli(self.config.repos, self.config.issues, self.config.prs)
        pushed = 0
        for entry in entries:
            if entry.stage is None:
                # Epic tagging is the ONE sanctioned seeding write, executed
                # here through the skip_epics chokepoint BEFORE the exclusion
                # is honored (seeding.py write-path boundary).
                if entry.reason.startswith(_seeding.EPIC_NEEDS_SKIP_TAG):
                    self.github.skip_epics({int(entry.identifier): []})
                logger.info("seed excluded: %s", entry.reason)
                continue
            item = self._entry_to_item(entry, self.config.repos[0] if self.config.repos else "")
            if item.stage not in (StageName.REPO, StageName.FINISHED):
                self._pass_work_count += 1
            if item.stage is StageName.FINISHED and item.result is None:
                item.result = ItemResult(
                    passed=True, reason=entry.reason, final_stage=StageName.FINISHED
                )
            self._push_item(item, item.stage, enter=True)
            pushed += 1
        return pushed

    @staticmethod
    def _entry_to_item(entry: _seeding.SeedEntry, default_repo: str) -> WorkItem:
        """Turn one :class:`~.seeding.SeedEntry` into a queue-ready WorkItem.

        Args:
            entry: The seed entry (never an exclusion — caller filters).
            default_repo: Repo context for ``--issues`` / ``--prs`` entries
                (the legacy loop scopes an explicit issue list to the first
                resolved repo the same way).

        """
        assert entry.stage is not None  # noqa: S101  # caller filters exclusions
        if entry.kind == "repo":
            item = WorkItem(repo=str(entry.identifier), kind=ItemKind.REPO, stage=entry.stage)
        elif entry.kind == "pr":
            item = WorkItem(
                repo=default_repo, kind=ItemKind.PR, pr=int(entry.identifier), stage=entry.stage
            )
        else:
            item = WorkItem(
                repo=default_repo,
                kind=ItemKind.ISSUE,
                issue=int(entry.identifier),
                stage=entry.stage,
            )
        item.state = "ENTER"
        item.payload["entry_reason"] = entry.reason
        return item

    def _reseed_if_converged(self) -> bool:
        """Re-seed after full drain; False = stop (loops or zero-work exit).

        Mirrors the legacy zero-work early-exit (loop_runner
        ``_CONVERGENCE_PHASES``): when the just-finished pass produced zero
        actionable (non-repo, non-finished) work, the run converged — exit
        even if ``--loops`` remain.
        """
        if self._loops_run >= self.config.loops:
            logger.info("loop budget exhausted (%d/%d)", self._loops_run, self.config.loops)
            return False
        if self._pass_work_count == 0:
            logger.info(
                "zero-work convergence: pass %d produced no actionable items; exiting early",
                self._loops_run,
            )
            return False
        self._loops_run += 1
        logger.info("re-seeding: loop %d/%d", self._loops_run, self.config.loops)
        return self._seed_pass() > 0

    # -- shutdown ---------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """SIGINT/SIGTERM/SIGHUP -> one shutdown Event (first graceful, second immediate)."""

        def _handler(signum: int, frame: object) -> None:
            if self.shutdown.is_set():
                logger.warning("second signal %d: immediate shutdown", signum)
                self._immediate = True
            else:
                logger.warning(
                    "signal %d: graceful shutdown (grace %.0fs; press again to force)",
                    signum,
                    self.config.grace_s,
                )
                self.shutdown.set()
                self._grace_deadline = time.monotonic() + self.config.grace_s

        sigs = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):  # pragma: no branch - always true on POSIX
            sigs.append(signal.SIGHUP)
        for sig in sigs:
            try:
                signal.signal(sig, _handler)
            except ValueError:  # pragma: no cover - not on the main thread
                logger.debug("cannot install handler for %s off the main thread", sig)

    def _teardown_immediate(self) -> None:
        """Cancel the pool and synthesize interrupted results for in-flight items."""
        self.shutdown.set()
        try:
            self.pool.shutdown()
        except Exception:  # pragma: no cover - defensive
            logger.exception("pool shutdown raised")
        for item in list(self.in_flight.values()):
            self._park_resumable(item)
        self.in_flight.clear()
        self.inflight_per_repo.clear()

    def _finalize_resumable(self) -> None:
        """Mark every still-live item RESUMABLE at its stage (never FAILED)."""
        if not self.shutdown.is_set():
            return
        leftovers: list[WorkItem] = [item for _, _, item in self.timers]
        for stage_name, q in self.queues.items():
            if stage_name is StageName.FINISHED:
                continue
            leftovers.extend(q.snapshot())
        leftovers.extend(self.in_flight.values())
        for item in leftovers:
            if item.result is None:
                self._park_resumable(item)


def run_pipeline(config: PipelineConfig) -> int:
    """Run the queue-based pipeline to completion.

    Public entry point called from ``loop_runner.main()`` when ``--pipeline``
    (or ``HEPH_PIPELINE=1``) is enabled.

    Args:
        config: Pipeline configuration.

    Returns:
        Exit code: 130 interrupt, 1 any fail/skip/blocked, 0 clean.

    """
    # Imported here: pipeline_github maps the accessor onto the real gh
    # helpers and must stay out of the pure pipeline import surface.
    from hephaestus.automation.pipeline_github import PipelineGitHub

    def _github_for(repo_name: str, repo_root: Path) -> PipelineGitHub:
        return PipelineGitHub(
            config.org,
            repo=repo_name,
            dry_run=config.dry_run,
            repo_root=repo_root,
        )

    repo = config.repos[0] if config.repos else ""
    repo_root = Path(config.projects_dir) / repo if repo else Path(config.projects_dir)
    github = (
        _github_for(repo, repo_root) if repo else PipelineGitHub(config.org, dry_run=config.dry_run)
    )
    coordinator = Coordinator(config, github=github, github_factory=_github_for)
    return coordinator.run()
