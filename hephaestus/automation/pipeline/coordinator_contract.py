"""Static host contract shared by the coordinator collaborators."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections import Counter, OrderedDict, deque
    from collections.abc import Callable
    from pathlib import Path
    from threading import Event
    from typing import Any, Protocol

    from hephaestus.automation.learning_journal import LearningClaimRegistry, LearningJournalStore
    from hephaestus.automation.review_journal import CommentJournalReadError

    from . import (
        admission as _admission,
        coordinator_types as ct,
        seeding as _seeding,
        stages as stages_mod,
        work_item as work_item_mod,
    )
    from .coordinator_types import (
        ItemResult,
        PipelineConfig,
        WorkItem,
        _ActiveRepoIssueSource,
        _DirectIssueSource,
        _DirectPrSource,
        _PendingHandoff,
        _RepoEntrySource,
    )
    from .events import StageEvent
    from .jobs import JobHandle, JobResult
    from .queues import CompletionQueue, StageQueue, StageQueueLease
    from .routing import Route, StageName
    from .stages import Stage, StageContext, StageGitHub, base as stage_base_mod
    from .summary import TerminalSummary
    from .work_item import PreservedWorktree
    from .worker_protocol import AuxiliaryWorker, MainWorker

    class _CoordinatorHost(Protocol):
        """State and cross-collaborator methods supplied by ``Coordinator``."""

        config: PipelineConfig
        github: StageGitHub
        _github_factory: Callable[[str, Path], StageGitHub] | None
        shutdown: Event
        _worker_shutdown: Event
        _force_shutdown: Event
        _monotonic: Callable[[], float]
        _wall_time: Callable[[], float]
        shutdown_event: Event
        worker_shutdown_event: Event
        force_shutdown_event: Event
        _idle_poll_s: float
        _stall_ticks_before_retry: int
        _step_watchdog_s: float
        _file_overlap_warning_threshold: int
        completion_q: CompletionQueue
        pool: MainWorker
        auxiliary_pool: AuxiliaryWorker
        auxiliary_completion_q: CompletionQueue
        auxiliary_in_flight: dict[JobHandle, WorkItem]
        _learning_work_permit_ids: set[int]
        _direct_scope_bootstrap_pending: bool
        _grace_deadline: float | None
        _immediate: bool
        _progress: bool
        _fatal: bool
        _stalled_ticks: int
        _pass_work_count: int
        queues: dict[StageName, StageQueue]
        timers: list[tuple[float, int, WorkItem]]
        in_flight: dict[JobHandle, WorkItem]
        inflight_per_repo: Counter[str]
        stages: dict[StageName, Stage]
        items: list[WorkItem]
        ledger: list[ItemResult]
        preserved: list[PreservedWorktree]
        recovery_preserved: list[PreservedWorktree]
        event_log: deque[tuple[Any, ...]]
        _completion_wakeup: Event
        _completion_saturation: Event
        _pipeline_writer_worktrees: dict[tuple[str, str], WorkItem]
        _leases: dict[int, StageQueueLease]
        _pending_handoffs: dict[int, _PendingHandoff]
        _direct_issue_source: _DirectIssueSource | None
        _direct_pr_source: _DirectPrSource | None
        _direct_wave_lease: ct.WaveLease | None
        _wave_mode_active: bool
        _repo_entry_source: _RepoEntrySource | None
        _repo_issue_sources: deque[_ActiveRepoIssueSource]
        _live_work_permit_ids: set[int]
        _seen_item_ids: set[int]
        _manual_rebase_selected: set[tuple[str, str, int]]
        _plan_updates_selected: set[tuple[str, int]]
        _routes: dict[StageName, Route]
        _terminal_summary: TerminalSummary
        _ctx_cache: OrderedDict[str, StageContext]
        _ctx_cache_capacity: int
        _learning_claim_registry: LearningClaimRegistry
        _event_log_disabled: bool
        _observed_inflight_repos: set[str]
        _observed_circuit_breaker_states: dict[str, str]
        _metrics_registry: Any | None
        _metrics_server: Any | None
        _alert_tracker: Any | None
        _install_signals: bool
        _pool_shut_down: bool
        _loops_run: int
        _seq: int
        _agent_job_count: int
        _agent_job_time_s: float
        _auxiliary_job_count: int
        _auxiliary_job_time_s: float
        _auxiliary_job_failure_count: int
        _auxiliary_job_deferred_count: int

        def _direct_issue_identity(
            self, repo: str, issue: int, run_nonce: str
        ) -> tuple[int | None, str]: ...

        def _prepare_direct_item(
            self, entry: _seeding.SeedEntry, repo: str, base_sha: str, run_nonce: str | None = None
        ) -> WorkItem: ...

        def _seed_direct_issue_entry(
            self, repo: str, issue: int, *, github: StageGitHub
        ) -> _seeding.SeedEntry: ...

        def _drain_implementation(self) -> None: ...

        def _select_implementation_dispatch(
            self, items: list[ct.WorkItem]
        ) -> list[ct.WorkItem]: ...

        def _overlap_serialization_enabled(self) -> bool: ...

        def _record_file_overlap_deferral(self, item: ct.WorkItem, identity: str) -> None: ...

        @staticmethod
        def _file_overlap_deferral_age(item: ct.WorkItem) -> int: ...

        def _select_file_overlap_implementation_items(
            self, candidates: list[tuple[ct.WorkItem, str]]
        ) -> list[ct.WorkItem]: ...

        @staticmethod
        def _implementation_duplicates(items: list[ct.WorkItem]) -> list[ct.WorkItem]: ...

        def _active_implementation_file_claims(
            self,
            *,
            exclude_item: ct.WorkItem | None = None,
            exclude_item_ids: set[int] | None = None,
        ) -> set[_admission.PlanFileClaim]: ...

        def _clear_implementation_file_claims_on_exit(
            self, item: ct.WorkItem, target: ct.StageName
        ) -> None: ...

        def _claim_selected_implementation_item(self, item: ct.WorkItem) -> bool: ...

        def _read_implementation_file_claims(
            self, item: ct.WorkItem
        ) -> frozenset[_admission.PlanFileClaim]: ...

        def _defer_implementation_plan_read(
            self, item: ct.WorkItem, error: CommentJournalReadError
        ) -> None: ...

        def _admit(self, item: ct.WorkItem) -> bool: ...

        @property
        def live_work_count(self) -> int: ...

        @property
        def learning_work_count(self) -> int: ...

        def _try_acquire_work_permit(
            self, item: ct.WorkItem, stage: ct.StageName | None = None
        ) -> bool: ...

        def _release_work_permit(self, item: ct.WorkItem) -> None: ...

        def _lane_handoff_capacity(self, item: ct.WorkItem, target: ct.StageName) -> bool: ...

        @staticmethod
        def _is_auxiliary_stage(stage: ct.StageName) -> bool: ...

        def _persist_learning_intents(self, item: ct.WorkItem) -> None: ...

        def _submit(self, item: ct.WorkItem, request: ct.JobRequest) -> None: ...

        def _submit_ready_job(self, item: ct.WorkItem, request: ct.JobRequest) -> None: ...

        def _complete_rate_budget(self, item: ct.WorkItem, result: JobResult) -> None: ...

        def _drain_completions(self) -> None: ...

        def _wait_for_completion(self, timeout: float) -> None: ...

        def _handle_completion(
            self, handle: JobHandle, result: JobResult, *, auxiliary: bool = False
        ) -> None: ...

        def _record_completion_metrics(
            self, item: ct.WorkItem, handle: JobHandle, result: JobResult, *, auxiliary: bool
        ) -> None: ...

        def _activate_handoff(
            self,
            item: ct.WorkItem,
            target: ct.StageName,
            *,
            enter: bool,
            result: ct.ItemResult | None,
        ) -> None: ...

        def _record_event(self, event: str, *fields: ct.Any) -> None: ...

        @staticmethod
        def _item_key(item: ct.WorkItem) -> str: ...

        def _complete_pending_handoff_pair(
            self, first_id: int, first: _PendingHandoff, second_id: int, second: _PendingHandoff
        ) -> bool: ...

        def _drain_complementary_handoff_pairs(self) -> None: ...

        def _classify_repo_issue_entry(
            self, repo: str, source: ct.RepoIssueSource, number: int, github: StageGitHub
        ) -> _seeding.SeedEntry | None: ...

        def _record_issue_classification_failure(
            self, repo: str, number: int, error: Exception
        ) -> None: ...

        def _restore_learning_intents(
            self, item: ct.WorkItem, primary_stage: ct.StageName | None, primary_reason: str
        ) -> None: ...

        @staticmethod
        def _restore_post_processing(item: ct.WorkItem, record: dict[str, ct.Any]) -> bool: ...

        @staticmethod
        def _disable_valid_records(
            records: list[dict[str, ct.Any]], journal: LearningJournalStore
        ) -> bool: ...

        def _default_stages(self) -> dict[ct.StageName, stages_mod.Stage]: ...

        def _ctx_for_repo(self, repo: str) -> stages_mod.StageContext: ...

        def _ctx_for(self, item: ct.WorkItem) -> stages_mod.StageContext: ...

        def _budget_for(self, name: str) -> int: ...

        def _record_stage_event(self, event: StageEvent) -> None: ...

        def _observability_snapshot(self) -> dict[str, ct.Any]: ...

        def _health_snapshot(self) -> dict[str, ct.Any]: ...

        def _emit_observability_tick(self) -> None: ...

        @staticmethod
        def _emit_lane_gauges(registry: ct.Any, snapshot: dict[str, ct.Any]) -> None: ...

        def _wake_completion_wait(self) -> None: ...

        def run(self) -> int: ...

        def _effective_items(self) -> list[ct.WorkItem]: ...

        def _active_preserved_worktrees(self) -> list[work_item_mod.PreservedWorktree]: ...

        def _active_recovery_worktrees(self) -> list[work_item_mod.PreservedWorktree]: ...

        def _exit_code(self) -> int: ...

        def _all_idle(self) -> bool: ...

        def _record_terminal_result(self, item: ct.WorkItem) -> None: ...

        def _claim_item(
            self, stage_name: ct.StageName, *, index: int = 0
        ) -> ct.WorkItem | None: ...

        def _restore_source_lease(self, item: ct.WorkItem) -> bool: ...

        def _release_source_lease(self, item: ct.WorkItem) -> bool: ...

        def _handoff_item(
            self,
            item: ct.WorkItem,
            target: ct.StageName,
            *,
            enter: bool,
            result: ct.ItemResult | None = None,
        ) -> bool: ...

        def _drain_pending_handoffs(self) -> None: ...

        def _grace_exceeded(self) -> bool: ...

        def _idle_wait(self) -> None: ...

        def _retry_stalled_queues(self) -> None: ...

        def _timer_park(self, item: ct.WorkItem, delay_s: float) -> None: ...

        def _wake_timers(self) -> None: ...

        def _park_resumable(self, item: ct.WorkItem) -> None: ...

        def _record_resumable_recovery_worktrees(self, item: ct.WorkItem) -> None: ...

        @staticmethod
        def _job_result_event_fields(result: JobResult) -> dict[str, ct.Any]: ...

        @staticmethod
        def _job_result_error_class(result: JobResult) -> str | None: ...

        def _drain_queues(self) -> None: ...

        def _run_item(self, item: ct.WorkItem) -> None: ...

        def _step_with_watchdog(
            self, stage: stages_mod.Stage, item: ct.WorkItem, ctx: stages_mod.StageContext
        ) -> ct.StageStepResult: ...

        def _route(self, item: ct.WorkItem, outcome: ct.StageOutcome) -> None: ...

        def _route_direct_scope_bootstrap(
            self, item: ct.WorkItem, outcome: ct.StageOutcome
        ) -> None: ...

        def _route_retry(self, item: ct.WorkItem, outcome: ct.StageOutcome) -> None: ...

        def _register_pipeline_writer_worktree(
            self, item: ct.WorkItem, job: object, result: JobResult
        ) -> None: ...

        def _branch_worktree_owner_status(
            self, item: ct.WorkItem, branch: str, owner_path: str
        ) -> stage_base_mod.BranchWorktreeOwnerStatus: ...

        def _route_fail_back(
            self, item: ct.WorkItem, outcome: ct.StageOutcome, route: Route
        ) -> None: ...

        def _finish(self, item: ct.WorkItem, *, passed: bool, reason: str) -> None: ...

        def _install_signal_handlers(self) -> None: ...

        def _teardown_immediate(self) -> None: ...

        def _shutdown_pool(self) -> None: ...

        def _finalize_resumable(self) -> None: ...

        def _externalize_repo_issue_source(
            self, item: ct.WorkItem, source: ct.RepoIssueSource
        ) -> bool: ...

        def _repo_source_slots_used(self) -> int: ...

        def _repo_source_can_admit(self) -> bool: ...

        def _drain_repo_issue_sources(self) -> None: ...

        def _drain_repo_issue_source(self, active: ct._ActiveRepoIssueSource) -> bool: ...

        def _record_repo_source_failure(self, repo: str, reason: str) -> None: ...

        def _live_issue_keys(self) -> set[tuple[str, int]]: ...

        def _push_item(
            self,
            item: ct.WorkItem,
            stage: ct.StageName,
            enter: bool,
            *,
            defer_if_full: bool = False,
        ) -> bool: ...

        def _seed_pass(self) -> int: ...

        def _begin_direct_scope_bootstrap(self, repo: str) -> int: ...

        def _begin_repo_entry_source(self, repos: list[str]) -> None: ...

        def _drain_repo_entry_source(self) -> int: ...

        def _begin_direct_issue_source(self, repo: str, base_sha: str) -> None: ...

        def _begin_direct_pr_source(self, repo: str, base_sha: str) -> None: ...

        def _direct_issue_queues_can_accept(self) -> bool: ...

        def _prepare_direct_issue_item(
            self,
            source: ct._DirectIssueSource,
            issue: int,
        ) -> ct.WorkItem | None: ...

        def _read_direct_issue_candidate(
            self, source: ct._DirectIssueSource, issue: int, *, overlap_enabled: bool
        ) -> tuple[ct.WorkItem | None, CommentJournalReadError | None]: ...

        def _drain_direct_issue_source(self) -> int: ...

        def _drain_direct_pr_source(self) -> int: ...

        def _scope_seed_decision(
            self,
            issue: int,
            stage: ct.StageName | None,
            reason: str,
            scope_stages: frozenset[ct.StageName] | None,
            *,
            repo: str,
        ) -> tuple[ct.StageName | None, str, bool]: ...

        def _seed_direct_pr_entry(
            self, repo: str, pr: int, *, github: StageGitHub
        ) -> _seeding.SeedEntry: ...

        @staticmethod
        def _entry_to_item(entry: _seeding.SeedEntry, default_repo: str) -> ct.WorkItem: ...

        def _has_pending_seed_source(self) -> bool: ...

        def _reseed_if_converged(self) -> bool: ...

else:

    class _CoordinatorHost:
        pass
