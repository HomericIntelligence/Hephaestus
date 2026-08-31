"""Single-threaded event-loop coordinator for the queue-based pipeline (epic #1809).

## Semantics

The coordinator main thread owns all eight queues, the timer heap, in-flight
jobs, routing, and (through the
:class:`~hephaestus.automation.pipeline_github.PipelineGitHub` accessor) every
GitHub API mutation. The main pool runs agent, test, Git, and network jobs. The
auxiliary pool runs learning and terminal cleanup. Their bounded completion
queues are the only cross-thread channels. An event latch wakes the loop for
accepted completions and signals; callbacks and signal handlers never block on full queues.

Per tick (epic #1809 "Coordinator event loop"):

1. shutdown check (graceful drain, or immediate teardown after the grace
   window / a second signal);
2. wake expired timers (heapq) back into their stage queues;
3. drain ALL ready completions — interrupted results park the item
   RESUMABLE and never advance (``on_job_done`` is never called for them);
4. drain queues DOWNSTREAM-FIRST (finished → learning → merge_wait → pr_review
   → implementation → plan_review → planning → repo; finish work before
   admitting new) with admission control;
5. fully drained: re-seed discovery up to ``--loops``; explicit
   ``--issues``/``--prs`` selections drain once; otherwise block or converge.

``_run_item`` drives ``on_enter``/``step`` until a ``JobRequest`` (park +
submit) or a ``StageOutcome`` (route via ROUTES). Per-item ``try/except``: a
poisoned item routes to finished(fail) and never kills the loop.

Admission control: per-repo in-flight cap (= ``max_workers``), and the
implementation queue is additionally gated by dependency topological order
(:func:`~.admission.order_for_implementation`) and file-overlap serialization
through the coordinator's shared repo-scoped admission pass. Pool size =
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

from collections import Counter as Counter, OrderedDict as OrderedDict, deque as deque
from collections.abc import Callable as Callable, Iterable as Iterable, Iterator as Iterator
from contextlib import suppress as suppress
from dataclasses import dataclass as dataclass, field as field, replace as replace
from pathlib import Path as Path
from typing import Any as Any

from jinja2 import TemplateNotFound as TemplateNotFound

import hephaestus.automation.pipeline.admission as _admission
from hephaestus.automation.direct_review_recovery import (
    is_inspection_only_detached_push_failure as is_inspection_only_detached_push_failure,
    list_direct_review_recovery_paths as list_direct_review_recovery_paths,
)
from hephaestus.automation.issue_waves import (
    WAVE_LEASE_PAYLOAD as WAVE_LEASE_PAYLOAD,
    WAVE_NON_CODE_PAYLOAD as WAVE_NON_CODE_PAYLOAD,
    IssueWaveError as IssueWaveError,
    IssueWaveStore as IssueWaveStore,
    WaveLease as WaveLease,
    wave_entry_from_facts as wave_entry_from_facts,
)
from hephaestus.automation.models import IssueInfo as IssueInfo
from hephaestus.automation.pipeline.athena_executor_scope import (
    pipeline_requires_athena_executor as pipeline_requires_athena_executor,
)
from hephaestus.automation.pipeline.coordinator_stage_config import (
    _StageRunConfig as _StageRunConfig,
)
from hephaestus.automation.pipeline.events import (
    StageEvent as StageEvent,
    encode_stage_event as encode_stage_event,
)
from hephaestus.automation.pipeline.jobs import (
    WORKTREE_MATERIALIZED_KEY as WORKTREE_MATERIALIZED_KEY,
    AgentJob as AgentJob,
    GitJob as GitJob,
    JobHandle as JobHandle,
    JobResult as JobResult,
)
from hephaestus.automation.pipeline.queues import (
    CompletionQueue as CompletionQueue,
    StageQueue as StageQueue,
    StageQueueLease as StageQueueLease,
)
from hephaestus.automation.pipeline.routing import (
    PIPELINE_ORDER as PIPELINE_ORDER,
    ROUTES as ROUTES,
    Disposition as Disposition,
    PipelineScope as PipelineScope,
    Route as Route,
    StageName as StageName,
    StageOutcome as StageOutcome,
)
from hephaestus.automation.pipeline.stages import (
    Continue as Continue,
    FinishedStage as FinishedStage,
    ImplementationStage as ImplementationStage,
    JobRequest as JobRequest,
    LearningStage as LearningStage,
    MergeWaitStage as MergeWaitStage,
    PlanningStage as PlanningStage,
    PlanReviewStage as PlanReviewStage,
    PrReviewStage as PrReviewStage,
    RepoStage as RepoStage,
    Stage as Stage,
    StageContext as StageContext,
    StageGitHub as StageGitHub,
)
from hephaestus.automation.pipeline.stages.base import (
    BranchWorktreeOwnerStatus as BranchWorktreeOwnerStatus,
)
from hephaestus.automation.pipeline.stages.implementation import (
    PRE_PR_TEST_ARGV as PRE_PR_TEST_ARGV,
)
from hephaestus.automation.pipeline.stages.repo import (
    DIRECT_SCOPE_BASE_SHA_KEY as DIRECT_SCOPE_BASE_SHA_KEY,
    DIRECT_SCOPE_BOOTSTRAP_KEY as DIRECT_SCOPE_BOOTSTRAP_KEY,
    DIRECT_SCOPE_WORKTREE_NONCE_KEY as DIRECT_SCOPE_WORKTREE_NONCE_KEY,
    RepoIssueSource as RepoIssueSource,
    is_full_commit_sha as is_full_commit_sha,
    product_to_work_item as product_to_work_item,
)
from hephaestus.automation.pipeline.summary import (
    RunStats as RunStats,
    TerminalSummary as TerminalSummary,
    latest_logical_items as latest_logical_items,
    print_summary as print_summary,
)
from hephaestus.automation.pipeline.work_item import (
    ItemKind as ItemKind,
    ItemResult as ItemResult,
    PreservedWorktree as PreservedWorktree,
    WorkItem as WorkItem,
)
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO as STATE_IMPLEMENTATION_GO,
    STATE_PLAN_BLOCKED as STATE_PLAN_BLOCKED,
    is_epic as is_epic,
)
from hephaestus.prompts import PromptCatalog as PromptCatalog

#: Warn when any stage.step() call exceeds this duration (seconds) — the
#: stage protocol promises short (<~60s) main-thread steps. 15s proved too
#: tight in practice: routine repo-stage steps (clone + label reads over the
#: network) breached it on nearly every multi-repo run, burying real stalls
#: in noise (#2648).
_STEP_WATCHDOG_S = 60.0

#: Grace period for graceful shutdown (drain in-flight jobs up to this long).
_DEFAULT_GRACE_S = 30.0

#: Coordinator idle poll interval while waiting for completions (seconds).
_IDLE_POLL_S = 1.0

#: Number of fully stalled idle ticks before the coordinator force-runs work.
_STALL_TICKS_BEFORE_FORCE = 3

# Host-owned work-item payload key for the file reservation that admitted a
# queued implementation item. The frozen plan starts the reservation; paths
# from the verified checkout diff extend it through review and merge wait.
_IMPLEMENTATION_FILE_CLAIMS_PAYLOAD = "_implementation_file_claims"

_FILE_CLAIM_STAGES = frozenset(
    {StageName.IMPLEMENTATION, StageName.PR_REVIEW, StageName.MERGE_WAIT}
)
_REALIZED_DIFF_CLAIM_STAGES = frozenset({StageName.PR_REVIEW, StageName.MERGE_WAIT})

#: WorkItem payload key holding consecutive file-overlap deferrals.
_FILE_OVERLAP_DEFERRALS_KEY = "file_overlap_deferrals"

#: Host-owned snapshot of the active claims that last blocked a queued item.
_FILE_OVERLAP_BLOCKED_CLAIMS_KEY = "_file_overlap_blocked_claims"

#: Deferral counts strictly above this value are logged as warnings.
_FILE_OVERLAP_WARNING_THRESHOLD = 10

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

# JSONL is diagnostic only. GitHub facts, learning journals, arming state, and issue-wave
# checkpoints are the restart authorities for their owned state.
_DEFAULT_EVENT_LOG_CAPACITY = 1_024
_DEFAULT_TERMINAL_DETAIL_CAPACITY = 128
_SOURCE_REGISTRY_RETRY_DELAY_S = 0.05

#: Downstream-first drain order generated from the authoritative pipeline
#: order; the finished sink drains first so results are recorded promptly.
_DRAIN_ORDER: tuple[StageName, ...] = tuple(reversed(PIPELINE_ORDER))

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

StageStepResult = Continue | JobRequest | StageOutcome


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
    """Configuration shared by the coordinator and every pipeline stage."""

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
    learning_workers: int = 1
    learning_queue_capacity: int = 1
    dry_run: bool = False
    grace_s: float = _DEFAULT_GRACE_S
    phase_timeout_s: float | None = None
    agent: str = "claude"
    disable_pi_automation: bool = False
    auth_status_timeout: int = 10
    pi_isolation_adapter: str | None = None
    pi_dir: Path | None = None
    model: str = ""
    planner_model: str = ""
    reviewer_model: str = ""
    implementer_model: str = ""
    fallback_model: str = ""
    planner_reasoning_effort: str = ""
    reviewer_reasoning_effort: str = ""
    implementer_reasoning_effort: str = ""
    gh_extra_path_root: Path | None = None
    rate_guard_enabled: bool = True
    rate_guard_threshold: int = 200
    plugin_skills_dir: Path | None = None
    planner_timeout: int = 1200
    reviewer_timeout: int = 1200
    implementer_timeout: int = 1800
    address_review_timeout: int = 7200
    git_message_timeout: int = 1200
    poll_max_wait: int = 1200
    clone_timeout: int = 120
    network_timeout: int = 120
    gh_timeout: int = 120
    metadata_timeout: int = 10
    rebase_timeout: int = 2400
    diff_collect_timeout: int = 60
    pre_pr_test_timeout: int = 600
    no_advise: bool = False
    nitpick: bool = False
    drive_green_all: bool = False
    include_bot_prs: bool = True
    include_all_authors: bool = False
    # Per-budget overrides applied on top of the ROUTES defaults.
    budget_overrides: dict[str, int] = field(default_factory=dict)
    pre_pr_test_argv: tuple[str, ...] = PRE_PR_TEST_ARGV
    run_pre_pr_tests: bool = False
    serialize_file_overlap: bool = True
    # Zero disables the optional local observability server.
    metrics_port: int = 0
    # Alerts are emitted only from measured queue depths and breaker snapshots.
    alert_queue_depth_threshold: int = 100
    # Product-layer breaker snapshot reader; keeps this module zero-I/O.
    circuit_breaker_snapshot_provider: Callable[[], dict[str, dict[str, Any]]] | None = None
    event_log_path: Path | None = None
    evidence_receipt_dir: Path | None = None
    # Recent local diagnostic retention.  These limits intentionally do not
    # alter the GitHub journal or restart behavior.
    event_log_capacity: int = _DEFAULT_EVENT_LOG_CAPACITY
    terminal_detail_capacity: int = _DEFAULT_TERMINAL_DETAIL_CAPACITY
    projects_dir: Path = field(default_factory=lambda: Path.home() / "Projects")
    # Explicit exceptions to the normal ``projects_dir / repo`` checkout layout.
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
    # Appended so positional construction keeps ``repo_source_factory`` third.
    issue_limit: int | None = None
    enable_learn: bool = True
    reset_plan_review_sessions: frozenset[int] = frozenset()

    @property
    def enable_advise(self) -> bool:
        """Return the positive stage-facing form of ``no_advise``."""
        return not self.no_advise


def _work_window(config: PipelineConfig) -> int:
    """Return the global bound for all nonterminal pipeline work."""
    return max(1, config.parallel_repos * config.max_workers)


@dataclass
class _Paths:
    repo_root: Path
    worktree: Path
    projects_dir: Path
    source_workspaces: Any = None


@dataclass(frozen=True)
class _PendingHandoff:
    """A completed route retained by a source lease or bounded handoff slot.

    ``StageQueueLease`` keeps the source slot occupied until the destination
    accepts the item.  The coordinator records only the next intent here; it
    deliberately does not mutate ``WorkItem.stage``, ``state``, history, or a
    terminal result until that destination-first admission succeeds.  There
    can be at most one pending handoff per active lease, so this is bounded by
    fixed stage-queue capacity rather than acting as a spill buffer.
    """

    item: WorkItem
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
    issues: deque[int]
    base_sha: str
    run_nonce: str
    wave_lease: WaveLease | None = None
    overlap_blocked_claims: frozenset[_admission.PlanFileClaim] | None = None


@dataclass
class _DirectPrSource:
    """One bounded cursor over the caller-provided explicit PR scope.

    As with direct issues, a PR is classified only at a safe admission point;
    this avoids retaining large review-context payloads while queues saturate.
    """

    repo: str
    prs: Iterator[int]
    base_sha: str
    wave_lease: WaveLease | None = None
    pending_pr: int | None = None


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
