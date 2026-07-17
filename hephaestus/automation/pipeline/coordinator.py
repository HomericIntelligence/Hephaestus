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
    StrictReviewStage,
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
#: stage protocol promises short (<~15s) main-thread steps. 5s proved too
#: tight in practice: routine repo-stage steps (clone + label reads over the
#: network) breached it on nearly every multi-repo run, burying real stalls
#: in noise (#2247).
_STEP_WATCHDOG_S = 15.0

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
    StageName.STRICT_REVIEW,
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
    planner_reasoning_effort: str = ""
    reviewer_reasoning_effort: str = ""
    implementer_reasoning_effort: str = ""
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
    # command so repositories with non-standard unit-test layouts can opt in.
    pre_pr_test_argv: tuple[str, ...] = PRE_PR_TEST_ARGV
    run_pre_pr_tests: bool = False
    serialize_file_overlap: bool = True
    # Zero disables the optional local observability server. Values are
    # validated at the CLI boundary and again by MetricsHTTPServer on use.
    metrics_port: int = 0
    # Alerts are emitted only from measured queue depths and circuit-breaker
    # snapshots. Keep the threshold explicit and non-negative.
    alert_queue_depth_threshold: int = 100
    # A product-layer caller supplies the library breaker snapshot reader. The
    # coordinator remains a zero-I/O pipeline module and never imports the
    # resilience capability directly.
    circuit_breaker_snapshot_provider: Callable[[], dict[str, dict[str, Any]]] | None = None
    event_log_path: Path | None = None
    projects_dir: Path = field(default_factory=lambda: Path.home() / "Projects")
    # Optional exceptions to the normal ``projects_dir / repo`` checkout
    # layout.  The loop runner only sets an entry for a matching noncanonical
    # cwd checkout; unlisted repositories retain the conventional fallback.
    repo_roots: dict[str, Path] = field(default_factory=dict)
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
    planner_reasoning_effort: str = ""
    reviewer_reasoning_effort: str = ""
    implementer_reasoning_effort: str = ""
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


def _effective_repo_root(config: PipelineConfig, repo: str) -> Path:
    """Resolve *repo* to its explicit checkout or conventional projects path."""
    return Path(config.repo_roots.get(repo, Path(config.projects_dir) / repo))


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
        # Observability is opt-in.  Keep imports and all socket setup out of
        # the default construction path so the product layer retains its
        # zero-I/O import contract.
        self._metrics_registry: Any | None = None
        self._metrics_server: Any | None = None
        self._alert_tracker: Any | None = None
        # Gauges retain label series until explicitly updated.  Remember the
        # prior tick's dynamic labels so a completed job or state transition
        # is rendered as zero rather than as stale active work.
        self._observed_inflight_repos: set[str] = set()
        self._observed_circuit_breaker_states: dict[str, str] = {}
        if config.metrics_port:
            from hephaestus.observability.alerts import AlertTracker
            from hephaestus.observability.metrics import MetricsRegistry
            from hephaestus.observability.server import MetricsHTTPServer

            self._metrics_registry = MetricsRegistry()
            self._alert_tracker = AlertTracker(
                queue_depth_threshold=config.alert_queue_depth_threshold
            )
            self._metrics_server = MetricsHTTPServer(
                self._metrics_registry,
                port=config.metrics_port,
                health_provider=self._health_snapshot,
            )
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
            # Copy, not alias: ``ROUTES`` is a module-level shared table, so an
            # accidental in-place edit of ``self._routes`` would corrupt every
            # other run/test. The table is small and built once per run.
            self._routes = dict(ROUTES)

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
        self._pool_shut_down = False
        self._seen_item_ids: set[int] = set()
        self._stage_config = _StageRunConfig(
            enable_advise=not config.no_advise,
            agent=config.agent,
            model=config.model,
            planner_model=config.planner_model,
            reviewer_model=config.reviewer_model,
            implementer_model=config.implementer_model,
            planner_reasoning_effort=config.planner_reasoning_effort,
            reviewer_reasoning_effort=config.reviewer_reasoning_effort,
            implementer_reasoning_effort=config.implementer_reasoning_effort,
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
            StageName.STRICT_REVIEW: StrictReviewStage(),
            StageName.CI: CiStage(),
            StageName.MERGE_WAIT: MergeWaitStage(),
            StageName.FINISHED: FinishedStage(self.ledger, self.preserved),
        }

    def _ctx_for_repo(self, repo: str) -> StageContext:
        """Return the (cached, per-repo) StageContext for *repo*."""
        ctx = self._ctx_cache.get(repo)
        if ctx is None:
            root = _effective_repo_root(self.config, repo)
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

    def _observability_snapshot(self) -> dict[str, Any]:
        """Read the coordinator lifecycle values that observability exposes."""
        circuit_breakers: dict[str, dict[str, Any]] = {}
        provider = self.config.circuit_breaker_snapshot_provider
        if provider is not None:
            try:
                circuit_breakers = provider()
            except Exception:
                # Observability must not terminate a production automation
                # loop if an optional diagnostic provider is broken.
                logger.exception("circuit-breaker snapshot provider failed")

        return {
            "queue_depths": {name.value: len(queue) for name, queue in self.queues.items()},
            "inflight_per_repo": dict(self.inflight_per_repo),
            "inflight_jobs": len(self.in_flight),
            "circuit_breakers": circuit_breakers,
            "loops_run": self._loops_run,
        }

    def _health_snapshot(self) -> dict[str, Any]:
        """Return the local server's JSON health response without external I/O."""
        snapshot = self._observability_snapshot()
        snapshot["status"] = "stopping" if self.shutdown.is_set() else "ok"
        return snapshot

    def _emit_observability_tick(self) -> None:
        """Update live gauges and durably record alert state transitions."""
        registry = self._metrics_registry
        tracker = self._alert_tracker
        if registry is None or tracker is None:
            return
        snapshot = self._observability_snapshot()
        for stage, depth in snapshot["queue_depths"].items():
            registry.gauge(
                "hephaestus_pipeline_queue_depth",
                "Queued pipeline work items by stage.",
            ).set(depth, labels={"stage": stage})
        registry.gauge(
            "hephaestus_pipeline_inflight_jobs",
            "Pipeline jobs currently owned by the worker pool.",
        ).set(snapshot["inflight_jobs"])
        inflight_by_repo = registry.gauge(
            "hephaestus_pipeline_inflight_per_repo",
            "Pipeline jobs currently in flight by repository.",
        )
        current_repos: set[str] = set()
        for repo, count in snapshot["inflight_per_repo"].items():
            repo_name = str(repo)
            current_repos.add(repo_name)
            inflight_by_repo.set(count, labels={"repo": repo_name})
        for repo in self._observed_inflight_repos - current_repos:
            inflight_by_repo.set(0, labels={"repo": repo})
        self._observed_inflight_repos = current_repos

        breaker_states = registry.gauge(
            "hephaestus_circuit_breaker_state",
            "Circuit-breaker lifecycle state (active state has value 1).",
        )
        current_breaker_states: dict[str, str] = {}
        for name, breaker in snapshot["circuit_breakers"].items():
            breaker_name = str(name)
            state = str(breaker["state"])
            previous_state = self._observed_circuit_breaker_states.get(breaker_name)
            if previous_state is not None and previous_state != state:
                breaker_states.set(0, labels={"name": breaker_name, "state": previous_state})
            breaker_states.set(1, labels={"name": breaker_name, "state": state})
            current_breaker_states[breaker_name] = state
        for name, state in self._observed_circuit_breaker_states.items():
            if name not in current_breaker_states:
                breaker_states.set(0, labels={"name": name, "state": state})
        self._observed_circuit_breaker_states = current_breaker_states

        self._record_event("metrics_snapshot", snapshot)
        for event in tracker.observe(snapshot):
            registry.gauge(
                "hephaestus_pipeline_alert_active",
                "Current active pipeline alert state (1 active, 0 resolved).",
            ).set(int(event.status == "fired"), labels={"name": event.name})
            self._record_event(
                f"alert_{event.status}",
                {
                    "name": event.name,
                    "severity": event.severity,
                    "message": event.message,
                },
            )

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
            if self._metrics_server is not None:
                self._metrics_server.start()
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
                self._emit_observability_tick()
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
            # Reap the pool on EVERY exit path — a fatal exception never sets
            # self.shutdown, so without this the executor and in-flight AgentJob
            # subprocesses (e.g. claude reviewers) would leak (#2059). Idempotent
            # via _pool_shut_down, so the signal path's earlier call is a no-op.
            self._shutdown_pool()
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
            try:
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
            finally:
                if self._metrics_server is not None:
                    self._metrics_server.stop()
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

        The queue is STAGE-keyed, so one drain round can hold issues from
        several repos (#1795), and dependency ordering runs across the whole
        round on a shared issue-number space (``IssueInfo`` docstring). Two
        distinct work items therefore only conflict when they share the SAME
        ``(repo, issue)`` — that is the transient retry/fail-back re-enqueue we
        collapse below. Two DIFFERENT repos that happen to share an issue number
        (``A#71`` vs ``B#71``) are NOT duplicates and must both dispatch; the
        old code (and the pre-#2057 assert) keyed on issue number alone and
        would have silently dropped one or crashed (#2057).
        """
        q = self.queues[StageName.IMPLEMENTATION]
        if not len(q):
            return
        items = self._dedup_implementation_items([q.pop() for _ in range(len(q))])
        issue_items, ambiguous = self._index_issue_items(items)
        infos = [
            IssueInfo(
                number=number,
                title=str(item.payload.get("issue_title", "")),
                dependencies=list(item.payload.get("dependencies", [])),
            )
            for number, item in issue_items.items()
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
        # Cross-repo same-number items bypass the number-keyed gates and dispatch
        # directly — they are distinct work that the ordering model cannot rank.
        dispatch_items = [issue_items[number] for number in dispatch]
        dispatch_items.extend(it for group in ambiguous.values() for it in group)
        for item in dispatch_items:
            if self.shutdown.is_set() or not self._admit(item):
                continue  # stays queued (re-pushed below)
            ran.add(id(item))
            self._record_event("drain", StageName.IMPLEMENTATION.value, self._item_key(item))
            self._run_item(item)
        # Preserve original queue order for deferred / non-admitted / non-issue items.
        for it in items:
            if id(it) not in ran:
                q.push(it)

    def _dedup_implementation_items(self, items: list[WorkItem]) -> list[WorkItem]:
        """Drop transient duplicate work items, keyed by ``(repo, issue)`` (#2057).

        A retry/fail-back can re-enqueue an issue while a prior copy is still
        queued, so the round may briefly hold two items for the same
        ``(repo, issue)``. Keep the first-queued and terminalize the rest as
        superseded (was a hard assert that crashed the whole run, #1952). Keyed
        by ``(repo, issue)`` so a cross-repo same-number pair (``A#71``/``B#71``)
        is preserved as distinct work. ``issue is None`` items never dedup.
        """
        seen: set[tuple[str, int]] = set()
        deduped: list[WorkItem] = []
        for it in items:
            if it.issue is None:
                deduped.append(it)
                continue
            key = (it.repo, it.issue)
            if key in seen:
                logger.warning(
                    "implementation %s#%s already queued; dropping duplicate work item",
                    it.repo,
                    it.issue,
                )
                self._finish(
                    it, passed=True, reason=f"{it.repo}#{it.issue} superseded by queued duplicate"
                )
                continue
            seen.add(key)
            deduped.append(it)
        return deduped

    @staticmethod
    def _index_issue_items(
        items: list[WorkItem],
    ) -> tuple[dict[int, WorkItem], dict[int, list[WorkItem]]]:
        """Index issue items by number for number-keyed topo/overlap dispatch.

        Returns ``(issue_items, ambiguous)``. Dispatch is driven by ordered issue
        NUMBERS, so items are indexed by number to look back up. A cross-repo
        same-number pair collides in that dict — those numbers move to
        ``ambiguous`` (issue number → its distinct items) and dispatch directly,
        bypassing the number-keyed topo/overlap gates, which cannot represent two
        items under one number. The ambiguity is inherent to the shared
        issue-number-space dependency model (``IssueInfo``); dispatching both is
        correct — neither is a duplicate (#2057). ``issue is None`` items are
        skipped (dispatched elsewhere / re-queued).
        """
        issue_items: dict[int, WorkItem] = {}
        ambiguous: dict[int, list[WorkItem]] = {}
        for it in items:
            if it.issue is None:
                continue
            if it.issue in ambiguous:
                ambiguous[it.issue].append(it)
            elif it.issue in issue_items:
                ambiguous[it.issue] = [issue_items.pop(it.issue), it]
            else:
                issue_items[it.issue] = it
        return issue_items, ambiguous

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
        """Run one stage.step, warning when it breaches the <~15s contract."""
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
        route = self._routes.get(item.stage)
        if route is None:
            # A stage absent from this run's (possibly scope-trimmed) route
            # table has no next/fail mapping — routing it would KeyError. This
            # happens when a seeder-created REPO item is poisoned under a
            # partial ``--phases`` scope whose ``trimmed_routes`` omits REPO
            # (#2294). Fail closed to the sink instead of crashing the whole
            # run, which the poison handler that called us already intends.
            logger.error(
                "coordinator: %s has no route in this run's stage scope; "
                "finishing failed instead of crashing (%s)",
                item.stage.value,
                outcome.note or "unroutable",
            )
            reason = outcome.note or f"unroutable:{item.stage.value}"
            self._finish(item, passed=False, reason=reason)
            return
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

    def _live_issue_keys(self) -> set[tuple[str, int]]:
        """Return ``(repo, issue)`` keys currently queued (any stage) or in-flight.

        The identity set the upstream idempotency guard consults: an ISSUE item is
        "live" if a WorkItem for the same ``(repo, issue)`` sits in any stage queue
        or in ``in_flight``. Cross-repo same-number issues are distinct (#2058).
        """
        keys: set[tuple[str, int]] = set()
        for q in self.queues.values():
            for it in q.snapshot():
                if it.kind is ItemKind.ISSUE and it.issue is not None:
                    keys.add((it.repo, it.issue))
        for it in self.in_flight.values():
            if it.kind is ItemKind.ISSUE and it.issue is not None:
                keys.add((it.repo, it.issue))
        return keys

    def _push_item(self, item: WorkItem, stage: StageName, enter: bool) -> None:
        """Push *item* into *stage*'s queue (the single push chokepoint).

        Every durable GitHub mutation for this transition already happened
        inside the stage, immediately before the outcome that got us here.

        Upstream idempotency guard (#2107): a genuinely NEW ISSUE work item whose
        ``(repo, issue)`` is already queued (any stage) or in-flight is refused —
        it never enters the pipeline, so the drain-level dedup (#2058) is not even
        exercised. Object identity (``_seen_item_ids``) distinguishes a new item
        from an already-tracked item re-pushing itself on timer/retry/fail-back/
        advance, which must always be allowed through.
        """
        if (
            id(item) not in self._seen_item_ids
            and item.kind is ItemKind.ISSUE
            and item.issue is not None
            and (item.repo, item.issue) in self._live_issue_keys()
        ):
            logger.info("seed skipped: #%s already queued/in-flight in %s", item.issue, item.repo)
            return
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
        if not self.config.issues and not self.config.prs:
            entries.extend(self._pending_arm_recovery_entries())
        if self.config.issues or self.config.prs:
            entries.extend(self._seed_direct_scope(default_repo))
        pushed = 0
        for entry in entries:
            if entry.stage is None:
                # Epic tagging is the ONE sanctioned seeding write, executed
                # here through the skip_epics chokepoint BEFORE the exclusion
                # is honored (seeding.py write-path boundary).
                if entry.skip_tag_obligation is not None:
                    self.github.skip_epics({entry.skip_tag_obligation.issue: []})
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

    def _pending_arm_recovery_entries(self) -> list[_seeding.SeedEntry]:
        """Seed non-terminal durable arms through merge_wait after a restart."""
        entries: list[_seeding.SeedEntry] = []
        scope_stages = self.config.scope.stages if self.config.scope is not None else None
        for repo in self.config.repos:
            reader = getattr(self._ctx_for_repo(repo).github, "pending_drive_green_arms", None)
            if not callable(reader):
                continue
            try:
                records = reader()
            except Exception as exc:
                logger.warning("drive-green arm recovery read failed for %s: %s", repo, exc)
                continue
            for issue_number, pr_number in records:
                stage, reason, passed = self._scope_seed_decision(
                    issue_number,
                    StageName.MERGE_WAIT,
                    f"PR #{pr_number} has a pending durable drive-green arm record",
                    scope_stages,
                )
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr_number,
                        stage=stage,
                        reason=reason,
                        pr_number=pr_number,
                        issue_number=issue_number,
                        passed=passed,
                        merge_wait_recovery=True,
                    )
                )
        return entries

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
        reader = getattr(github, "pending_drive_green_arms", None)
        pending_arms = set(reader()) if callable(reader) else set()
        requested_issues = list(self.config.issues)
        recovered_issues: set[int] = set()
        for issue, pr in pending_arms:
            if issue not in requested_issues:
                continue
            stage, reason, passed = self._scope_seed_decision(
                issue,
                StageName.MERGE_WAIT,
                f"PR #{pr} has a pending durable drive-green arm record",
                scope_stages,
            )
            entries.append(
                _seeding.SeedEntry(
                    kind="pr",
                    identifier=pr,
                    stage=stage,
                    reason=reason,
                    pr_number=pr,
                    issue_number=issue,
                    passed=passed,
                    merge_wait_recovery=True,
                )
            )
            recovered_issues.add(issue)
        issue_numbers = [issue for issue in requested_issues if issue not in recovered_issues]
        if issue_numbers:
            issue_numbers = _admission._filter_open_issues(repo, issue_numbers)
        for issue in issue_numbers:
            facts = _seeding.seed_issue_from_github(issue, github)
            entry = _seeding.seed_entry_from_facts(facts)
            stage, reason = entry.stage, entry.reason
            stage, reason, passed = self._scope_seed_decision(issue, stage, reason, scope_stages)
            entries.append(replace(entry, stage=stage, reason=reason, passed=passed))
        for pr in self.config.prs:
            issue_number = github.find_issue_for_pr(pr)
            scope_identifier = issue_number if issue_number is not None else pr
            if issue_number is not None and (issue_number, pr) in pending_arms:
                stage, reason, passed = self._scope_seed_decision(
                    scope_identifier,
                    StageName.MERGE_WAIT,
                    f"PR #{pr} has a pending durable drive-green arm record",
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
                        merge_wait_recovery=True,
                    )
                )
                continue
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
                    StageName.MERGE_WAIT,
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
        if entry.merge_wait_recovery:
            item.payload["merge_wait_recovery"] = True
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
        self._shutdown_pool()

    def _shutdown_pool(self) -> None:
        """Cancel the pool and park in-flight items RESUMABLE. Idempotent.

        Called on BOTH exit paths: the signal path (via
        :meth:`_teardown_immediate`) and — critically — the ``run()`` ``finally``
        block, so a fatal exception (which never sets ``self.shutdown``) still
        cancels the executor and reaps in-flight ``AgentJob`` subprocesses
        instead of leaking them (#2059). Guarded by ``_pool_shut_down`` so a
        signal-then-fatal (or double ``finally``) sequence shuts down once.
        """
        if self._pool_shut_down:
            return
        self._pool_shut_down = True
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
        if not self.shutdown.is_set() and not self._fatal:
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
    repo_root = _effective_repo_root(config, repo) if repo else Path(config.projects_dir)
    github = (
        _github_for(repo, repo_root) if repo else PipelineGitHub(config.org, dry_run=config.dry_run)
    )
    coordinator = Coordinator(config, github=github, github_factory=_github_for)
    return coordinator.run()
