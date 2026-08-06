# The façade deliberately re-exports the coordinator's historical symbols.
# ruff: noqa: F403, F405, D105, D107, E501
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from hephaestus.automation.issue_guard import (
    GitHubIssueGuardStore,
    GuardHandle,
    GuardLostError,
    GuardStore,
    IssueGuard,
    assert_recovery_secret_absent,
)
from hephaestus.automation.pipeline.guarded_github import GuardedStageGitHub

from .coordinator_dispatch import ImplementationDispatcher
from .coordinator_runtime import CoordinatorRuntime
from .coordinator_sources import SourceCoordinator
from .coordinator_types import *

logger = logging.getLogger(__name__)

# Keep the emitted metric catalogue visible on the public façade.  The runtime
# collaborator owns emission; this compatibility catalogue keeps the existing
# observability drift guard scoped to the public coordinator module.
_COORDINATOR_METRIC_NAMES = (
    "hephaestus_pipeline_queue_depth",
    "hephaestus_pipeline_inflight_jobs",
    "hephaestus_pipeline_inflight_per_repo",
    "hephaestus_pipeline_loops_total",
    "hephaestus_pipeline_stalled_ticks",
    "hephaestus_circuit_breaker_state",
    "hephaestus_pipeline_alert_active",
    "hephaestus_pipeline_jobs_total",
    "hephaestus_pipeline_agent_job_seconds_total",
)


class SourceGuardClaim:
    """Temporary source claim that can transfer ownership to a WorkItem."""

    def __init__(self, coordinator: "Coordinator", repository: str, issue: int, handle: GuardHandle | None) -> None:
        self.coordinator = coordinator
        self.repository = repository
        self.issue = issue
        self.handle = handle
        self.transferred = False
        raw = coordinator._ctx_for_repo(repository.split("/", 1)[1]).github
        self.github = (
            GuardedStageGitHub(
                raw=raw,
                guard_store=coordinator.guard_store_factory(handle.credential.repository),
                credential=handle.credential,
            )
            if handle is not None
            else raw
        )

    def __enter__(self) -> "SourceGuardClaim | None":
        if self.coordinator.config.dry_run or not self.coordinator._guard_enabled:
            return self
        return self if self.handle is not None else None

    def transfer_to(self, item: WorkItem) -> None:
        """Transfer the claim to an admitted item before leaving the context."""
        if (
            self.handle is None
            and not self.coordinator.config.dry_run
            and self.coordinator._guard_enabled
        ):
            raise GuardLostError("cannot transfer an unowned source claim")
        if item.issue != self.issue or item.repo != self.repository.split("/", 1)[1]:
            raise GuardLostError("source claim target differs from work item")
        if self.handle is None:
            self.transferred = True
            return
        key = (self.repository, self.issue)
        self.coordinator._issue_guards[key] = self.handle
        item.payload["_issue_guard_handle"] = self.handle
        self.transferred = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is None or self.transferred:
            return
        self.coordinator._release_guard_handle(self.repository, self.issue, self.handle, "source claim finished")


def _guard_repository(org: str, repo: str) -> str:
    return f"{org}/{repo}"


class Coordinator(CoordinatorRuntime, SourceCoordinator, ImplementationDispatcher):
    """Assemble the coordinator's type, runtime, source, and dispatch seams."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        github: StageGitHub,
        pool: Any | None = None,
        stages: dict[StageName, Stage] | None = None,
        github_factory: Callable[[str, Path], StageGitHub] | None = None,
        guard_store_factory: Callable[[str], GuardStore] | None = None,
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
        self._guard_enabled = guard_store_factory is not None or type(github).__name__ == "PipelineGitHub"
        assert_recovery_secret_absent()
        self.run_id = uuid.uuid4()
        self.guard_store_factory = guard_store_factory or GitHubIssueGuardStore
        self._issue_guards: dict[tuple[str, int], GuardHandle] = {}
        self._temporary_source_guard_count = 0
        self._ownership_lost = False
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
            from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

            pool = WorkerPool(
                size=work_window,
                shutdown=self.shutdown,
                completion_q=self.completion_q,
                gh_extra_path_root=config.gh_extra_path_root,
                github_job_runner=PipelineGitHubJobRunner(
                    org=config.org,
                    dry_run=config.dry_run,
                    guard_store_factory=self.guard_store_factory,
                ),
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
        # A holder path reported by Git becomes a safe supersession signal
        # only after a successful create-worktree completion registered it
        # here.  Paths discovered from Git alone can belong to a human or a
        # different automation process and must fail closed.
        self._pipeline_writer_worktrees: dict[tuple[str, str], WorkItem] = {}
        # Known implementation plans retain their repository-scoped file
        # claims for the lifetime of the submitted job.  Never reconstruct
        # this from mutable issue comments during later drain rounds (#2451).
        self._inflight_implementation_claims: dict[JobHandle, set[_admission.PlanFileClaim]] = {}
        # An admission snapshot belongs to the WorkItem, not one of its
        # worktree/agent/test/push jobs. Keeping it for the whole
        # implementation stage prevents a later sub-job from re-fetching a
        # mutable plan and changing the reservation that admitted this work.
        self._implementation_file_claims: dict[int, set[_admission.PlanFileClaim]] = {}
        self.inflight_per_repo: Counter[str] = Counter()
        # A normal (non-implementation) drain claims instead of popping.  The
        # active lease reserves its source capacity while an item executes or
        # waits for a worker completion.  A pending route is represented by a
        # single intent attached to that lease, never by an overflow queue.
        self._leases: dict[int, StageQueueLease] = {}
        self._pending_handoffs: dict[int, _PendingHandoff] = {}
        self._direct_issue_source: _DirectIssueSource | None = None
        self._direct_pr_source: _DirectPrSource | None = None
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
        # Recovery checkouts are intentionally distinct from failed-item
        # debugging worktrees: a later fresh review may pass, but the prior
        # checkout still needs explicit operator cleanup guidance.
        self.recovery_preserved: list[PreservedWorktree] = []
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

    def _guard_service(self, repository: str) -> IssueGuard:
        """Build a guard service carrying this coordinator's run identity."""
        return IssueGuard(
            self.guard_store_factory(repository),
            run_id=self.run_id,
        )

    def _claim_source_issue(self, repo: str, issue: int, stage: str) -> SourceGuardClaim:
        """Acquire a temporary source claim before any issue mutation."""
        repository = _guard_repository(self.config.org, repo)
        handle = (
            None
            if self.config.dry_run or not self._guard_enabled
            else self._guard_service(repository).acquire(repository, issue, stage)
        )
        if handle is not None:
            self._temporary_source_guard_count += 1
        return SourceGuardClaim(self, repository, issue, handle)

    def _prepare_direct_item(
        self, entry: _seeding.SeedEntry, repo: str, base_sha: str, run_nonce: str | None = None
    ) -> WorkItem:
        """Materialize a direct entry and apply its scope-specific metadata."""
        item = self._entry_to_item(entry, repo)
        if is_full_commit_sha(base_sha):
            item.payload[DIRECT_SCOPE_BASE_SHA_KEY] = base_sha
            if run_nonce and item.kind is ItemKind.ISSUE and item.pr is None and item.issue is not None:
                item.branch = f"{item.issue}-auto-impl-direct-{run_nonce}"
                item.payload[DIRECT_SCOPE_WORKTREE_NONCE_KEY] = run_nonce
        if item.stage not in (StageName.REPO, StageName.FINISHED):
            self._pass_work_count += 1
        if item.stage is StageName.FINISHED and item.result is None:
            item.result = ItemResult(
                passed=entry.passed, reason=entry.reason, final_stage=StageName.FINISHED
            )
        return item

    def _seed_direct_issue_entry(
        self, repo: str, issue: int, *, github: StageGitHub | None = None
    ) -> _seeding.SeedEntry:
        """Classify a direct issue through its target repository accessor."""
        if github is None and self._guard_enabled and not self.config.dry_run:
            with self._claim_source_issue(repo, issue, "direct-issue-source") as claim:
                if claim is None:
                    return _seeding.SeedEntry(
                        kind="issue",
                        identifier=issue,
                        stage=StageName.FINISHED,
                        reason="issue guard is held by another automation run",
                        passed=False,
                    )
                return self._seed_direct_issue_entry(repo, issue, github=claim.github)
        github = github or (self._ctx_for_repo(repo).github if repo else self.github)
        scope_stages = self.config.scope.stages if self.config.scope is not None else None
        facts = _seeding.seed_issue_from_github(issue, github)
        if STATE_PLAN_BLOCKED in facts.labels:
            github.ensure_blocked_audit(issue)
        entry = _seeding.seed_entry_from_facts(facts)
        stage, reason, passed = self._scope_seed_decision(
            issue, entry.stage, entry.reason, scope_stages
        )
        return replace(entry, stage=stage, reason=reason, passed=passed)

    def _guard_for_item(self, item: WorkItem) -> GuardHandle:
        """Return the item claim, acquiring lazily only for compatibility seeds."""
        if item.issue is None:
            raise GuardLostError("repository item has no issue guard")
        key = (_guard_repository(self.config.org, item.repo), item.issue)
        handle = self._issue_guards.get(key) or item.payload.get("_issue_guard_handle")
        if isinstance(handle, GuardHandle):
            self._issue_guards[key] = handle
            return handle
        if self.config.dry_run:
            raise GuardLostError("dry-run has no durable issue guard")
        acquired = self._guard_service(key[0]).acquire(key[0], item.issue, item.stage.value)
        if acquired is None:
            raise GuardLostError("issue is already owned by another automation run")
        self._issue_guards[key] = acquired
        item.payload["_issue_guard_handle"] = acquired
        return acquired

    def _confirm_item_guard(self, item: WorkItem, minimum_valid_for: timedelta) -> None:
        """Confirm the exact item guard immediately before dispatch."""
        if self.config.dry_run or not self._guard_enabled or item.issue is None:
            return
        handle = self._guard_for_item(item)
        confirmed = self._guard_service(handle.credential.repository).confirm(
            handle.credential, minimum_valid_for
        )
        key = (handle.credential.repository, handle.credential.issue)
        self._issue_guards[key] = confirmed
        item.payload["_issue_guard_handle"] = confirmed

    def _release_guard_handle(
        self, repository: str, issue: int, handle: GuardHandle, reason: str
    ) -> None:
        """Release an owner handle and retain it on failure for recovery."""
        if self.config.dry_run or not self._guard_enabled:
            return
        self._guard_service(repository).release(handle, reason)
        self._issue_guards.pop((repository, issue), None)
        if self._temporary_source_guard_count:
            self._temporary_source_guard_count -= 1

    def _release_item_guard(self, item: WorkItem, reason: str) -> None:
        """Release the guard retained by a terminal work item."""
        if item.issue is None or self.config.dry_run or not self._guard_enabled:
            return
        handle = item.payload.get("_issue_guard_handle")
        if not isinstance(handle, GuardHandle):
            return
        self._release_guard_handle(handle.credential.repository, item.issue, handle, reason)

    def _release_all_guards(self, reason: str) -> None:
        """Attempt owner-only release after workers have been stopped."""
        for (repository, issue), handle in list(self._issue_guards.items()):
            try:
                self._release_guard_handle(repository, issue, handle, reason)
            except Exception as exc:
                logger.warning(
                    "issue guard for %s#%s remains for recovery: %s", repository, issue, exc
                )

    @staticmethod
    def _minimum_dispatch_lease(job: object) -> timedelta:
        timeout = getattr(job, "timeout_s", 0)
        if isinstance(timeout, int) and timeout > 0:
            return timedelta(seconds=timeout)
        return timedelta(0)


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
