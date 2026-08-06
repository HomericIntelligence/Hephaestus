# The façade deliberately re-exports the coordinator's historical symbols.
# ruff: noqa: F403, F405
from .coordinator_dispatch import ImplementationDispatcher
from .coordinator_runtime import CoordinatorRuntime
from .coordinator_sources import SourceCoordinator
from .coordinator_types import *

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
            from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

            pool = WorkerPool(
                size=work_window,
                shutdown=self.shutdown,
                completion_q=self.completion_q,
                gh_extra_path_root=config.gh_extra_path_root,
                github_job_runner=PipelineGitHubJobRunner(
                    org=config.org,
                    dry_run=config.dry_run,
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
            name: StageQueue(work_window) for name in PIPELINE_ORDER
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
        # A context contains a GitHub accessor and path configuration but no
        # mutable item state.  At most C items can be live, so an LRU of C is
        # enough for concurrent work and prevents all-org discovery from
        # retaining one accessor per repository.
        self._ctx_cache: OrderedDict[str, StageContext] = OrderedDict()
        self._ctx_cache_capacity = work_window


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
