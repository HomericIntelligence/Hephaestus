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
        CompletionQueue,
        ItemResult,
        JobHandle,
        PipelineConfig,
        PreservedWorktree,
        RepoIssueSource,
        Route,
        Stage,
        StageContext,
        StageGitHub,
        StageName,
        StageQueue,
        StageQueueLease,
        TerminalSummary,
        WorkItem,
        _ActiveRepoIssueSource,
        _DirectIssueSource,
        _DirectPrSource,
        _PendingHandoff,
        _RepoEntrySource,
        _StageRunConfig,
    )

    class _CoordinatorHost(Protocol):
        """State and cross-collaborator methods supplied by ``Coordinator``."""

        config: PipelineConfig
        github: StageGitHub
        _github_factory: Callable[[str, Path], StageGitHub] | None
        shutdown: Event
        completion_q: CompletionQueue
        pool: Any
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
        _inflight_implementation_claims: dict[JobHandle, set[_admission.PlanFileClaim]]
        _implementation_file_claims: dict[int, set[_admission.PlanFileClaim]]
        _leases: dict[int, StageQueueLease]
        _pending_handoffs: dict[int, _PendingHandoff]
        _direct_issue_source: _DirectIssueSource | None
        _direct_pr_source: _DirectPrSource | None
        _repo_entry_source: _RepoEntrySource | None
        _repo_issue_sources: deque[_ActiveRepoIssueSource]
        _live_work_permit_ids: set[int]
        _seen_item_ids: set[int]
        _routes: dict[StageName, Route]
        _terminal_summary: TerminalSummary
        _stage_config: _StageRunConfig
        _ctx_cache: OrderedDict[str, StageContext]
        _ctx_cache_capacity: int
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

        @property
        def live_work_count(self) -> int: ...

        def _ctx_for_repo(self, repo: str) -> StageContext: ...

        def _record_event(self, event: str, *fields: Any) -> None: ...

        def _try_acquire_work_permit(self, item: WorkItem) -> bool: ...

        def _release_work_permit(self, item: WorkItem) -> None: ...

        def _record_terminal_result(self, item: WorkItem) -> None: ...

        def _claim_item(self, stage_name: StageName, *, index: int = 0) -> WorkItem | None: ...

        def _release_source_lease(self, item: WorkItem) -> bool: ...

        def _run_item(self, item: WorkItem) -> None: ...

        def _finish(self, item: WorkItem, *, passed: bool, reason: str) -> None: ...

        def _externalize_repo_issue_source(
            self, item: WorkItem, source: RepoIssueSource
        ) -> bool: ...

        def _drain_repo_issue_sources(self) -> None: ...

        def _seed_products(self, item: WorkItem) -> None: ...

        def _push_item(
            self,
            item: WorkItem,
            stage: StageName,
            enter: bool,
            *,
            defer_if_full: bool = False,
        ) -> bool: ...

        @staticmethod
        def _item_key(item: WorkItem) -> str: ...

        def _seed_pass(self) -> int: ...

        def _drain_repo_entry_source(self) -> int: ...

        def _begin_direct_issue_source(self, repo: str, base_sha: str) -> None: ...

        def _begin_direct_pr_source(self, repo: str, base_sha: str) -> None: ...

        def _drain_direct_issue_source(self) -> int: ...

        def _drain_direct_pr_source(self) -> int: ...

        def _reseed_if_converged(self) -> bool: ...

        def _drain_implementation(self) -> None: ...

        def _overlap_serialization_enabled(self) -> bool: ...

        def _active_implementation_file_claims(
            self, *, exclude_item: WorkItem | None = None
        ) -> set[_admission.PlanFileClaim]: ...

        def _capture_implementation_file_claims(
            self, item: WorkItem
        ) -> set[_admission.PlanFileClaim]: ...

        def _clear_implementation_file_claims_on_exit(
            self, item: WorkItem, target: StageName
        ) -> None: ...

        def _admit(self, item: WorkItem) -> bool: ...

else:

    class _CoordinatorHost:
        """Runtime-empty base for the statically checked host contract."""
