"""Static host contract shared by the coordinator collaborators."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections import Counter, OrderedDict, deque
    from collections.abc import Callable
    from pathlib import Path
    from threading import Event
    from typing import Any, Protocol

    from . import admission as _admission
    from .coordinator_types import (
        ItemResult,
        PipelineConfig,
        RepoIssueSource,
        WorkItem,
        _ActiveRepoIssueSource,
        _DirectIssueSource,
        _DirectPrSource,
        _PendingHandoff,
        _RepoEntrySource,
    )
    from .jobs import JobHandle, JobResult
    from .queues import CompletionQueue, StageQueue, StageQueueLease
    from .routing import Route, StageName
    from .seeding import SeedEntry
    from .stages import JobRequest, Stage, StageContext, StageGitHub
    from .summary import TerminalSummary
    from .work_item import PreservedWorktree

    class _CoordinatorHost(Protocol):
        """State and cross-collaborator methods supplied by ``Coordinator``."""

        # fmt: off
        config: PipelineConfig
        github: StageGitHub
        _github_factory: Callable[[str, Path], StageGitHub] | None
        shutdown: Event
        _force_shutdown: Event
        _monotonic: Callable[[], float]
        _wall_time: Callable[[], float]
        shutdown_event: Event
        force_shutdown_event: Event
        _idle_poll_s: float
        _stall_ticks_before_force: int
        _step_watchdog_s: float
        _file_overlap_warning_threshold: int
        completion_q: CompletionQueue
        auxiliary_completion_q: CompletionQueue
        pool: Any
        auxiliary_pool: Any
        queues: dict[StageName, StageQueue]
        timers: list[tuple[float, int, WorkItem]]
        in_flight: dict[JobHandle, WorkItem]
        auxiliary_in_flight: dict[JobHandle, WorkItem]
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
        _inflight_implementation_claims: dict[JobHandle, set[_admission.PlanFileClaim]]
        _implementation_file_claims: dict[int, set[_admission.PlanFileClaim]]
        _leases: dict[int, StageQueueLease]
        _pending_handoffs: dict[int, _PendingHandoff]
        _direct_issue_source: _DirectIssueSource | None
        _direct_pr_source: _DirectPrSource | None
        _direct_wave_lease: Any
        _wave_mode_active: bool
        _repo_entry_source: _RepoEntrySource | None
        _repo_issue_sources: deque[_ActiveRepoIssueSource]
        _live_work_permit_ids: set[int]
        _learning_work_permit_ids: set[int]
        _seen_item_ids: set[int]
        _routes: dict[StageName, Route]
        _terminal_summary: TerminalSummary
        _ctx_cache: OrderedDict[str, StageContext]
        _ctx_cache_capacity: int
        _learning_claim_registry: Any
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
        _auxiliary_pool_separate: bool
        _direct_scope_bootstrap_pending: bool
        _grace_deadline: float | None
        _immediate: bool
        _progress: bool
        _fatal: bool
        _auxiliary_job_failure_count: int
        _pass_work_count: int
        _stalled_ticks: int

        @property
        def live_work_count(self) -> int: pass  # fmt: skip
        def _ctx_for_repo(self, repo: str) -> StageContext: pass  # fmt: skip
        def _record_event(self, event: str, *fields: Any) -> None: pass  # fmt: skip
        def _try_acquire_work_permit(self, item: WorkItem, stage: StageName | None = None) -> bool: pass  # noqa: E501
        def _release_work_permit(self, item: WorkItem) -> None: pass  # fmt: skip
        def _record_terminal_result(self, item: WorkItem) -> None: pass  # fmt: skip
        def _claim_item(self, stage_name: StageName, *, index: int = 0) -> WorkItem | None: pass
        def _release_source_lease(self, item: WorkItem) -> bool: pass  # fmt: skip

        def _run_item(self, item: WorkItem) -> None: pass  # fmt: skip

        def _finish(self, item: WorkItem, *, passed: bool, reason: str) -> None: pass  # fmt: skip

        @staticmethod
        def _is_auxiliary_stage(stage: StageName) -> bool: pass  # fmt: skip

        def _lane_handoff_capacity(self, item: WorkItem, target: StageName) -> bool: pass

        def _persist_learning_intents(self, item: WorkItem) -> None: pass  # fmt: skip

        def _submit(self, item: WorkItem, request: JobRequest) -> None: pass  # fmt: skip

        def _drain_completions(self) -> None: pass  # fmt: skip

        def _wait_for_completion(self, timeout: float) -> None: pass  # fmt: skip

        def _ctx_for(self, item: WorkItem) -> StageContext: pass  # fmt: skip

        def _park_resumable(self, item: WorkItem) -> None: pass  # fmt: skip

        def _timer_park(self, item: WorkItem, delay_s: float) -> None: pass  # fmt: skip

        @staticmethod
        def _job_result_event_fields(result: JobResult) -> dict[str, Any]: pass  # fmt: skip

        def _register_pipeline_writer_worktree(
            self,
            item: WorkItem,
            job: object,
            result: JobResult,
        ) -> None: pass  # fmt: skip

        def _scope_seed_decision(
            self,
            issue: int,
            stage: StageName | None,
            reason: str,
            scope_stages: frozenset[StageName] | None,
        ) -> tuple[StageName | None, str, bool]: pass  # fmt: skip

        def _classify_repo_issue_entry(
            self,
            repo: str,
            source: RepoIssueSource,
            number: int,
            github: StageGitHub,
        ) -> SeedEntry | None: pass  # fmt: skip

        def _restore_learning_intents(
            self,
            item: WorkItem,
            primary_stage: StageName | None,
            primary_reason: str,
        ) -> None: pass  # fmt: skip

        def _direct_issue_identity(
            self,
            repo: str,
            issue: int,
            run_nonce: str,
        ) -> tuple[int | None, str]: pass  # fmt: skip

        def _externalize_repo_issue_source(self, item: WorkItem, source: RepoIssueSource) -> bool: pass  # noqa: E501
        def _drain_repo_issue_sources(self) -> None: pass  # fmt: skip
        def _seed_products(self, item: WorkItem) -> None: pass  # fmt: skip
        def _push_item(
            self,
            item: WorkItem,
            stage: StageName,
            enter: bool,
            *,
            defer_if_full: bool = False,
        ) -> bool: pass  # fmt: skip

        @staticmethod
        def _item_key(item: WorkItem) -> str: pass  # fmt: skip
        def _seed_pass(self) -> int: pass  # fmt: skip
        def _drain_repo_entry_source(self) -> int: pass  # fmt: skip
        def _begin_direct_issue_source(self, repo: str, base_sha: str) -> None: pass  # fmt: skip
        def _begin_direct_pr_source(self, repo: str, base_sha: str) -> None: pass  # fmt: skip
        def _drain_direct_issue_source(self) -> int: pass  # fmt: skip
        def _drain_direct_pr_source(self) -> int: pass  # fmt: skip
        def _prepare_direct_item(
            self, entry: Any, repo: str, base_sha: str, run_nonce: str | None = None
        ) -> WorkItem: pass  # fmt: skip
        def _seed_direct_issue_entry(
            self, repo: str, issue: int, *, github: StageGitHub | None = None
        ) -> Any: pass  # fmt: skip
        def _reseed_if_converged(self) -> bool: pass  # fmt: skip
        def _drain_implementation(self) -> None: pass  # fmt: skip
        def _overlap_serialization_enabled(self) -> bool: pass  # fmt: skip
        def _active_implementation_file_claims(
            self, *, exclude_item: WorkItem | None = None
        ) -> set[_admission.PlanFileClaim]: pass  # fmt: skip
        def _capture_implementation_file_claims(
            self, item: WorkItem
        ) -> set[_admission.PlanFileClaim]: pass  # fmt: skip
        def _clear_implementation_file_claims_on_exit(
            self, item: WorkItem, target: StageName
        ) -> None: pass  # fmt: skip
        def _admit(self, item: WorkItem) -> bool: pass  # fmt: skip
        # fmt: on
else:

    class _CoordinatorHost:
        pass
