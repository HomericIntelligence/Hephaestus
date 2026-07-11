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

### ``--phase-timeout`` queue semantics

This flag bounds each AGENT JOB, not a whole phase subprocess: the coordinator
maps ``phase_timeout_s`` onto ``AgentJob.timeout_s`` at submit time.

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

When an interrupt overlaps a non-passing ledger entry or fatal coordinator
error, 130 deliberately takes priority because the run did not complete.
"""

from __future__ import annotations

import heapq
import json
import logging
import queue as queue_mod
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeAlias

from hephaestus.automation.agent_config import DEFAULT_CI_POLL_MAX_WAIT
from hephaestus.automation.models import IssueInfo
from hephaestus.automation.pipeline import admission as _admission, seeding as _seeding
from hephaestus.automation.pipeline.events import StageEvent, encode_stage_event
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue, StageQueue
from hephaestus.automation.pipeline.routing import (
    PIPELINE_ORDER,
    ROUTES,
    Disposition,
    PipelineScope,
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
from hephaestus.automation.pipeline.stages.implementation import PRE_PR_TEST_ARGV
from hephaestus.automation.pipeline.stages.repo import product_to_work_item
from hephaestus.automation.pipeline.summary import RunStats, latest_logical_items, print_summary
from hephaestus.automation.pipeline.work_item import (
    ItemKind,
    ItemResult,
    PreservedWorktree,
    WorkItem,
)
from hephaestus.automation.state_labels import STATE_IMPLEMENTATION_GO

logger = logging.getLogger(__name__)

#: Warn when any stage.step() call exceeds this duration (seconds) — the
#: stage protocol promises short (<~5s) main-thread steps.
_STEP_WATCHDOG_S = 5.0

#: Grace period for graceful shutdown (drain in-flight jobs up to this long).
_DEFAULT_GRACE_S = 30.0

#: Coordinator idle poll interval while waiting for completions (seconds).
_IDLE_POLL_S = 1.0

#: Number of fully stalled idle ticks before the coordinator force-runs work.
_STALL_TICKS_BEFORE_FORCE = 3

#: Upper bound on Continue-transitions per _run_item call (defensive: a stage
#: that never yields a JobRequest/StageOutcome would otherwise spin forever).
_MAX_STEPS_PER_TICK = 100

#: Global safety cap on FAIL_BACK regressions per item: the sum of every
#: budget in ROUTES. Stages enforce the real per-key budgets themselves (the
#: house on_job_done pattern); this cap only guarantees cross-stage regression
#: cycles terminate even if a stage's own bookkeeping has a bug.
_FAIL_BACK_CAP = sum(sum(route.budgets.values()) for route in ROUTES.values())

_WAKE_HANDLE = object()

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

StageStepResult: TypeAlias = Continue | JobRequest | StageOutcome


def _budget_lookup(name: str) -> int:
    """Look up a budget across all ROUTES rows (conservative default 1)."""
    for route in ROUTES.values():
        if name in route.budgets:
            return route.budgets[name]
    return 1


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable representation for event-log fields."""
    if isinstance(value, StageName):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


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
    include_bot_prs: bool = True
    include_all_authors: bool = False
    # When False, the CI stage's pre-fix mechanical rebase is skipped and every
    # behind/conflicting PR falls through to the fix agent (``--no-mechanical-rebase``).
    # Read by ``stages/ci.py`` via ``ctx.config.enable_mechanical_rebase``.
    enable_mechanical_rebase: bool = True
    # Wall-clock seconds the CI stage may wait for pending checks before timing
    # out. Mirrors ``CIDriverOptions.poll_max_wait`` / ``--poll-max-wait``.
    poll_max_wait: int = DEFAULT_CI_POLL_MAX_WAIT
    # Per-budget overrides applied on top of the ROUTES defaults by the
    # coordinator's budget accessor. ``--max-fix-iterations N`` maps to
    # ``{"ci_fix": N}`` so the CI-fix attempt budget is caller-tunable.
    budget_overrides: dict[str, int] = field(default_factory=dict)
    # Configurable argv for the optional pre-PR unit-test gate. The
    # implementation stage reads this vector instead of hardcoding the test
    # command so non-pixi repos and non-standard unit-test layouts can opt in.
    pre_pr_test_argv: tuple[str, ...] = PRE_PR_TEST_ARGV
    run_pre_pr_tests: bool = False
    serialize_file_overlap: bool = True
    event_log_path: Path | None = None
    projects_dir: Path = field(default_factory=lambda: Path.home() / "Projects")
    json_out: bool = False
    # Optional contiguous stage subset. When set, the coordinator routes items
    # through ``scope.trimmed_routes()`` instead of the full ``ROUTES`` table,
    # so a caller (e.g. ``hephaestus-plan-issues``) can run a partial pipeline
    # (planning -> plan_review) with every out-of-scope target rewritten to
    # FINISHED. ``None`` runs the full pipeline.
    scope: PipelineScope | None = None
    # Re-seed override for scoped re-runs (``--force`` on the planner CLI):
    # when True, issues already at-or-past ``state:plan-go`` are re-routed to
    # PLANNING instead of being classified past the scope (and thus skipped).
    force: bool = False


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
    include_bot_prs: bool = True
    include_all_authors: bool = False
    enable_mechanical_rebase: bool = True
    poll_max_wait: int = DEFAULT_CI_POLL_MAX_WAIT
    pre_pr_test_argv: tuple[str, ...] = PRE_PR_TEST_ARGV


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
        self.preserved: list[PreservedWorktree] = []
        self.items: list[WorkItem] = []
        self.event_log: list[tuple[Any, ...]] = []
        self._event_log_disabled = False
        self.stages: dict[StageName, Stage] = stages or self._default_stages()
        # Route table for this run: the full ROUTES, or a scope-trimmed copy
        # (out-of-scope next/fail targets rewritten to FINISHED) when the
        # config pins a contiguous stage subset. Computed once — trimming is
        # pure and the scope is immutable for the run's lifetime. FINISHED is
        # the universal sink: ``trimmed_routes`` omits it unless it is in the
        # scope set, so its terminal route is re-added here — every item
        # eventually routes into FINISHED and _route must find it.
        if config.scope is not None:
            self._routes = config.scope.trimmed_routes()
            self._routes.setdefault(StageName.FINISHED, ROUTES[StageName.FINISHED])
        else:
            self._routes = ROUTES

        self._install_signals = install_signals
        self._seq = 0
        self._grace_deadline: float | None = None
        self._immediate = False
        self._agent_job_count = 0
        self._agent_job_time_s = 0.0
        self._loops_run = 0
        self._pass_work_count = 0
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
            include_bot_prs=config.include_bot_prs,
            include_all_authors=config.include_all_authors,
            enable_mechanical_rebase=config.enable_mechanical_rebase,
            poll_max_wait=config.poll_max_wait,
            pre_pr_test_argv=config.pre_pr_test_argv,
            run_pre_pr_tests=config.run_pre_pr_tests,
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

    def _ctx_for_repo(self, repo: str) -> StageContext:
        """Return the (cached, per-repo) StageContext for *repo*."""
        ctx = self._ctx_cache.get(repo)
        if ctx is None:
            root = Path(self.config.projects_dir) / repo
            ctx = StageContext(
                config=self._stage_config,
                org=self.config.org,
                dry_run=self.config.dry_run,
                github=(
                    self._github_factory(repo, root)
                    if self._github_factory is not None
                    else self.github
                ),
                paths=_Paths(
                    repo_root=root,
                    worktree=root,
                    projects_dir=Path(self.config.projects_dir),
                ),
                budget_fn=self._budget_for,
                event_fn=self._record_stage_event,
            )
            self._ctx_cache[repo] = ctx
        return ctx

    def _ctx_for(self, item: WorkItem) -> StageContext:
        """Return the (cached, per-repo) StageContext for *item*."""
        return self._ctx_for_repo(item.repo)

    def _budget_for(self, name: str) -> int:
        """Config-aware budget accessor injected as ``StageContext.budget_fn``.

        A ``config.budget_overrides`` entry (e.g. ``--max-fix-iterations N`` ->
        ``{"ci_fix": N}``) takes precedence over the ROUTES default, so a caller
        can tune a stage's per-item budget without editing the routing table.
        """
        override = self.config.budget_overrides.get(name)
        if override is not None:
            return override
        return _budget_lookup(name)

    def _record_stage_event(self, event: StageEvent) -> None:
        """Validate and persist a closed-schema event emitted by a stage."""
        event_name, fields = encode_stage_event(event)
        self._record_event(event_name, fields)

    def _record_event(self, event: str, *fields: Any) -> None:
        """Append an event to memory and, when configured, to JSONL on disk."""
        self.event_log.append((event, *fields))
        if self._event_log_disabled:
            return
        path = self.config.event_log_path
        if path is None:
            return
        record = {
            "ts": time.time(),
            "event": event,
            "fields": [_json_safe(field) for field in fields],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning("failed to write pipeline event log %s: %s", path, exc)
            self._event_log_disabled = True

    def _wake_completion_wait(self) -> None:
        """Wake the coordinator if it is blocked in completion_q.get()."""
        self.completion_q.put((_WAKE_HANDLE, JobResult(ok=False, interrupted=True, error="wake")))

    # -- run loop ---------------------------------------------------------------

    def run(self) -> int:
        """Run the pipeline to quiescence (or interrupt) and return the exit code."""
        started = time.monotonic()
        if self._install_signals:
            self._install_signal_handlers()
        try:
            self._record_event(
                "run_start",
                {
                    "org": self.config.org,
                    "repos": self.config.repos,
                    "issues": self.config.issues,
                    "prs": self.config.prs,
                    "loops": self.config.loops,
                    "max_workers": self.config.max_workers,
                },
            )
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
                loops_run=self._loops_run,
                agent_job_count=self._agent_job_count,
                agent_job_time_s=self._agent_job_time_s,
                wall_s=time.monotonic() - started,
            )
            summary_items = self._effective_items()
            preserved = self._active_preserved_worktrees()
            self._record_event(
                "run_end",
                {
                    "exit_code": exit_code,
                    "interrupted": stats.interrupted,
                    "items": len(summary_items),
                    "agent_jobs": self._agent_job_count,
                    "wall_s": stats.wall_s,
                },
            )
            print_summary(summary_items, stats, preserved, json_out=self.config.json_out)
        return exit_code

    def _effective_items(self) -> list[WorkItem]:
        """Return latest logical items, collapsing superseded re-seed attempts."""
        return latest_logical_items(self.items)

    def _active_preserved_worktrees(self) -> list[PreservedWorktree]:
        """Return preserved worktrees for latest failed items that still exist."""
        failed_items = {
            (item.repo, item.issue or item.pr or 0)
            for item in self._effective_items()
            if item.result is not None and not item.result.passed
        }
        active: list[PreservedWorktree] = []
        seen: set[PreservedWorktree] = set()
        for repo, issue_or_pr, path in self.preserved:
            entry = (repo, issue_or_pr, path)
            if entry in seen or (repo, issue_or_pr) not in failed_items or not Path(path).exists():
                continue
            seen.add(entry)
            active.append(entry)
        return active

    def _exit_code(self) -> int:
        """130 on interrupt; 1 on any effective fail/skip/blocked; 0 clean."""
        if self.shutdown.is_set():
            # Interrupt deliberately takes priority over non-passing ledger
            # entries and fatal coordinator errors: a signal means the run did
            # not complete, so wrappers must classify it as cancellation even
            # if earlier work had already failed.
            return 130
        effective_results = [item.result for item in self._effective_items() if item.result]
        results = effective_results or self.ledger
        if self._fatal or any(not result.passed for result in results):
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
            if self._stalled_ticks >= _STALL_TICKS_BEFORE_FORCE:
                self._force_run_one()
                return
        timeout = _IDLE_POLL_S
        if self.timers:
            timeout = min(timeout, max(0.01, self.timers[0][0] - time.monotonic()))
        self._wait_for_completion(timeout=timeout)

    def _force_run_one(self) -> None:
        """Run the first item of the most-downstream non-empty queue."""
        assert not self.in_flight, "force-run requires no in-flight work"  # noqa: S101
        self._stalled_ticks = 0
        for stage_name in _DRAIN_ORDER:
            q = self.queues[stage_name]
            if len(q):
                item = q.pop()
                logger.error(
                    "pipeline stalled with no in-flight work; "
                    "force-running %s item %s; inflight_per_repo=%s",
                    stage_name.value,
                    self._item_key(item),
                    dict(self.inflight_per_repo),
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
        self._record_event("timer_park", item.stage.value, self._item_key(item), delay_s)

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
            if handle is _WAKE_HANDLE:
                self._record_event("wake", "completion_q")
                continue
            self._handle_completion(handle, result)

    def _wait_for_completion(self, timeout: float) -> None:
        """Block up to *timeout* for one completion; handle it if one arrives."""
        try:
            handle, result = self.completion_q.get(timeout=timeout)
        except queue_mod.Empty:
            return
        if handle is _WAKE_HANDLE:
            self._record_event("wake", "completion_q")
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
            self._record_event(
                "complete_unknown",
                type(handle.job).__name__,
                handle.on_done_state,
                self._job_result_event_fields(result),
            )
            logger.warning("completion for unknown handle (already torn down?): %s", handle)
            return
        self._record_event(
            "complete",
            type(handle.job).__name__,
            self._item_key(item),
            item.stage.value,
            handle.on_done_state,
            {
                "descr": getattr(handle.job, "descr", ""),
                **self._job_result_event_fields(result),
            },
        )
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
        item.state = (
            handle.on_done_state.value
            if isinstance(handle.on_done_state, StageName)
            else handle.on_done_state
        )
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
        self._record_event("resumable", self._item_key(item), item.stage.value, item.state)
        logger.info(
            "interrupt: item %s RESUMABLE at %s (never failed)",
            self._item_key(item),
            item.stage.value,
        )

    @staticmethod
    def _job_result_event_fields(result: JobResult) -> dict[str, Any]:
        """Return bounded, output-free job result fields for durable event logs."""
        fields = {
            "ok": result.ok,
            "interrupted": result.interrupted,
            "error": Coordinator._job_result_error_class(result),
            "duration_s": round(result.duration_s, 3),
        }
        if result.worker_id:
            fields["worker_id"] = result.worker_id
        return fields

    @staticmethod
    def _job_result_error_class(result: JobResult) -> str | None:
        """Classify job failures without persisting raw error text."""
        if result.error is None:
            return None
        if result.interrupted:
            return "interrupted"
        if result.error.startswith("worker_crash:"):
            return "worker_crash"
        return "error"

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
                self._record_event("drain", stage_name.value, self._item_key(item))
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
        queued = q.snapshot()
        issue_numbers = [it.issue for it in queued if it.issue is not None]
        duplicate_issues = sorted(
            issue for issue, count in Counter(issue_numbers).items() if count > 1
        )
        assert not duplicate_issues, (  # noqa: S101
            f"implementation queue must not contain duplicate issue numbers: {duplicate_issues}"
        )

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
        if self.config.serialize_file_overlap and self.config.max_workers > 1 and len(ordered) > 1:
            # Resolve each issue's owning repo from its own WorkItem: the queue is
            # keyed by stage, so one round can hold issues from several repos (#1795).
            repo_of = {
                number: (self.config.org, item.repo)
                for number, item in issue_items.items()
                if item.repo
            }
            dispatch, deferred = _admission._select_non_overlapping(ordered, repo_of=repo_of)
            for number in deferred:
                logger.info("implementation #%s deferred (file overlap)", number)
        ran: set[int] = set()
        for number in dispatch:
            item = issue_items[number]
            if self.shutdown.is_set() or not self._admit(item):
                continue  # stays queued (re-pushed below)
            ran.add(id(item))
            self._record_event("drain", StageName.IMPLEMENTATION.value, self._item_key(item))
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

    def _step_with_watchdog(
        self, stage: Stage, item: WorkItem, ctx: StageContext
    ) -> StageStepResult:
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
                # --phase-timeout bounds each AGENT JOB, not a phase subprocess.
                job = replace(job, timeout_s=int(self.config.phase_timeout_s))
        handle = self.pool.submit(
            job,
            request.on_done_state,
            claim_key=self._item_key(item),
            claim_stage=item.stage.value,
        )
        self.in_flight[handle] = item
        self.inflight_per_repo[item.repo] += 1
        self._record_event(
            "submit",
            type(job).__name__,
            self._item_key(item),
            request.on_done_state,
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
        route = self._routes[item.stage]
        disposition = outcome.disposition

        if item.stage is StageName.FINISHED:
            # Sink outcomes are terminal: the result is already recorded.
            self._record_event("done", self._item_key(item), outcome.note)
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
        self._record_event("push", stage.value, self._item_key(item))

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
        discovery_repos = [] if self.config.issues or self.config.prs else self.config.repos
        entries = _seeding.seed_from_cli(discovery_repos, [], [])
        default_repo = self.config.repos[0] if self.config.repos else ""
        if self.config.issues or self.config.prs:
            entries.extend(self._seed_direct_scope(default_repo))
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
                    passed=entry.passed, reason=entry.reason, final_stage=StageName.FINISHED
                )
            self._push_item(item, item.stage, enter=True)
            pushed += 1
        return pushed

    def _clamp_seed_stage_to_scope(
        self,
        issue: int,
        stage: StageName | None,
        reason: str,
        scope_stages: frozenset[StageName] | None,
    ) -> tuple[StageName | None, str]:
        """Compatibility wrapper returning only stage/reason for callers."""
        stage, reason, _passed = self._scope_seed_decision(issue, stage, reason, scope_stages)
        return stage, reason

    def _scope_seed_decision(
        self,
        issue: int,
        stage: StageName | None,
        reason: str,
        scope_stages: frozenset[StageName] | None,
    ) -> tuple[StageName | None, str, bool]:
        """Reconcile a classified entry stage with the run's pipeline scope.

        Full-pipeline runs (``scope_stages is None``) pass the classification
        through unchanged. Under a partial scope (e.g. the planner CLI's
        planning -> plan_review scope) an issue can classify PAST the scope —
        an at-or-past ``state:plan-go`` issue seeds to IMPLEMENTATION, which is
        out of scope. Two reconciliations:

        - ``--force``: re-route any in-pipeline (non-excluded) stage that is not
          already the scope's entry stage back to the scope's FIRST stage so
          the work is redone (for the planner scope, re-plan from PLANNING).
        - default: an issue that classifies past the scope has already
          completed the scoped work, so clamp it to FINISHED (pass) rather than
          push it into an out-of-scope stage the trimmed route table has no row
          for. In-scope classifications (e.g. PLANNING/PLAN_REVIEW) are kept.

        Exclusions (``stage is None``: ``state:skip`` / epic) are never
        overridden — force is a re-plan knob, not a skip bypass.

        Args:
            issue: The issue number (for the reason string).
            stage: The classified entry stage (or None when excluded).
            reason: The classification reason.
            scope_stages: The scope's stage set, or None for a full run.

        Returns:
            The reconciled ``(stage, reason, passed)``. ``passed`` is used when
            the stage is clamped directly to ``FINISHED``.

        """
        if stage is None or scope_stages is None:
            return stage, reason, True

        first_in_scope = next((s for s in PIPELINE_ORDER if s in scope_stages), None)
        if self.config.force:
            # Force re-routes an at-or-past-scope stage back to the scope's
            # entry so the scoped work is redone. A PRE-scope stage (earlier in
            # PIPELINE_ORDER than first_in_scope) is left untouched — force is a
            # redo knob for work already in/past the scope, not a fast-forward
            # that pulls un-started upstream work into the scope. (For the
            # planner planning->plan_review scope direct seeding produces no
            # pre-scope items, but a later scope, e.g. implementation->pr_review,
            # has repo/planning/plan_review upstream.)
            if first_in_scope is not None and stage != first_in_scope:
                first_idx = PIPELINE_ORDER.index(first_in_scope)
                if PIPELINE_ORDER.index(stage) >= first_idx:
                    return first_in_scope, f"#{issue} force re-plan ({reason})", True
            return stage, reason, True

        if stage not in scope_stages:
            if first_in_scope is not None and PIPELINE_ORDER.index(stage) < PIPELINE_ORDER.index(
                first_in_scope
            ):
                return (
                    StageName.FINISHED,
                    f"#{issue} not ready for selected scope ({reason})",
                    False,
                )
            # Classified past the scope: the scoped work is already done.
            return StageName.FINISHED, f"#{issue} already past selected scope ({reason})", True
        return stage, reason, True

    def _seed_direct_scope(self, repo: str) -> list[_seeding.SeedEntry]:
        """Seed explicit ``--issues`` / ``--prs`` through the target repo accessor."""
        github = self._ctx_for_repo(repo).github if repo else self.github
        entries: list[_seeding.SeedEntry] = []
        scope_stages = self.config.scope.stages if self.config.scope is not None else None
        issue_numbers = list(self.config.issues)
        if issue_numbers:
            issue_numbers = _admission._filter_open_issues(repo, issue_numbers)
        for issue in issue_numbers:
            facts = _seeding.seed_issue_from_github(issue, github)
            stage, reason = _seeding.classify_issue(facts)
            stage, reason, passed = self._scope_seed_decision(issue, stage, reason, scope_stages)
            entries.append(
                _seeding.SeedEntry(
                    kind="issue",
                    identifier=issue,
                    stage=stage,
                    reason=reason,
                    pr_number=facts.pr_number if facts.pr_is_open else None,
                    issue_title=facts.title,
                    issue_body=facts.body,
                    passed=passed,
                )
            )
        for pr in self.config.prs:
            issue_number = github.find_issue_for_pr(pr)
            scope_identifier = issue_number if issue_number is not None else pr
            pr_state = github.gh_pr_state(pr)
            pr_state_name = ((pr_state or {}).get("state") or "").upper()
            if pr_state_name == "MERGED":
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=StageName.FINISHED,
                        reason=f"PR #{pr} already merged",
                        pr_number=pr,
                        issue_number=issue_number,
                        passed=True,
                    )
                )
                continue
            if pr_state_name == "CLOSED":
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=StageName.FINISHED,
                        reason=f"PR #{pr} already closed without merging",
                        pr_number=pr,
                        issue_number=issue_number,
                        passed=False,
                    )
                )
                continue
            has_go, _has_no_go = github.pr_has_implementation_state_label(pr)
            if has_go:
                stage, reason, passed = self._scope_seed_decision(
                    scope_identifier,
                    StageName.CI,
                    f"PR #{pr} carries {STATE_IMPLEMENTATION_GO}",
                    scope_stages,
                )
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=stage,
                        reason=reason,
                        pr_number=pr,
                        issue_number=issue_number,
                        passed=passed,
                    )
                )
            else:
                stage, reason, passed = self._scope_seed_decision(
                    scope_identifier,
                    StageName.PR_REVIEW,
                    f"PR #{pr} without {STATE_IMPLEMENTATION_GO} — awaiting review",
                    scope_stages,
                )
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=stage,
                        reason=reason,
                        pr_number=pr,
                        issue_number=issue_number,
                        passed=passed,
                    )
                )
        return entries

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
                repo=default_repo,
                kind=ItemKind.PR,
                issue=entry.issue_number,
                pr=entry.pr_number or int(entry.identifier),
                stage=entry.stage,
            )
        else:
            item = WorkItem(
                repo=default_repo,
                kind=ItemKind.ISSUE,
                issue=int(entry.identifier),
                pr=entry.pr_number,
                stage=entry.stage,
            )
            item.payload["issue_title"] = entry.issue_title
            item.payload["issue_body"] = entry.issue_body
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
        self._seed_pass()
        return self._pass_work_count > 0

    # -- shutdown ---------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """SIGINT/SIGTERM/SIGHUP -> one shutdown Event (first graceful, second immediate)."""

        def _handler(signum: int, frame: object) -> None:
            if self.shutdown.is_set():
                logger.warning("second signal %d: immediate shutdown", signum)
                self._immediate = True
                self._wake_completion_wait()
            else:
                logger.warning(
                    "signal %d: graceful shutdown (grace %.0fs; press again to force)",
                    signum,
                    self.config.grace_s,
                )
                self.shutdown.set()
                self._grace_deadline = time.monotonic() + self.config.grace_s
                self._wake_completion_wait()

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

    Public entry point called from ``loop_runner.main()`` on the default
    queue-pipeline path.

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
