"""Single-threaded event-loop coordinator for the queue-based pipeline (epic #1809).

## Semantics

The coordinator runs on the process main thread and owns all seven stage
queues, the timer heap, the in-flight registry, all routing, and (through the
:class:`~hephaestus.automation.pipeline_github.PipelineGitHub` accessor) every
GitHub API mutation. A single worker pool executes agent, build/test, and
git/network jobs; the ONLY cross-thread data channel is the completion queue.
A separate event latch wakes the idle loop for both accepted completions and
signals, so neither a worker callback nor a signal handler can block on a
full queue.

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
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeAlias

from jinja2 import TemplateNotFound

from hephaestus.automation.models import IssueInfo
from hephaestus.automation.pipeline import admission as _admission, seeding as _seeding
from hephaestus.automation.pipeline.events import StageEvent, encode_stage_event
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue, StageQueue, StageQueueLease
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
from hephaestus.automation.pipeline.stages.repo import (
    DIRECT_SCOPE_BASE_SHA_KEY,
    DIRECT_SCOPE_BOOTSTRAP_KEY,
    RepoIssueSource,
    is_full_commit_sha,
    product_to_work_item,
)
from hephaestus.automation.pipeline.summary import (
    RunStats,
    TerminalSummary,
    latest_logical_items,
    print_summary,
)
from hephaestus.automation.pipeline.work_item import (
    ItemKind,
    ItemResult,
    PreservedWorktree,
    WorkItem,
)
from hephaestus.automation.state_labels import STATE_IMPLEMENTATION_GO, STATE_PLAN_BLOCKED, is_epic
from hephaestus.prompts import PromptCatalog

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

# Host-owned work-item payload key for the immutable plan-file snapshot that
# admitted a queued implementation job.  It is consumed at submission and is
# never sourced from GitHub content.
_IMPLEMENTATION_FILE_CLAIMS_PAYLOAD = "_implementation_file_claims"

#: Upper bound on Continue-transitions per _run_item call (defensive: a stage
#: that never yields a JobRequest/StageOutcome would otherwise spin forever).
_MAX_STEPS_PER_TICK = 100

#: Global safety cap on FAIL_BACK regressions per item: the sum of every
#: budget in ROUTES. Stages enforce the real per-key budgets themselves (the
#: house on_job_done pattern); this cap only guarantees cross-stage regression
#: cycles terminate even if a stage's own bookkeeping has a bug.
_FAIL_BACK_CAP = sum(sum(route.budgets.values()) for route in ROUTES.values())

_PROMPT_PREFLIGHT_TEMPLATE = "shared/untrusted_notice.j2"
_PROMPT_PREFLIGHT_ERROR = "ERROR: Prompt templates missing or unreadable — reinstall: `uv sync`."

# In-memory diagnostics are intentionally finite.  The durable JSONL stream,
# when configured, remains a best-effort diagnostic artifact only; GitHub
# state is still the restart authority.
_DEFAULT_EVENT_LOG_CAPACITY = 1_024
_DEFAULT_TERMINAL_DETAIL_CAPACITY = 128
_SOURCE_REGISTRY_RETRY_DELAY_S = 0.05

#: Downstream-first drain order: finish work before admitting new (epic
#: #1809 "drain queues downstream-first (merge_wait -> ... -> repo)"; the
#: finished sink drains first of all so results are recorded promptly).
_DRAIN_ORDER: tuple[StageName, ...] = (
    StageName.FINISHED,
    StageName.MERGE_WAIT,
    StageName.PR_REVIEW,
    StageName.IMPLEMENTATION,
    StageName.PLAN_REVIEW,
    StageName.PLANNING,
    StageName.REPO,
)

# An explicit issue can classify into any of these queues.  The source cursor
# classifies only when every possible destination can accept one item, so the
# classification result never needs an unbounded spill buffer while it waits
# for a full stage queue.
_DIRECT_ISSUE_ENTRY_STAGES: frozenset[StageName] = frozenset(
    {
        StageName.PLANNING,
        StageName.PLAN_REVIEW,
        StageName.IMPLEMENTATION,
        StageName.PR_REVIEW,
        StageName.MERGE_WAIT,
        StageName.FINISHED,
    }
)

# A direct PR that reached one of these explicit safety handoffs cannot make
# further progress until a person changes the review state on GitHub.  Retain
# the identity only for this coordinator invocation so the current run
# converges, while a later invocation still re-evaluates fresh GitHub facts.
_DIRECT_PR_HANDOFF_REASONS: frozenset[str] = frozenset(
    {"human_blocked", "automation_threads_require_human_resolution"}
)

StageStepResult: TypeAlias = Continue | JobRequest | StageOutcome


def _budget_lookup(name: str) -> int:
    """Look up a budget across all ROUTES rows (conservative default 1)."""
    for route in ROUTES.values():
        if name in route.budgets:
            return route.budgets[name]
    return 1


def _preflight_prompt_catalog() -> None:
    """Fail before pipeline startup when packaged prompts cannot be loaded."""
    try:
        PromptCatalog.current().render(_PROMPT_PREFLIGHT_TEMPLATE)
    except (OSError, TemplateNotFound, ValueError) as exc:
        raise SystemExit(f"{_PROMPT_PREFLIGHT_ERROR}\nCause: {exc}") from exc


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
    # An org-wide invocation supplies a fresh paged source per pass instead
    # of materializing every repository in ``repos``.  The callable keeps
    # source state out of the durable configuration and makes reseeds restart
    # discovery from GitHub, which remains the sole routing authority.
    repo_source_factory: Callable[[], Iterator[str]] | None = None
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
    # Per-budget overrides applied on top of the ROUTES defaults by the
    # coordinator's budget accessor.
    budget_overrides: dict[str, int] = field(default_factory=dict)
    # Configurable argv for the optional pre-PR test gate. The
    # implementation stage reads this vector instead of hardcoding the test
    # command so repositories with non-standard test layouts can opt in.
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
    # Recent local diagnostic retention.  These limits intentionally do not
    # alter the GitHub journal or restart behavior.
    event_log_capacity: int = _DEFAULT_EVENT_LOG_CAPACITY
    terminal_detail_capacity: int = _DEFAULT_TERMINAL_DETAIL_CAPACITY
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


def _work_window(config: PipelineConfig) -> int:
    """Return the global bound for all nonterminal pipeline work."""
    return max(1, config.parallel_repos * config.max_workers)


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
    pre_pr_test_argv: tuple[str, ...] = PRE_PR_TEST_ARGV


@dataclass
class _Paths:
    """Coordinator-owned path accessor injected as ``StageContext.paths``."""

    repo_root: Path
    worktree: Path
    projects_dir: Path


@dataclass(frozen=True)
class _PendingHandoff:
    """A completed route retained by its source-stage lease.

    ``StageQueueLease`` keeps the source slot occupied until the destination
    accepts the item.  The coordinator records only the next intent here; it
    deliberately does not mutate ``WorkItem.stage``, ``state``, history, or a
    terminal result until that destination-first admission succeeds.  There
    can be at most one pending handoff per active lease, so this is bounded by
    the fixed capacity of the coordinator's stage queues rather than acting as
    a spill buffer.
    """

    target: StageName
    enter: bool
    result: ItemResult | None = None


@dataclass
class _DirectIssueSource:
    """One bounded cursor over the caller-provided explicit issue scope.

    The cursor stores no classified :class:`SeedEntry` or :class:`WorkItem`.
    Classification happens only after all possible direct-entry queues can
    accept an item, allowing the resulting item to enter immediately.
    """

    repo: str
    issues: Iterator[int]
    base_sha: str


@dataclass
class _DirectPrSource:
    """One bounded cursor over the caller-provided explicit PR scope.

    As with direct issues, a PR is classified only at a safe admission point.
    This avoids retaining an eager list of potentially large review-context
    payloads while one of the downstream review queues is saturated.
    """

    repo: str
    prs: Iterator[int]
    base_sha: str


@dataclass
class _ActiveRepoIssueSource:
    """One detached, bounded repository issue cursor owned by the coordinator.

    Repository setup is a normal REPO-stage work item.  Once it has produced
    this cursor, retaining that item would monopolize a REPO queue lease and
    let one repository starve its peers.  The cursor registry keeps only this
    minimal state and is capped by the global work window.
    """

    repo: str
    source: RepoIssueSource


@dataclass
class _RepoEntrySource:
    """One bounded FIFO cursor over repository discovery entries.

    A REPO-stage lease remains with the active repository through its issue
    cursor, so this source advances only when both the global permit and the
    REPO queue admit the next repository. ``pending`` is a defensive single
    retry slot; it never becomes a repository spill list.
    """

    repos: Iterator[str]
    pending: str | None = None


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
        if config.event_log_capacity < 1:
            raise ValueError("event_log_capacity must be positive")
        if config.terminal_detail_capacity < 1:
            raise ValueError("terminal_detail_capacity must be positive")
        self.shutdown = threading.Event()
        # These latches are the control plane for the bounded completion
        # queue.  They carry no WorkItem/JobResult payload and therefore
        # cannot become a second, unbounded completion buffer.
        self._completion_wakeup = threading.Event()
        self._completion_saturation = threading.Event()
        work_window = _work_window(config)
        self.completion_q: CompletionQueue = queue_mod.Queue(maxsize=work_window)
        if pool is None:
            # Imported here, not module-top: WorkerPool is the pipeline's one
            # I/O-capable module and tests never need it.
            from hephaestus.automation.pipeline.worker_pool import WorkerPool

            pool = WorkerPool(
                size=work_window,
                shutdown=self.shutdown,
                completion_q=self.completion_q,
            )
        else:
            # The coordinator owns the cross-thread transport.  An injected
            # unbounded fake queue is replaced; a differently bounded queue
            # is rejected so it cannot silently weaken the global capacity.
            injected_completion_q = getattr(pool, "completion_q", None)
            injected_maxsize = getattr(injected_completion_q, "maxsize", 0)
            if isinstance(injected_maxsize, int) and (
                injected_maxsize > 0 and injected_maxsize != work_window
            ):
                raise ValueError(
                    "injected completion queue capacity must match the coordinator work window"
                )
            # Test doubles conventionally expose ``completion_q`` while the
            # production WorkerPool keeps the channel private. Rebind both
            # shapes so an injected real pool cannot publish into the stale
            # queue supplied to its constructor.
            pool.completion_q = self.completion_q
            if hasattr(pool, "_completion_q"):
                pool._completion_q = self.completion_q
        self.pool: Any = pool
        set_completion_notifiers = getattr(pool, "set_completion_notifiers", None)
        if callable(set_completion_notifiers):
            set_completion_notifiers(
                wakeup=self._completion_wakeup,
                saturation=self._completion_saturation,
            )

        self.queues: dict[StageName, StageQueue] = {
            name: StageQueue(work_window) for name in StageName
        }
        self.timers: list[tuple[float, int, WorkItem]] = []
        self.in_flight: dict[JobHandle, WorkItem] = {}
        # Known implementation plans retain their repository-scoped file
        # claims for the lifetime of the submitted job.  Never reconstruct
        # this from mutable issue comments during later drain rounds (#2451).
        self._inflight_implementation_claims: dict[JobHandle, set[_admission.PlanFileClaim]] = {}
        self.inflight_per_repo: Counter[str] = Counter()
        # A normal (non-implementation) drain claims instead of popping.  The
        # active lease reserves its source capacity while an item executes or
        # waits for a worker completion.  A pending route is represented by a
        # single intent attached to that lease, never by an overflow queue.
        self._leases: dict[int, StageQueueLease] = {}
        self._pending_handoffs: dict[int, _PendingHandoff] = {}
        self._direct_issue_source: _DirectIssueSource | None = None
        self._direct_pr_source: _DirectPrSource | None = None
        self._direct_pr_handoff_keys: set[tuple[str, int]] = set()
        self._direct_scope_bootstrap_pending = False
        self._repo_entry_source: _RepoEntrySource | None = None
        self._repo_issue_sources: deque[_ActiveRepoIssueSource] = deque()
        # A StageQueue's capacity only bounds that one stage.  This permit
        # set is the coordinator-wide admission budget: an item acquires one
        # permit on first entry and keeps it while it moves between queues,
        # leases, in-flight jobs, timers, and a retained handoff.  It releases
        # only after the finished sink completes.  The set is therefore
        # bounded by ``_work_window(config)``, not by the number of stages.
        self._live_work_permit_ids: set[int] = set()
        self.ledger: list[ItemResult] = []
        self.preserved: list[PreservedWorktree] = []
        self.items: list[WorkItem] = []
        self._terminal_summary = TerminalSummary()
        self.event_log: deque[tuple[Any, ...]] = deque(maxlen=config.event_log_capacity)
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
            pre_pr_test_argv=config.pre_pr_test_argv,
            run_pre_pr_tests=config.run_pre_pr_tests,
        )
        # A context contains a GitHub accessor and path configuration but no
        # mutable item state.  At most C items can be live, so an LRU of C is
        # enough for concurrent work and prevents all-org discovery from
        # retaining one accessor per repository.
        self._ctx_cache: OrderedDict[str, StageContext] = OrderedDict()
        self._ctx_cache_capacity = work_window

    # -- wiring ---------------------------------------------------------------

    def _default_stages(self) -> dict[StageName, Stage]:
        """Build the full production stage map."""
        return {
            StageName.REPO: RepoStage(),
            StageName.PLANNING: PlanningStage(),
            StageName.PLAN_REVIEW: PlanReviewStage(),
            StageName.IMPLEMENTATION: ImplementationStage(),
            StageName.PR_REVIEW: PrReviewStage(),
            StageName.MERGE_WAIT: MergeWaitStage(),
            StageName.FINISHED: FinishedStage(self.ledger, self.preserved),
        }

    def _ctx_for_repo(self, repo: str) -> StageContext:
        """Return the (cached, per-repo) StageContext for *repo*."""
        ctx = self._ctx_cache.get(repo)
        if ctx is not None:
            self._ctx_cache.move_to_end(repo)
        else:
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
                now_fn=time.monotonic,
                budget_fn=self._budget_for,
                event_fn=self._record_stage_event,
            )
            if len(self._ctx_cache) >= self._ctx_cache_capacity:
                self._ctx_cache.popitem(last=False)
            self._ctx_cache[repo] = ctx
        return ctx

    def _ctx_for(self, item: WorkItem) -> StageContext:
        """Return the (cached, per-repo) StageContext for *item*."""
        return self._ctx_for_repo(item.repo)

    def _budget_for(self, name: str) -> int:
        """Config-aware budget accessor injected as ``StageContext.budget_fn``.

        A ``config.budget_overrides`` entry (e.g. ``--max-fix-iterations N`` ->
        takes precedence over the ROUTES default, so a caller can tune a
        stage's per-item budget without editing the routing table.
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
            "stalled_ticks": self._stalled_ticks,
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

        registry.gauge(
            "hephaestus_pipeline_loops_total",
            "Reseed passes run by this coordinator process.",
        ).set(snapshot["loops_run"])
        registry.gauge(
            "hephaestus_pipeline_stalled_ticks",
            "Consecutive drain ticks without pipeline progress.",
        ).set(snapshot["stalled_ticks"])

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
        """Wake the coordinator without writing a sentinel into its bounded queue."""
        self._completion_wakeup.set()

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
                    "repo_source": "streamed" if self.config.repo_source_factory else "explicit",
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
                self._drain_repo_entry_source()
                self._drain_repo_issue_sources()
                self._drain_direct_pr_source()
                self._drain_direct_issue_source()
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
                print_summary(
                    summary_items,
                    stats,
                    preserved,
                    json_out=self.config.json_out,
                    terminal_summary=(
                        self._terminal_summary if self._terminal_summary.total else None
                    ),
                )
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
        if self._fatal:
            return 1
        if self._terminal_summary.total:
            passing = self._terminal_summary.dispositions.get("pass", 0)
            return 1 if passing < self._terminal_summary.total else 0
        effective_results = [item.result for item in self._effective_items() if item.result]
        results = effective_results or self.ledger
        if any(not result.passed for result in results):
            return 1
        return 0

    def _all_idle(self) -> bool:
        """Return True when no ready, leased, timed, or in-flight work remains."""
        return (
            all(len(q) == 0 for q in self.queues.values())
            and not self.timers
            and not self.in_flight
            and not self._leases
            and not self._pending_handoffs
            and self._direct_issue_source is None
            and self._direct_pr_source is None
            and self._repo_entry_source is None
            and not self._repo_issue_sources
        )

    @property
    def live_work_count(self) -> int:
        """Return the number of nonterminal work permits currently held."""
        return len(self._live_work_permit_ids)

    def _try_acquire_work_permit(self, item: WorkItem) -> bool:
        """Reserve one global live-work slot for a newly admitted item."""
        item_id = id(item)
        if item_id in self._live_work_permit_ids:
            return True
        if self.live_work_count >= _work_window(self.config):
            return False
        self._live_work_permit_ids.add(item_id)
        return True

    def _release_work_permit(self, item: WorkItem) -> None:
        """Release *item*'s permit after the terminal sink has completed."""
        self._live_work_permit_ids.discard(id(item))

    def _record_terminal_result(self, item: WorkItem) -> None:
        """Aggregate one completed/resumable item and trim detailed retention.

        The local result collections are an operator convenience, not recovery
        state.  Keep only the configured newest completed details while a
        constant-space aggregate preserves the full run's pass/fail/total
        reporting and exit status.
        """
        if item.result is None or item.payload.get("_summary_recorded", False):
            return
        if (
            item.kind is ItemKind.PR
            and item.pr is not None
            and item.result.reason in _DIRECT_PR_HANDOFF_REASONS
        ):
            self._direct_pr_handoff_keys.add((item.repo, item.pr))
        item.payload["_summary_recorded"] = True
        self._terminal_summary.record(item)
        self._seen_item_ids.discard(id(item))

        # Move a completed item to the tail so the bounded window is ordered
        # by terminal completion rather than by its initial seed time.  An
        # item can be absent in narrow direct unit tests; runtime items are
        # always tracked by _push_item first.
        for index, candidate in enumerate(self.items):
            if candidate is item:
                self.items.pop(index)
                self.items.append(item)
                break

        retained = self.config.terminal_detail_capacity
        completed = [
            index
            for index, candidate in enumerate(self.items)
            if candidate.payload.get("_summary_recorded", False)
        ]
        for index in reversed(completed[:-retained]):
            self.items.pop(index)

        # The sink may record a result before a cleanup job completes.  At
        # most C such items can coexist, so these lists are bounded by N + C
        # between records and by N after every terminal completion.
        if len(self.ledger) > retained:
            del self.ledger[:-retained]
        if len(self.preserved) > retained:
            del self.preserved[:-retained]

    # -- stage-queue leases -------------------------------------------------

    def _claim_item(self, stage_name: StageName, *, index: int = 0) -> WorkItem | None:
        """Claim one ready item while retaining its source-stage capacity."""
        lease = self.queues[stage_name].claim_at(index)
        if lease is None:
            return None
        item = lease.item
        item_id = id(item)
        if item_id in self._leases:  # pragma: no cover - internal invariant
            lease.restore()
            raise RuntimeError(f"duplicate active lease for {self._item_key(item)}")
        self._leases[item_id] = lease
        return item

    def _restore_source_lease(self, item: WorkItem) -> bool:
        """Return an active item lease to its source queue without a transition."""
        self._pending_handoffs.pop(id(item), None)
        lease = self._leases.pop(id(item), None)
        if lease is None:
            return False
        lease.restore()
        return True

    def _release_source_lease(self, item: WorkItem) -> bool:
        """Release an active lease when work leaves every stage queue.

        Timers and terminal sink completion are neither source restores nor
        destination-first handoffs.  The external owner takes responsibility
        for the item, so release its source slot directly without disturbing a
        different queue head selected ahead of it by topo scheduling.
        """
        self._pending_handoffs.pop(id(item), None)
        lease = self._leases.pop(id(item), None)
        if lease is None:
            return False
        lease.release()
        return True

    def _activate_handoff(
        self,
        item: WorkItem,
        target: StageName,
        *,
        enter: bool,
        result: ItemResult | None,
    ) -> None:
        """Publish a route only after its destination accepted the item."""
        item.stage = target
        if enter:
            item.state = "ENTER"
            item.payload["_enter_pending"] = True
            item.add_history_event(target, item.state, note="enqueued")
        if result is not None:
            item.result = result
        self._record_event("push", target.value, self._item_key(item))

    def _handoff_item(
        self,
        item: WorkItem,
        target: StageName,
        *,
        enter: bool,
        result: ItemResult | None = None,
    ) -> bool:
        """Route an item destination-first, retaining a full-target intent.

        Direct unit-test calls do not own a source lease and retain the
        historical push behavior.  Normal drains always carry a lease, so a
        full destination only records a bounded intent and the completed
        stage action is never replayed.
        """
        lease = self._leases.get(id(item))
        if lease is None:
            if result is not None:
                item.result = result
            self._push_item(item, target, enter=enter)
            return True

        if target is item.stage:
            # RETRY to the source is a restore, not a self-handoff: a held
            # lease deliberately blocks its source's ``offer`` method.
            if result is not None:  # pragma: no cover - terminal target is FINISHED
                raise RuntimeError("terminal result cannot route to the source stage")
            return self._restore_source_lease(item)

        if lease.handoff(self.queues[target]):
            self._leases.pop(id(item), None)
            self._pending_handoffs.pop(id(item), None)
            self._activate_handoff(item, target, enter=enter, result=result)
            self._progress = True
            return True

        pending = _PendingHandoff(target=target, enter=enter, result=result)
        existing = self._pending_handoffs.setdefault(id(item), pending)
        if existing != pending:  # pragma: no cover - no item can route twice while leased
            raise RuntimeError(f"conflicting pending handoff for {self._item_key(item)}")
        self._record_event(
            "handoff_pending",
            item.stage.value,
            target.value,
            self._item_key(item),
        )
        return False

    def _drain_pending_handoffs(self) -> None:
        """Retry every retained route whose destination may have opened."""
        for item_id, pending in list(self._pending_handoffs.items()):
            lease = self._leases.get(item_id)
            if lease is None:  # pragma: no cover - defensive bookkeeping repair
                self._pending_handoffs.pop(item_id, None)
                continue
            item = lease.item
            if not lease.handoff(self.queues[pending.target]):
                continue
            self._leases.pop(item_id, None)
            self._pending_handoffs.pop(item_id, None)
            self._activate_handoff(
                item,
                pending.target,
                enter=pending.enter,
                result=pending.result,
            )
            self._record_event(
                "handoff_retry",
                item.stage.value,
                self._item_key(item),
            )
            self._progress = True

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
                item = self._claim_item(stage_name)
                if item is None:  # pragma: no cover - len/claim are coordinator-thread atomic
                    continue
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
        # A timer is outside every stage queue.  Release a normal drain's
        # source lease before parking so its capacity is not stranded while
        # preserving the timer's single owner for the work item.
        self._release_source_lease(item)
        wake = time.monotonic() + max(0.0, delay_s)
        heapq.heappush(self.timers, (wake, self._seq, item))
        self._seq += 1
        item.add_history_event(item.stage, item.state, note=f"timer-parked {delay_s:.1f}s")
        self._record_event("timer_park", item.stage.value, self._item_key(item), delay_s)

    def _wake_timers(self) -> None:
        """Move expired timer entries back only after their stage accepts them.

        The timer heap owns an item until its stage queue accepts it.  An
        expired entry whose stage is at capacity remains at the heap head for a
        later tick; it is ordinary bounded backpressure, not a pipeline fault.
        """
        now = time.monotonic()
        while self.timers and self.timers[0][0] <= now:
            _, _, item = self.timers[0]
            if not self._push_item(item, item.stage, enter=False, defer_if_full=True):
                return
            heapq.heappop(self.timers)
            self._progress = True

    # -- completions ----------------------------------------------------------

    def _drain_completions(self) -> None:
        """Drain ALL ready completions without blocking."""
        # Clear before inspection.  A callback that publishes between this
        # clear and the final get_nowait() either has its result drained now
        # (leaving a harmless set latch) or sets the latch for the next wait.
        self._completion_wakeup.clear()
        while True:
            try:
                handle, result = self.completion_q.get_nowait()
            except queue_mod.Empty:
                break
            self._handle_completion(handle, result)

        if self._completion_saturation.is_set():
            # This cannot occur when the C-in-flight invariant holds: each
            # active job has one reserved slot in the C-sized completion
            # queue.  A WorkerPool callback never blocks or spills when that
            # invariant is violated; fail the run and finalize its still-live
            # item resumably instead.  Crucially, this is not a signal.
            self._record_event("completion_saturation")
            raise RuntimeError("completion queue saturated")

    def _wait_for_completion(self, timeout: float) -> None:
        """Wait for a completion, a saturation fault, or a signal wake latch."""
        # A test double may synchronously enqueue without owning the notifier;
        # avoid an unnecessary idle wait in that compatible case.  Production
        # callbacks set the event after every accepted completion.
        if self.completion_q.empty() and not self._completion_saturation.is_set():
            self._completion_wakeup.wait(timeout=timeout)
        self._drain_completions()

    def _handle_completion(self, handle: JobHandle, result: JobResult) -> None:
        """Route one completed job back to its item.

        Interrupted results park the item RESUMABLE — they never advance and
        never reach ``on_job_done`` (base-protocol contract).
        """
        self._progress = True
        item = self.in_flight.pop(handle, None)
        self._inflight_implementation_claims.pop(handle, None)
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
        if self._metrics_registry is not None:
            outcome = "interrupted" if result.interrupted else ("ok" if result.ok else "failed")
            self._metrics_registry.counter(
                "hephaestus_pipeline_jobs_total",
                "Completed pipeline jobs by stage and outcome.",
            ).inc(labels={"stage": item.stage.value, "outcome": outcome})
            if isinstance(handle.job, AgentJob):
                # Counter.inc rejects negative amounts; a monotonic-clock skew
                # could yield a tiny negative duration, so clamp defensively.
                self._metrics_registry.counter(
                    "hephaestus_pipeline_agent_job_seconds_total",
                    "Cumulative agent job wall-clock seconds.",
                ).inc(max(result.duration_s, 0.0))

        if result.interrupted:
            self._park_resumable(item)
            return

        # Direct runners mint an opaque id on the first turn.  Keep it under
        # the logical role rather than a round number so the next reviewer or
        # writer turn resumes the same compacted conversation.
        if isinstance(handle.job, AgentJob) and result.ok and result.session_id:
            session_key = handle.job.session_agent or handle.job.agent
            item.session_ids[session_key] = result.session_id

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
        self._release_source_lease(item)
        item.result = ItemResult(
            passed=False,
            reason=f"resumable at {item.stage.value}",
            final_stage=item.stage,
        )
        self._record_terminal_result(item)
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
        # A transition that previously found a full target owns its source
        # lease.  Retry it before ordinary draining, then again after each
        # stage: draining a target can open exactly the slot it needs.
        self._drain_pending_handoffs()
        for stage_name in _DRAIN_ORDER:
            if self.shutdown.is_set():
                return
            if stage_name is StageName.IMPLEMENTATION:
                self._drain_implementation()
                self._drain_pending_handoffs()
                continue
            q = self.queues[stage_name]
            for _ in range(len(q)):
                if self.shutdown.is_set():
                    return
                item = self._claim_item(stage_name)
                if item is None:  # pragma: no cover - len/claim are coordinator-thread atomic
                    break
                if not self._admit(item):
                    self._restore_source_lease(item)
                    continue
                self._record_event("drain", stage_name.value, self._item_key(item))
                self._run_item(item)
            self._drain_pending_handoffs()

    def _externalize_repo_issue_source(self, item: WorkItem, source: RepoIssueSource) -> bool:
        """Retire setup work and enroll its cursor in the bounded fair registry.

        A repository cursor is not a pipeline work item: it carries no global
        permit and no REPO-stage lease once checkout/setup finishes.  Keeping
        those owners would both count as an extra live object and let the
        first repository hold the REPO queue while its entire issue backlog
        drains.  The registry holds at most C cursor objects and drains them
        in FIFO rotation instead.
        """
        if len(self._repo_issue_sources) >= _work_window(self.config):
            return False

        item.payload.pop("_repo_issue_source", None)
        self._repo_issue_sources.append(_ActiveRepoIssueSource(repo=item.repo, source=source))
        self._release_source_lease(item)
        self._release_work_permit(item)
        self._seen_item_ids.discard(id(item))
        with suppress(ValueError):  # provisional item is normally tracked
            self.items.remove(item)
        self._record_event("repo_source_activate", item.repo, len(self._repo_issue_sources))
        return True

    def _repo_source_slots_used(self) -> int:
        """Return active cursors plus every live REPO setup reservation.

        A repository setup item becomes a detached cursor after ``SOURCE``.
        Reserve that future cursor slot from its first queue admission through
        terminalization so a later setup item cannot strand at ``SOURCE``
        merely because earlier setups filled the registry first.
        """
        candidates: dict[int, WorkItem] = {}
        for queue in self.queues.values():
            for item in queue.snapshot():
                candidates[id(item)] = item
        for _wake, _sequence, item in self.timers:
            candidates[id(item)] = item
        for item in self.in_flight.values():
            candidates[id(item)] = item
        for lease in self._leases.values():
            candidates[id(lease.item)] = lease.item
        reservations = sum(
            item.kind is ItemKind.REPO and id(item) in self._live_work_permit_ids
            for item in candidates.values()
        )
        return len(self._repo_issue_sources) + reservations

    def _repo_source_can_admit(self) -> bool:
        """Return whether a detached repo cursor may classify and admit one issue."""
        return self.live_work_count < _work_window(self.config) and all(
            self.queues[stage].can_offer() for stage in _DIRECT_ISSUE_ENTRY_STAGES
        )

    def _drain_repo_issue_sources(self) -> None:
        """Run one round-robin admission attempt for every active repo cursor."""
        for _ in range(len(self._repo_issue_sources)):
            active = self._repo_issue_sources.popleft()
            if self._drain_repo_issue_source(active):
                self._repo_issue_sources.append(active)

    def _drain_repo_issue_source(  # noqa: C901 - source lifecycle is intentionally linear
        self, active: _ActiveRepoIssueSource
    ) -> bool:
        """Consume one detached repository cursor until one child is admitted.

        Exclusions (notably epics) need no child capacity and are consumed in
        order after their durable skip write.  An eligible pending row stays
        in ``source.pending`` until every possible entry queue and the global
        permit budget can accept it; no classified product is retained.

        Returns:
            ``True`` while this cursor remains active, otherwise ``False``
            after normal exhaustion or a recorded discovery failure.

        """
        repo = active.repo
        source = active.source
        ctx = self._ctx_for_repo(repo)
        while True:
            metadata = source.pending
            if metadata is None:
                try:
                    metadata = next(source.metadata)
                except StopIteration:
                    self._record_event("repo_source_complete", repo, source.seeded_count)
                    return False
                except Exception as exc:
                    logger.warning("repo:%s: discovery source failed: %s", repo, exc)
                    self._record_repo_source_failure(repo, f"discovery failed: {exc}")
                    return False

            try:
                number = int(metadata["number"])
                labels = list(metadata.get("labels") or [])
                title = str(metadata.get("title") or "")
            except (KeyError, TypeError, ValueError) as exc:
                self._record_repo_source_failure(
                    repo, f"discovery failed: malformed metadata: {exc}"
                )
                return False
            # Metadata epics need no expensive issue fetch.  The durable
            # label write is completed before source consumption advances.
            if is_epic(labels, title):
                try:
                    ctx.github.skip_epics({number: labels})
                except Exception as exc:
                    logger.warning(
                        "repo:%s: could not tag excluded epic #%d state:skip: %s",
                        repo,
                        number,
                        exc,
                    )
                    self._record_repo_source_failure(repo, f"epic skip tag failed: {exc}")
                    return False
                logger.info("repo:%s: #%d is an epic; tagged state:skip, excluded", repo, number)
                source.pending = None
                self._progress = True
                return True

            if not self._repo_source_can_admit():
                source.pending = metadata
                return True

            try:
                facts = _seeding.seed_issue_from_github(number, ctx.github)
                if STATE_PLAN_BLOCKED in facts.labels:
                    ctx.github.ensure_blocked_audit(number)
                entry = _seeding.seed_entry_from_facts(facts)
                scope_stages = self.config.scope.stages if self.config.scope is not None else None
                stage, reason, passed = self._scope_seed_decision(
                    number, entry.stage, entry.reason, scope_stages
                )
                entry = replace(entry, stage=stage, reason=reason, passed=passed)
            except Exception as exc:
                logger.warning("repo:%s: issue #%d classification failed: %s", repo, number, exc)
                self._record_repo_source_failure(repo, f"discovery failed: {exc}")
                return False

            if entry.stage is None:
                try:
                    if entry.skip_tag_obligation is not None:
                        ctx.github.skip_epics({entry.skip_tag_obligation.issue: []})
                except Exception as exc:
                    logger.warning(
                        "repo:%s: could not tag excluded epic #%d state:skip: %s",
                        repo,
                        number,
                        exc,
                    )
                    self._record_repo_source_failure(repo, f"epic skip tag failed: {exc}")
                    return False
                logger.info("[%s] excluded: %s", repo, entry.reason)
                source.pending = None
                self._progress = True
                return True

            new_item = self._entry_to_item(entry, repo)
            if new_item.stage is StageName.FINISHED and new_item.result is None:
                new_item.result = ItemResult(
                    passed=entry.passed,
                    reason=entry.reason,
                    final_stage=StageName.FINISHED,
                )
            elif new_item.stage is not StageName.REPO:
                self._pass_work_count += 1

            source.pending = None
            if self._push_item(new_item, new_item.stage, enter=True):
                source.seeded_count += 1
                self._progress = True
                return True
            # The preflight covers capacity; a false push is therefore only
            # the existing idempotent duplicate guard.  Do not retain it in
            # a second source buffer.  The duplicate's live peer now governs
            # the next admission opportunity.
            self._progress = True
            return True

    def _record_repo_source_failure(self, repo: str, reason: str) -> None:
        """Retain a bounded terminal failure after a detached cursor aborts."""
        item = WorkItem(repo=repo, kind=ItemKind.REPO, stage=StageName.FINISHED)
        item.result = ItemResult(passed=False, reason=reason, final_stage=StageName.REPO)
        item.payload["entry_stage"] = StageName.REPO.value
        self.items.append(item)
        self._record_terminal_result(item)

    def _drain_implementation(self) -> None:
        """Drain the implementation queue under topo order + file-overlap gating.

        REUSES :func:`admission.order_for_implementation` (dependencies
        first) and :func:`admission._select_non_overlapping` (defer plans
        touching the same files while a peer is queued or already executing,
        #1623/#2451 — only engaged when real parallelism is possible,
        mirroring the legacy ``serialize_file_overlap`` gate).

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
        # Derive topology from a bounded snapshot, then lease only selected
        # items. A raw pop makes an item ownerless; each lease preserves its
        # original FIFO ticket while other ready work may still dispatch.
        for duplicate in self._implementation_duplicates(q.snapshot()):
            if not self._claim_selected_implementation_item(duplicate):
                return
            logger.warning(
                "implementation %s#%s already queued; dropping duplicate work item",
                duplicate.repo,
                duplicate.issue,
            )
            self._finish(
                duplicate,
                passed=True,
                reason=f"{duplicate.repo}#{duplicate.issue} superseded by queued duplicate",
            )
            if id(duplicate) in self._leases:
                return

        items = q.snapshot()
        # A snapshot belongs only to the drain that selected it.  Anything
        # still queued after an admission/capacity deferral is re-evaluated on
        # its next drain, rather than carrying an unsubmitted old reservation.
        for queued_item in items:
            queued_item.payload.pop(_IMPLEMENTATION_FILE_CLAIMS_PAYLOAD, None)
        dispatch_items, submission_claims = self._select_implementation_dispatch(items)
        for item in dispatch_items:
            if self.shutdown.is_set() or not self._admit(item):
                continue
            if not self._claim_selected_implementation_item(item):
                continue
            # Preserve the exact immutable snapshot used by the overlap gate.
            # A plan comment can change between admission and submission, but
            # it must not change the reservation that made this item eligible.
            if (claims := submission_claims.get(id(item))) is not None:
                item.payload[_IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = set(claims)
            self._record_event("drain", StageName.IMPLEMENTATION.value, self._item_key(item))
            self._run_item(item)

    def _select_implementation_dispatch(
        self, items: list[WorkItem]
    ) -> tuple[list[WorkItem], dict[int, set[_admission.PlanFileClaim]]]:
        """Order queued work and reserve immutable snapshots for this drain."""
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
        selected_claims: dict[int, set[_admission.PlanFileClaim]] = {}
        if self.config.serialize_file_overlap and self.config.max_workers > 1 and ordered:
            # Resolve each issue's owning repo from its own WorkItem: the queue is
            # keyed by stage, so one round can hold issues from several repos (#1795).
            repo_of = {
                number: (self.config.org, item.repo)
                for number, item in issue_items.items()
                if item.repo
            }
            inflight_claims = self._active_implementation_file_claims()
            selection_kwargs: dict[str, Any] = {
                "repo_of": repo_of,
                "selected_claims": selected_claims,
            }
            if inflight_claims:
                selection_kwargs["initial_claims"] = inflight_claims
            dispatch, deferred = _admission._select_non_overlapping(ordered, **selection_kwargs)
            for number in deferred:
                logger.info("implementation #%s deferred (file overlap)", number)
        dispatch_items = [issue_items[number] for number in dispatch]
        submission_claims = {
            id(issue_items[number]): set(claims) for number, claims in selected_claims.items()
        }
        # Cross-repo same-number items bypass only the number-keyed dependency
        # ordering.  They still need their own repo-scoped overlap admission;
        # otherwise a same-number collision could evade active reservations.
        if self.config.serialize_file_overlap and self.config.max_workers > 1:
            claimed = self._active_implementation_file_claims()
            for claims in selected_claims.values():
                claimed.update(claims)
            ambiguous_items, ambiguous_claims = self._select_ambiguous_implementation_items(
                ambiguous, claimed
            )
            dispatch_items.extend(ambiguous_items)
            submission_claims.update(ambiguous_claims)
        else:
            # Cross-repo same-number items are distinct work even though the
            # shared issue-number dependency model cannot rank them (#2057).
            dispatch_items.extend(it for group in ambiguous.values() for it in group)
        return dispatch_items, submission_claims

    @staticmethod
    def _implementation_duplicates(items: list[WorkItem]) -> list[WorkItem]:
        """Return non-first queued duplicates keyed by ``(repo, issue)`` (#2057)."""
        seen: set[tuple[str, int]] = set()
        duplicates: list[WorkItem] = []
        for item in items:
            if item.issue is None:
                continue
            key = (item.repo, item.issue)
            if key in seen:
                duplicates.append(item)
            else:
                seen.add(key)
        return duplicates

    def _active_implementation_file_claims(self) -> set[_admission.PlanFileClaim]:
        """Return the immutable plan claims held by active implementation jobs."""
        claims: set[_admission.PlanFileClaim] = set()
        for active_claims in self._inflight_implementation_claims.values():
            claims.update(active_claims)
        return claims

    def _select_ambiguous_implementation_items(
        self,
        ambiguous: dict[int, list[WorkItem]],
        claimed: set[_admission.PlanFileClaim],
    ) -> tuple[list[WorkItem], dict[int, set[_admission.PlanFileClaim]]]:
        """Apply file-overlap admission to cross-repo same-number items.

        Dependency order cannot represent these items under its number-only
        keys, but file paths are repository-scoped and must still honour both
        active reservations and earlier selections from this drain.
        """
        dispatch: list[WorkItem] = []
        snapshots: dict[int, set[_admission.PlanFileClaim]] = {}
        for group in ambiguous.values():
            for item in group:
                if item.issue is None:  # defensive: index construction excludes this case
                    continue
                repo = (self.config.org, item.repo)
                planned = _admission._fetch_planned_files(item.issue, repo=repo)
                item_claims = {(repo, path) for path in planned} if planned else set()
                if item_claims and (item_claims & claimed):
                    logger.info(
                        "implementation %s#%s deferred (file overlap)", item.repo, item.issue
                    )
                    continue
                claimed.update(item_claims)
                # Preserve an empty snapshot too: unknown plans fail open,
                # but must not be fetched again after being admitted.
                snapshots[id(item)] = set(item_claims)
                dispatch.append(item)
        return dispatch, snapshots

    def _capture_implementation_file_claims(self, item: WorkItem) -> set[_admission.PlanFileClaim]:
        """Return the immutable claims reserved for an implementation submission.

        The parallel admission gate places its exact selection snapshot in the
        host-owned payload.  Serial and overlap-opt-out submissions retain the
        established one-time, fail-open lookup behavior.
        """
        if item.stage is not StageName.IMPLEMENTATION or item.issue is None:
            return set()
        selected = item.payload.pop(_IMPLEMENTATION_FILE_CLAIMS_PAYLOAD, None)
        if selected is not None:
            return set(selected)
        repo = (self.config.org, item.repo)
        planned = _admission._fetch_planned_files(item.issue, repo=repo)
        return {(repo, path) for path in planned} if planned else set()

    def _claim_selected_implementation_item(self, item: WorkItem) -> bool:
        """Claim *item* at its current position, preserving FIFO retry order."""
        for index, queued in enumerate(self.queues[StageName.IMPLEMENTATION].snapshot()):
            if queued is item:
                claimed = self._claim_item(StageName.IMPLEMENTATION, index=index)
                if claimed is not item:  # pragma: no cover - coordinator-thread invariant
                    raise RuntimeError("implementation queue selected a different item")
                return True
        return False

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
        return len(self.in_flight) < _work_window(self.config) and self.inflight_per_repo[
            item.repo
        ] < max(1, self.config.max_workers)

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
                    if (
                        item.kind is ItemKind.REPO
                        and item.state == "SOURCE"
                        and isinstance(item.payload.get("_repo_issue_source"), RepoIssueSource)
                    ):
                        source = item.payload["_repo_issue_source"]
                        # Setup work has completed. Move only its bounded
                        # cursor into the fair registry, releasing the REPO
                        # lease and provisional permit so one repository
                        # cannot monopolize the stage while it streams.
                        if not self._externalize_repo_issue_source(item, source):
                            # Admission reserves a registry slot before a
                            # setup item enters REPO. This fallback preserves
                            # liveness for an injected/custom queue that
                            # violates that invariant: a bounded timer owns
                            # the item until a cursor slot becomes free.
                            logger.debug("repo:%s: waiting for a source-registry slot", item.repo)
                            self._timer_park(item, _SOURCE_REGISTRY_RETRY_DELAY_S)
                        return
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
        implementation_claims = self._capture_implementation_file_claims(item)
        handle = self.pool.submit(
            job,
            request.on_done_state,
            claim_key=self._item_key(item),
            claim_stage=item.stage.value,
        )
        self.in_flight[handle] = item
        if implementation_claims:
            self._inflight_implementation_claims[handle] = implementation_claims
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
        if item.stage is StageName.REPO and item.payload.get(DIRECT_SCOPE_BOOTSTRAP_KEY, False):
            self._route_direct_scope_bootstrap(item, outcome)
            return
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
            self._record_terminal_result(item)
            self._release_source_lease(item)
            self._release_work_permit(item)
            return

        if disposition is Disposition.ADVANCE:
            self._seed_products(item)
            target = route.next
            if target is StageName.FINISHED:
                self._finish(item, passed=True, reason=outcome.note or "advance")
            else:
                self._handoff_item(item, target, enter=True)
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

    def _route_direct_scope_bootstrap(self, item: WorkItem, outcome: StageOutcome) -> None:
        """Activate direct cursors only after their checkout proof succeeds.

        A partial pipeline scope deliberately omits ``REPO`` from its route
        table.  This internal setup item therefore has its own tiny terminal
        protocol instead of widening the operator-selected stage scope.  It
        never reaches an agent-capable stage unless the repo stage completed
        clone-plus-sync and then prepared the label vocabulary.
        """
        if outcome.disposition is Disposition.RETRY:
            self._route_retry(item, outcome)
            return
        if outcome.disposition is not Disposition.FINISH_PASS:
            self._direct_scope_bootstrap_pending = False
            self._finish(
                item,
                passed=False,
                reason=outcome.note or "direct scope checkout preparation failed",
            )
            return

        base_sha = item.payload.get(DIRECT_SCOPE_BASE_SHA_KEY)
        if not is_full_commit_sha(base_sha):
            if not self.config.dry_run:
                self._direct_scope_bootstrap_pending = False
                self._finish(
                    item,
                    passed=False,
                    reason="direct scope checkout returned an invalid default-branch SHA",
                )
                return
            # Dry-run never submits checkout or implementation jobs, so it
            # cannot truthfully obtain an immutable checkout SHA. Do not
            # attach a fake pin: direct items retain normal preview behavior
            # while real runs still fail closed without the proof.
            base_sha = ""
        try:
            self._begin_direct_pr_source(item.repo, base_sha)
            self._begin_direct_issue_source(item.repo, base_sha)
        except Exception as exc:
            self._direct_scope_bootstrap_pending = False
            logger.warning(
                "direct scope for %s could not initialize after sync: %s", item.repo, exc
            )
            self._finish(item, passed=False, reason=f"direct scope initialization failed: {exc}")
            return

        # A successful bootstrap is coordinator plumbing, not an issue/repo
        # result.  Free its sole bounded slot before admitting the first
        # direct entry, mirroring successful REPO-source externalization.
        self._release_source_lease(item)
        self._release_work_permit(item)
        self._direct_scope_bootstrap_pending = False
        self._seen_item_ids.discard(id(item))
        with suppress(ValueError):
            self.items.remove(item)
        self._record_event("direct_scope_ready", item.repo)
        self._drain_direct_pr_source()
        self._drain_direct_issue_source()

    def _route_retry(self, item: WorkItem, outcome: StageOutcome) -> None:
        """Apply the RETRY row: heap-park on a recorded delay, else next tick.

        RETRY timer contract (base.py): the stage records its backoff in
        ``payload["retry_delay_s"]`` immediately before returning; a missing
        key means "retry on the next drain tick". Under dry-run a DELAYED
        retry waits on real-world progress (for example, PR merges) the preview
        will never make, so the item finishes instead of stalling the heap.
        """
        delay = item.payload.pop("retry_delay_s", None)
        if delay is None:
            if not self._restore_source_lease(item):
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
            self._handoff_item(item, target, enter=True)

    def _finish(self, item: WorkItem, *, passed: bool, reason: str) -> None:
        """Set the item's result and hand it to the finished sink."""
        if item.stage is StageName.REPO and item.payload.get(DIRECT_SCOPE_BOOTSTRAP_KEY, False):
            self._direct_scope_bootstrap_pending = False
        if item.stage is StageName.FINISHED:
            # Poisoned inside the sink: record directly, never re-queue.
            item.result = ItemResult(passed=passed, reason=reason, final_stage=item.stage)
            if not item.payload.get("_recorded", False):
                self.ledger.append(item.result)
                item.payload["_recorded"] = True
            self._record_terminal_result(item)
            self._release_source_lease(item)
            self._release_work_permit(item)
            return
        result = ItemResult(passed=passed, reason=reason, final_stage=item.stage)
        self._handoff_item(item, StageName.FINISHED, enter=True, result=result)

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
        for lease in self._leases.values():
            it = lease.item
            if it.kind is ItemKind.ISSUE and it.issue is not None:
                keys.add((it.repo, it.issue))
        return keys

    def _push_item(
        self,
        item: WorkItem,
        stage: StageName,
        enter: bool,
        *,
        defer_if_full: bool = False,
    ) -> bool:
        """Push *item* into *stage*'s queue (the single push chokepoint).

        Every durable GitHub mutation for this transition already happened
        inside the stage, immediately before the outcome that got us here.

        Upstream idempotency guard (#2107): a genuinely NEW ISSUE work item whose
        ``(repo, issue)`` is already queued (any stage) or in-flight is refused —
        it never enters the pipeline, so the drain-level dedup (#2058) is not even
        exercised. Object identity (``_seen_item_ids``) distinguishes a new item
        from an already-tracked item re-pushing itself on timer/retry/fail-back/
        advance, which must always be allowed through.

        ``defer_if_full`` makes bounded capacity an ordinary backpressure
        result rather than an exception. It is reserved for timer wake-ups,
        whose heap entry must remain the item's owner until the stage accepts
        it. All ordinary callers retain the strict overflow invariant.

        Returns:
            ``True`` when the queue accepted the item; ``False`` when a
            duplicate seed was skipped or a deferred queue is full.

        """
        is_new_item = id(item) not in self._seen_item_ids
        if (
            is_new_item
            and item.kind is ItemKind.ISSUE
            and item.issue is not None
            and (item.repo, item.issue) in self._live_issue_keys()
        ):
            logger.info("seed skipped: #%s already queued/in-flight in %s", item.issue, item.repo)
            return False
        if is_new_item and not self._try_acquire_work_permit(item):
            logger.debug(
                "global live-work capacity reached (%d); deferring %s",
                _work_window(self.config),
                self._item_key(item),
            )
            return False
        acquired_permit = is_new_item
        if not self.queues[stage].offer(item):
            if acquired_permit:
                self._release_work_permit(item)
            if defer_if_full:
                return False
            raise OverflowError("StageQueue is full")
        item.stage = stage
        if enter:
            item.state = "ENTER"
            item.payload["_enter_pending"] = True
            item.add_history_event(stage, item.state, note="enqueued")
        if is_new_item:
            self._seen_item_ids.add(id(item))
            self.items.append(item)
            item.payload.setdefault("entry_stage", stage.value)
        self._record_event("push", stage.value, self._item_key(item))
        return True

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
        # A reseed pass creates fresh WorkItems for the same logical issues.
        # Keep final status semantics aligned with _effective_items(): the
        # latest pass supersedes earlier attempts without retaining an
        # unbounded logical-identity map in the terminal aggregate.
        self._terminal_summary.reset()
        self._pass_work_count = 0
        has_direct_scope = bool(self.config.issues or self.config.prs)
        discovery_repos = [] if has_direct_scope else self.config.repos
        # Repository discovery is a source, not a list of pre-built
        # ``SeedEntry``/``WorkItem`` values.  Keep the legacy empty call so
        # direct test seams and any non-repository synthetic entries retain
        # their established contract; production returns no entries here.
        entries = _seeding.seed_from_cli([], [], [])
        self._begin_repo_entry_source(discovery_repos if not entries else [])
        default_repo = self.config.repos[0] if self.config.repos else ""
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
            if self._push_item(item, item.stage, enter=True):
                pushed += 1
        if has_direct_scope:
            pushed += self._begin_direct_scope_bootstrap(default_repo)
        else:
            # No explicit cursor exists in an unscoped pass. Keep the calls
            # for their established empty-source cleanup semantics without
            # manufacturing a checkout pin outside the direct bootstrap.
            self._begin_direct_pr_source(default_repo, "")
            self._begin_direct_issue_source(default_repo, "")
        return (
            pushed
            + self._drain_repo_entry_source()
            + (0 if has_direct_scope else self._drain_direct_pr_source())
            + (0 if has_direct_scope else self._drain_direct_issue_source())
        )

    def _begin_direct_scope_bootstrap(self, repo: str) -> int:
        """Enqueue the one checkout proof required by an explicit CLI scope."""
        self._direct_scope_bootstrap_pending = True
        if not repo:
            self._direct_scope_bootstrap_pending = False
            item = WorkItem(repo="", kind=ItemKind.REPO, stage=StageName.FINISHED)
            item.result = ItemResult(
                passed=False,
                reason="explicit --issues/--prs scope requires exactly one repository",
                final_stage=StageName.FINISHED,
            )
            return int(self._push_item(item, StageName.FINISHED, enter=True))
        item = WorkItem(repo=repo, kind=ItemKind.REPO, stage=StageName.REPO)
        item.payload[DIRECT_SCOPE_BOOTSTRAP_KEY] = True
        return int(self._push_item(item, StageName.REPO, enter=True))

    def _begin_repo_entry_source(self, repos: list[str]) -> None:
        """Initialize one FIFO source for this pass's repository discovery.

        Strict source-order fairness is intentional: an active repository
        holds its REPO-stage lease until its bounded issue cursor finishes, so
        the next repository is admitted only after that stage slot is free.
        This prevents both an unbounded source spill and starvation from
        repeatedly retrying a later repository ahead of an earlier one.
        """
        if self.config.repo_source_factory is not None:
            self._repo_entry_source = _RepoEntrySource(repos=self.config.repo_source_factory())
        elif repos:
            self._repo_entry_source = _RepoEntrySource(repos=iter(repos))
        else:
            self._repo_entry_source = None

    def _drain_repo_entry_source(self) -> int:
        """Admit repository discovery items from the FIFO source when safe."""
        source = self._repo_entry_source
        if source is None:
            return 0

        pushed = 0
        while (
            self.live_work_count < _work_window(self.config)
            and self.queues[StageName.REPO].can_offer()
            and self._repo_source_slots_used() < _work_window(self.config)
        ):
            repo = source.pending
            if repo is None:
                try:
                    repo = next(source.repos)
                except StopIteration:
                    self._repo_entry_source = None
                    break
                except Exception as exc:
                    raise RuntimeError(f"repository discovery source failed: {exc}") from exc
            item = WorkItem(repo=repo, kind=ItemKind.REPO, stage=StageName.REPO)
            if not self._push_item(item, StageName.REPO, enter=True):
                # The coordinator is single-threaded and the predicate above
                # reserves both capacities. Retain exactly one retry value if
                # an injected/custom queue declines unexpectedly.
                source.pending = repo
                break
            source.pending = None
            pushed += 1
        return pushed

    def _begin_direct_issue_source(self, repo: str, base_sha: str) -> None:
        """Initialize one bounded cursor for this pass's ``--issues`` input."""
        self._direct_issue_source = None
        if not self.config.issues:
            return
        open_issues = _admission._filter_open_issues(repo, self.config.issues)
        self._direct_issue_source = _DirectIssueSource(
            repo=repo,
            issues=iter(open_issues),
            base_sha=base_sha,
        )

    def _begin_direct_pr_source(self, repo: str, base_sha: str) -> None:
        """Initialize one bounded cursor for this pass's ``--prs`` input."""
        self._direct_pr_source = None
        if self.config.prs:
            self._direct_pr_source = _DirectPrSource(
                repo=repo,
                prs=(
                    pr for pr in self.config.prs if (repo, pr) not in self._direct_pr_handoff_keys
                ),
                base_sha=base_sha,
            )

    def _direct_issue_queues_can_accept(self) -> bool:
        """Return whether one direct issue can be classified and enqueued now."""
        return self.live_work_count < _work_window(self.config) and all(
            self.queues[stage].can_offer() for stage in _DIRECT_ISSUE_ENTRY_STAGES
        )

    def _drain_direct_issue_source(self) -> int:
        """Pull explicit issues directly into queues without a seed spill buffer.

        An issue is classified only after its eventual destination is
        guaranteed to have capacity.  The loop fills immediately available
        capacity, then leaves the remaining caller-owned iterator live until
        normal queue draining creates another safe admission point.
        """
        source = self._direct_issue_source
        if source is None:
            return 0

        pushed = 0
        while self._direct_issue_queues_can_accept():
            try:
                issue = next(source.issues)
            except StopIteration:
                self._direct_issue_source = None
                break

            entry = self._seed_direct_issue_entry(source.repo, issue)
            if entry.stage is None:
                # Epic tagging is the one sanctioned seeding write.  Complete
                # it before honoring the source exclusion, exactly as the
                # ordinary seed path does.
                if entry.skip_tag_obligation is not None:
                    self.github.skip_epics({entry.skip_tag_obligation.issue: []})
                logger.info("seed excluded: %s", entry.reason)
                continue

            item = self._entry_to_item(entry, source.repo)
            if is_full_commit_sha(source.base_sha):
                item.payload[DIRECT_SCOPE_BASE_SHA_KEY] = source.base_sha
            if item.stage not in (StageName.REPO, StageName.FINISHED):
                self._pass_work_count += 1
            if item.stage is StageName.FINISHED and item.result is None:
                item.result = ItemResult(
                    passed=entry.passed,
                    reason=entry.reason,
                    final_stage=StageName.FINISHED,
                )
            # ``_direct_issue_queues_can_accept`` covers every possible
            # classifier result and the coordinator-wide permit budget. This
            # source owns the coordinator thread, so a failed lifecycle push
            # can only be an idempotent duplicate; it never represents
            # saturation or a completion/shutdown fault.
            if self._push_item(item, item.stage, enter=True):
                pushed += 1
        return pushed

    def _drain_direct_pr_source(self) -> int:
        """Pull explicit PRs into queues without an eager seed-entry spill.

        Explicit PRs retain their historical precedence over explicit issues
        when both flags are supplied.  As a source advances only after the
        prior PR leaves the global live-work window, that precedence cannot
        discard later PRs on a saturated queue.
        """
        source = self._direct_pr_source
        if source is None:
            return 0

        pushed = 0
        while self._direct_issue_queues_can_accept():
            try:
                pr = next(source.prs)
            except StopIteration:
                self._direct_pr_source = None
                break

            # The compatibility helper returns exactly one entry here; unlike
            # the legacy call over ``config.prs`` it cannot retain a source
            # sized list while waiting for capacity.
            entry = self._seed_direct_pr_scope(source.repo, (pr,))[0]
            if entry.stage is None:
                logger.info("seed excluded: %s", entry.reason)
                continue

            item = self._entry_to_item(entry, source.repo)
            if is_full_commit_sha(source.base_sha):
                item.payload[DIRECT_SCOPE_BASE_SHA_KEY] = source.base_sha
            if item.stage not in (StageName.REPO, StageName.FINISHED):
                self._pass_work_count += 1
            if item.stage is StageName.FINISHED and item.result is None:
                item.result = ItemResult(
                    passed=entry.passed,
                    reason=entry.reason,
                    final_stage=StageName.FINISHED,
                )
            # The common direct-entry admission predicate reserves every
            # possible classifier target and the global permit. A failed push
            # is therefore only an idempotent duplicate, not saturation.
            if self._push_item(item, item.stage, enter=True):
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
        """Classify direct entries for legacy direct-classifier callers.

        Runtime seeding never materializes the explicit issue scope here;
        :meth:`_drain_direct_issue_source` owns its bounded cursor.  This
        compatibility helper remains for direct classifier tests.
        """
        entries: list[_seeding.SeedEntry] = []
        issue_numbers = _admission._filter_open_issues(repo, self.config.issues)
        for issue in issue_numbers:
            entries.append(self._seed_direct_issue_entry(repo, issue))
        entries.extend(self._seed_direct_pr_scope(repo))
        return entries

    def _seed_direct_issue_entry(self, repo: str, issue: int) -> _seeding.SeedEntry:
        """Classify one direct issue through its target repository accessor."""
        github = self._ctx_for_repo(repo).github if repo else self.github
        scope_stages = self.config.scope.stages if self.config.scope is not None else None
        facts = _seeding.seed_issue_from_github(issue, github)
        if STATE_PLAN_BLOCKED in facts.labels:
            github.ensure_blocked_audit(issue)
        entry = _seeding.seed_entry_from_facts(facts)
        stage, reason, passed = self._scope_seed_decision(
            issue, entry.stage, entry.reason, scope_stages
        )
        return replace(entry, stage=stage, reason=reason, passed=passed)

    def _seed_direct_pr_scope(
        self, repo: str, prs: Iterable[int] | None = None
    ) -> list[_seeding.SeedEntry]:
        """Classify explicit PRs for compatibility callers or a one-PR source pull."""
        github = self._ctx_for_repo(repo).github if repo else self.github
        entries: list[_seeding.SeedEntry] = []
        scope_stages = self.config.scope.stages if self.config.scope is not None else None
        for pr in self.config.prs if prs is None else prs:
            issue_number = github.find_issue_for_pr(pr)
            if issue_number is None:
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=StageName.FINISHED,
                        reason=(
                            f"PR #{pr} has no linked issue; refusing review without "
                            "requirements context"
                        ),
                        pr_number=pr,
                        passed=False,
                    )
                )
                continue
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
                issue_facts: _seeding.IssueFacts | None
                review_context: dict[str, str] | None
                try:
                    issue_facts = _seeding.seed_issue_from_github(issue_number, github)
                    review_context = github.pr_review_context(pr)
                except Exception as exc:
                    logger.warning("PR #%d: review context read failed: %s", pr, exc)
                    review_context = None
                    issue_facts = None
                if issue_facts is None or review_context is None:
                    entries.append(
                        _seeding.SeedEntry(
                            kind="pr",
                            identifier=pr,
                            stage=StageName.FINISHED,
                            reason=f"PR #{pr} review context could not be read",
                            pr_number=pr,
                            issue_number=issue_number,
                            passed=False,
                        )
                    )
                    continue
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
                        issue_title=issue_facts.title,
                        issue_body=issue_facts.body,
                        pr_description=review_context["pr_description"],
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
                # Only a resolved, linked issue supplies requirements context.
                # A PR number is not a safe substitute: its author controls
                # the PR body, so an unlinked direct PR must remain orphaned
                # and fail closed before PR review.
                issue=entry.issue_number,
                pr=entry.pr_number or int(entry.identifier),
                stage=entry.stage,
            )
            item.payload["issue_title"] = entry.issue_title
            item.payload["issue_body"] = entry.issue_body
            item.payload["pr_description"] = entry.pr_description
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
            # A scoped issue with an open PR enters PR_REVIEW as an issue
            # work item. Preserve that provenance so the stage adopts a
            # dedicated PR checkout rather than falling back to the shared
            # repository root.
            if entry.stage is StageName.PR_REVIEW and entry.pr_number is not None:
                item.payload["existing_pr"] = True
        item.state = "ENTER"
        item.payload["entry_reason"] = entry.reason
        return item

    def _has_pending_seed_source(self) -> bool:
        """Return whether this pass still owns an unclassified input cursor.

        A source may have admitted no issue yet, so ``_pass_work_count`` alone
        cannot distinguish a converged pass from one awaiting its first safe
        queue slot.  Sources themselves remain bounded: one direct cursor,
        one repository-entry cursor, and at most C active repo cursors.
        """
        return any(
            (
                self._repo_entry_source is not None,
                bool(self._repo_issue_sources),
                self._direct_issue_source is not None,
                self._direct_pr_source is not None,
                self._direct_scope_bootstrap_pending,
            )
        )

    def _only_direct_pr_handoffs_remain(self) -> bool:
        """Return whether every explicitly scoped PR needs an external human action.

        This is deliberately limited to ``--prs`` without ``--issues`` and
        only lasts for the current coordinator.  A later invocation must
        query GitHub again because a reviewer may have resolved a thread or
        changed the PR head in the meantime.
        """
        if self.config.issues or not self.config.prs or not self.config.repos:
            return False
        repo = self.config.repos[0]
        return all((repo, pr) in self._direct_pr_handoff_keys for pr in self.config.prs)

    def _reseed_if_converged(self) -> bool:
        """Re-seed after full drain; False = stop (loops or zero-work exit).

        Mirrors the legacy zero-work early-exit (loop_runner
        ``_CONVERGENCE_PHASES``): when the just-finished pass produced zero
        actionable (non-repo, non-finished) work, the run converged — exit
        even if ``--loops`` remain.
        """
        if self._only_direct_pr_handoffs_remain():
            logger.info("direct PR handoff convergence: all scoped PRs require human action")
            return False
        if self._loops_run >= self.config.loops:
            logger.info("loop budget exhausted (%d/%d)", self._loops_run, self.config.loops)
            return False
        if self._pass_work_count == 0 and not self._has_pending_seed_source():
            logger.info(
                "zero-work convergence: pass %d produced no actionable items; exiting early",
                self._loops_run,
            )
            return False
        self._loops_run += 1
        logger.info("re-seeding: loop %d/%d", self._loops_run, self.config.loops)
        self._seed_pass()
        return self._pass_work_count > 0 or self._has_pending_seed_source()

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
            # ``self.shutdown`` belongs to the coordinator's signal path. A
            # normal ``finally`` must reap worker resources without changing
            # its exit outcome to an interruption (#2431), while a genuine
            # signal preserves the pool's direct cancellation semantics.
            self.pool.shutdown(mark_interrupted=self.shutdown.is_set())
        except Exception:  # pragma: no cover - defensive
            logger.exception("pool shutdown raised")
        for item in list(self.in_flight.values()):
            self._park_resumable(item)
        self.in_flight.clear()
        self._inflight_implementation_claims.clear()
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
        leftovers.extend(lease.item for lease in self._leases.values())
        seen: set[int] = set()
        for item in leftovers:
            if id(item) in seen:
                continue
            seen.add(id(item))
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
    _preflight_prompt_catalog()

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
