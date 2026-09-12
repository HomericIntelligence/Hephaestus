"""Assemble the queue coordinator and its two worker lanes."""

import logging
import queue as queue_mod
import threading
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path

import hephaestus.automation.pipeline.coordinator_types as ct
import hephaestus.automation.pipeline.seeding as _seeding
from hephaestus.automation.pipeline.athena_executor_scope import (
    pipeline_requires_athena_executor,
)
from hephaestus.automation.pipeline.jobs import JobHandle
from hephaestus.automation.pipeline.queues import CompletionQueue, StageQueue, StageQueueLease
from hephaestus.automation.pipeline.routing import PIPELINE_ORDER, ROUTES, StageName
from hephaestus.automation.pipeline.stages import Stage, StageContext, StageGitHub
from hephaestus.automation.pipeline.stages.repo import (
    DIRECT_SCOPE_BASE_SHA_KEY,
    DIRECT_SCOPE_WORKTREE_NONCE_KEY,
    is_full_commit_sha,
)
from hephaestus.automation.pipeline.summary import TerminalSummary
from hephaestus.automation.pipeline.work_item import (
    ItemKind,
    ItemResult,
    PreservedWorktree,
    WorkItem,
)
from hephaestus.automation.state_labels import STATE_PLAN_BLOCKED

from .athena_skill_jobs import AthenaSkillExecutor
from .coordinator_dispatch import ImplementationDispatcher
from .coordinator_execution import ExecutionCoordinator
from .coordinator_issue_classification import IssueClassificationCoordinator
from .coordinator_learning import LearningRecoveryCoordinator
from .coordinator_runtime import CoordinatorRuntime
from .coordinator_sources import SourceCoordinator
from .worker_protocol import AuxiliaryWorker, MainWorker, WorkerFactory

logger = logging.getLogger(__name__)


class Coordinator(
    CoordinatorRuntime,
    IssueClassificationCoordinator,
    SourceCoordinator,
    ImplementationDispatcher,
    ExecutionCoordinator,
    LearningRecoveryCoordinator,
):
    """Assemble the coordinator's type, runtime, source, and dispatch seams."""

    def __init__(
        self,
        config: ct.PipelineConfig,
        *,
        github: StageGitHub,
        pool_factory: WorkerFactory[MainWorker] | None = None,
        auxiliary_pool_factory: WorkerFactory[AuxiliaryWorker] | None = None,
        stages: dict[StageName, Stage] | None = None,
        github_factory: Callable[[str, Path], StageGitHub] | None = None,
        install_signals: bool = True,
        monotonic: Callable[[], float] | None = None,
        wall_time: Callable[[], float] | None = None,
        shutdown_event: threading.Event | None = None,
        force_shutdown_event: threading.Event | None = None,
        idle_poll_s: float = ct._IDLE_POLL_S,
        stall_ticks_before_retry: int = ct._STALL_TICKS_BEFORE_RETRY,
    ) -> None:
        """Initialize coordinator state.

        Args:
            config: Pipeline configuration.
            github: The coordinator-owned StageGitHub accessor.
            pool_factory: Factory for an ordinary worker lane.
            auxiliary_pool_factory: Factory for an independent auxiliary lane.
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
        if config.learning_workers < 1:
            raise ValueError("learning_workers must be positive")
        if config.learning_queue_capacity < 1:
            raise ValueError("learning_queue_capacity must be positive")
        self._monotonic = monotonic or time.monotonic
        self._wall_time = wall_time or time.time
        self.shutdown = shutdown_event or threading.Event()
        self.shutdown_event = self.shutdown
        self._worker_shutdown = threading.Event()
        self.worker_shutdown_event = self._worker_shutdown
        self._force_shutdown = force_shutdown_event or threading.Event()
        self.force_shutdown_event = self._force_shutdown
        self._idle_poll_s = idle_poll_s
        self._stall_ticks_before_retry = stall_ticks_before_retry
        self._step_watchdog_s = ct._STEP_WATCHDOG_S
        self._file_overlap_warning_threshold = ct._FILE_OVERLAP_WARNING_THRESHOLD
        # These latches are the control plane for the bounded completion
        # queue.  They carry no WorkItem/JobResult payload and therefore
        # cannot become a second, unbounded completion buffer.
        self._completion_wakeup = threading.Event()
        self._completion_saturation = threading.Event()
        work_window = ct._work_window(config)
        self.completion_q: CompletionQueue = queue_mod.Queue(maxsize=work_window)
        self.auxiliary_completion_q: CompletionQueue = queue_mod.Queue(
            maxsize=max(config.learning_queue_capacity, config.learning_workers)
        )
        if (pool_factory is None) != (auxiliary_pool_factory is None):
            raise ValueError("main and auxiliary worker factories must be supplied together")
        athena_executor: AthenaSkillExecutor | None = None
        pool: MainWorker
        auxiliary_pool: AuxiliaryWorker
        if pool_factory is None:
            # Imported here, not module-top: WorkerPool is the pipeline's one
            # I/O-capable module and tests never need it.
            from hephaestus.automation.mnemosyne_skill_host import MnemosyneSkillHost
            from hephaestus.automation.pipeline.rebase_adr_policy import (
                select_rebase_policy,
            )
            from hephaestus.automation.pipeline.worker_pool import WorkerPool
            from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

            athena_executor = (
                MnemosyneSkillHost(gh_extra_path_root=config.gh_extra_path_root)
                if pipeline_requires_athena_executor(config)
                else None
            )
            pool = WorkerPool(
                size=work_window,
                shutdown=self._worker_shutdown,
                completion_q=self.completion_q,
                gh_extra_path_root=config.gh_extra_path_root,
                github_job_runner=PipelineGitHubJobRunner(
                    org=config.org,
                    dry_run=config.dry_run,
                    gh_timeout=config.gh_timeout,
                ),
                athena_skill_executor=athena_executor,
                # Bind the configured organization before the shared worker
                # selects a policy for each repository job.
                rebase_policy_selector=partial(select_rebase_policy, config.org),
                evidence_receipt_dir=config.evidence_receipt_dir,
                host_verification_pyxis_image=config.host_verification_pyxis_image,
                host_verification_pyxis_sha256=config.host_verification_pyxis_sha256,
                host_verification_pyxis_authority=config.host_verification_pyxis_authority,
                host_verification_pyxis_quota_root=config.host_verification_pyxis_quota_root,
                podman_machine=config.podman_machine,
            )
            from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool

            auxiliary_pool = AuxiliaryWorkerPool(
                size=config.learning_workers,
                shutdown=self._force_shutdown,
                completion_q=self.auxiliary_completion_q,
                athena_skill_executor=athena_executor,
                cleanup_runner=pool.run_cleanup_git,
            )
        else:
            assert auxiliary_pool_factory is not None  # noqa: S101
            pool = pool_factory(
                size=work_window,
                shutdown=self._worker_shutdown,
                completion_q=self.completion_q,
            )
            auxiliary_pool = auxiliary_pool_factory(
                size=config.learning_workers,
                shutdown=self._force_shutdown,
                completion_q=self.auxiliary_completion_q,
            )
        if pool is auxiliary_pool:
            raise ValueError("main and auxiliary worker lanes must be distinct")
        self.pool = pool
        self.auxiliary_pool = auxiliary_pool
        for lane in (pool, auxiliary_pool):
            lane.set_completion_notifiers(
                wakeup=self._completion_wakeup,
                saturation=self._completion_saturation,
            )

        self.queues: dict[StageName, StageQueue] = {
            name: StageQueue(
                config.learning_queue_capacity if name is StageName.LEARNING else work_window
            )
            for name in PIPELINE_ORDER
        }
        self.timers: list[tuple[float, int, WorkItem]] = []
        self.in_flight: dict[JobHandle, WorkItem] = {}
        self.auxiliary_in_flight: dict[JobHandle, WorkItem] = {}
        # A holder path reported by Git becomes a safe supersession signal
        # only after a successful create-worktree completion registered it
        # here.  Paths discovered from Git alone can belong to a human or a
        # different automation process and must fail closed.
        self._pipeline_writer_worktrees: dict[tuple[str, str], WorkItem] = {}
        self.inflight_per_repo: Counter[str] = Counter()
        # A normal (non-implementation) drain claims instead of popping.  The
        # active lease reserves its source capacity while an item executes or
        # waits for a worker completion.  A pending route is represented by a
        # single intent attached to that lease, never by an overflow queue.
        #
        # id()-keyed registries invariant: every dict below is keyed by
        # ``id(item)``, which is only safe because entries are released while
        # the coordinator's single event-loop thread still holds a live
        # reference to the item (lease/handoff/permit release happens on the
        # coordinator thread before the last reference can drop), and WorkItem
        # objects are never mutated into new identities. Do not add id()-keyed
        # state that outlives the item's live reference.
        self._leases: dict[int, StageQueueLease] = {}
        self._pending_handoffs: dict[int, ct._PendingHandoff] = {}
        self._direct_issue_source: ct._DirectIssueSource | None = None
        self._direct_pr_source: ct._DirectPrSource | None = None
        self._direct_wave_lease: ct.WaveLease | None = None
        self._wave_mode_active = False
        self._direct_scope_bootstrap_pending = False
        self._repo_entry_source: ct._RepoEntrySource | None = None
        self._repo_issue_sources: deque[ct._ActiveRepoIssueSource] = deque()
        # A StageQueue's capacity only bounds that one stage.  This permit
        # set is the coordinator-wide admission budget: an item acquires one
        # permit on first entry and keeps it while it moves between queues,
        # leases, in-flight jobs, timers, and a retained handoff.  It releases
        # after terminal bookkeeping completes. The set is therefore
        # bounded by ``ct._work_window(config)``, not by the number of stages.
        self._live_work_permit_ids: set[int] = set()
        self._learning_work_permit_ids: set[int] = set()
        self.ledger: list[ItemResult] = []
        self.preserved: list[PreservedWorktree] = []
        # Recovery checkouts are intentionally distinct from failed-item
        # debugging worktrees: a later fresh review may pass, but the prior
        # checkout still needs explicit operator cleanup guidance.
        self.recovery_preserved: list[PreservedWorktree] = []
        self.items: list[WorkItem] = []
        self._terminal_summary = TerminalSummary()
        self.event_log: deque[tuple[ct.Any, ...]] = deque(maxlen=config.event_log_capacity)
        self._event_log_disabled = False
        # Observability is opt-in.  Keep imports and all socket setup out of
        # the default construction path so the product layer retains its
        # zero-I/O import contract.
        self._metrics_registry: ct.Any | None = None
        self._metrics_server: ct.Any | None = None
        self._alert_tracker: ct.Any | None = None
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
        # pure and the scope is immutable for the run's lifetime. Learning is
        # an implicit auxiliary detour and FINISHED is the universal sink, so
        # both control routes remain available outside the selected main-lane
        # scope.
        if config.scope is not None:
            self._routes = config.scope.trimmed_routes()
            self._routes.setdefault(StageName.LEARNING, ROUTES[StageName.LEARNING])
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
        self._auxiliary_job_count = 0
        self._auxiliary_job_time_s = 0.0
        self._auxiliary_job_failure_count = 0
        self._auxiliary_job_deferred_count = 0
        self._loops_run = 0
        self._pass_work_count = 0
        self._progress = False
        self._stalled_ticks = 0
        self._fatal = False
        self._pool_shut_down = False
        self._seen_item_ids: set[int] = set()
        self._manual_rebase_selected: set[tuple[str, str, int]] = set()
        self._plan_updates_selected: set[tuple[str, int]] = set()
        # A context contains a GitHub accessor and path configuration but no
        # mutable item state.  At most C items can be live, so an LRU of C is
        # enough for concurrent work and prevents all-org discovery from
        # retaining one accessor per repository.
        self._ctx_cache: OrderedDict[str, StageContext] = OrderedDict()
        self._ctx_cache_capacity = work_window
        from hephaestus.automation.learning_journal import LearningClaimRegistry

        self._learning_claim_registry = LearningClaimRegistry()

    def _direct_issue_identity(
        self, repo: str, issue: int, run_nonce: str
    ) -> tuple[int | None, str]:
        """Resolve a direct issue's existing PR or new implementation branch."""
        github = self._ctx_for_repo(repo).github
        existing_pr = github.find_pr_for_issue(issue)
        branch = "" if existing_pr is not None else f"{issue}-auto-impl-direct-{run_nonce}"
        return existing_pr, branch

    def _prepare_direct_item(
        self, entry: _seeding.SeedEntry, repo: str, base_sha: str, run_nonce: str | None = None
    ) -> WorkItem:
        """Materialize a direct entry and apply its scope-specific metadata."""
        item = self._entry_to_item(entry, repo)
        if (
            self.config.explicit_pr_review
            and item.stage is StageName.PR_REVIEW
            and item.pr is not None
        ):
            item.payload["explicit_pr_review"] = True
            item.payload["existing_pr"] = True
        if is_full_commit_sha(base_sha):
            item.payload[DIRECT_SCOPE_BASE_SHA_KEY] = base_sha
            if (
                run_nonce
                and item.kind is ItemKind.ISSUE
                and item.pr is None
                and item.issue is not None
            ):
                item.branch = f"{item.issue}-auto-impl-direct-{run_nonce}"
                item.payload[DIRECT_SCOPE_WORKTREE_NONCE_KEY] = run_nonce
        if (
            self.config.update_plan
            and item.stage is StageName.PLANNING
            and item.issue in self.config.issues
            and (repo, item.issue) not in self._plan_updates_selected
        ):
            item.payload["update_plan_required"] = True
        if self.config.rebase and item.stage not in (
            StageName.REPO,
            StageName.FINISHED,
            StageName.LEARNING,
        ):
            keys = {
                (repo, kind, number)
                for kind, number in (("issue", item.issue), ("pr", item.pr))
                if number is not None
            }
            if keys and not keys.intersection(self._manual_rebase_selected):
                item.payload["manual_rebase_required"] = True
                item.payload["manual_rebase_resume_stage"] = item.stage.value
                item.payload["manual_rebase_selection_keys"] = keys
                item.stage = StageName.IMPLEMENTATION
        if item.stage not in (StageName.REPO, StageName.FINISHED):
            self._pass_work_count += 1
        if item.stage is StageName.FINISHED and item.result is None:
            item.result = ItemResult(
                passed=entry.passed, reason=entry.reason, final_stage=StageName.FINISHED
            )
        return item

    def _seed_direct_issue_entry(
        self, repo: str, issue: int, *, github: StageGitHub
    ) -> _seeding.SeedEntry:
        """Classify a direct issue through its target repository accessor."""
        scope_stages = self.config.scope.stages if self.config.scope is not None else None
        facts = _seeding.seed_issue_from_github(issue, github)
        if STATE_PLAN_BLOCKED in facts.labels:
            github.ensure_blocked_audit(issue)
        entry = _seeding.seed_entry_from_facts(facts)
        stage, reason, passed = self._scope_seed_decision(
            issue, entry.stage, entry.reason, scope_stages, repo=repo
        )
        return replace(entry, stage=stage, reason=reason, passed=passed)


def run_pipeline(config: ct.PipelineConfig) -> int:
    """Run the queue-based pipeline to completion.

    Public entry point called from ``loop_runner.main()`` on the default
    queue-pipeline path.

    Args:
        config: Pipeline configuration.

    Returns:
        Exit code: 130 interrupt, 1 any fail/skip/blocked, 0 clean.

    """
    ct._preflight_prompt_catalog()

    # Imported here: pipeline_github maps the accessor onto the real gh
    # helpers and must stay out of the pure pipeline import surface.
    from hephaestus.automation.pipeline_github import PipelineGitHub

    def _github_for(repo_name: str, repo_root: Path) -> PipelineGitHub:
        state_root = Path(str(config.repo_state_roots.get(repo_name, repo_root)))
        return PipelineGitHub(
            config.org,
            repo=repo_name,
            dry_run=config.dry_run,
            repo_root=repo_root,
            state_root=state_root,
            gh_timeout=config.gh_timeout,
        )

    repo = config.repos[0] if config.repos else ""
    repo_root = ct._effective_repo_root(config, repo) if repo else Path(config.projects_dir)
    github = (
        _github_for(repo, repo_root)
        if repo
        else PipelineGitHub(config.org, dry_run=config.dry_run, gh_timeout=config.gh_timeout)
    )
    coordinator = Coordinator(config, github=github, github_factory=_github_for)
    return coordinator.run()
