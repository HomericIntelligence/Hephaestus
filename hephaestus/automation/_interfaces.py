"""Declared abstraction contracts for the automation pipeline.

Import directly from this module — not via ``hephaestus.automation`` —
to avoid defeating the lazy-export design of the package __init__.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable


@runtime_checkable
class PRDiscoveryProtocol(Protocol):
    """Structural contract for PR discovery (PRDiscovery, used by CIDriver).

    Full public surface of PRDiscovery (pr_discovery.py): resolve_viewer_login:58,
    discover_bot_prs:96, discover_failing_prs:183, is_bot_pr_mode:260,
    list_open_prs_remaining:279, pr_merge_state:375.
    """

    def resolve_viewer_login(self) -> str:
        """Resolve the authenticated viewer's GitHub login."""

    def discover_bot_prs(self) -> dict[int, int]:
        """Discover bot-authored PRs mapped by issue number."""

    def discover_failing_prs(self, *args: Any, **kwargs: Any) -> Any:
        """Discover PRs with failing CI checks."""

    def is_bot_pr_mode(self, issue_number: int, pr_number: int) -> bool:
        """Check if issue has an associated bot-authored PR."""

    def list_open_prs_remaining(self) -> list[dict[str, Any]]:
        """List all open PRs not yet processed."""

    def pr_merge_state(self, pr_number: Any) -> tuple[str, str]:
        """Get the merge state and status of a PR."""


@runtime_checkable
class StatusTrackerProtocol(Protocol):
    """Structural contract for the slot tracker (StatusTracker).

    Full public surface of StatusTracker (status_tracker.py): acquire_slot:31,
    release_slot:54, update_slot:68, get_status:82, get_active_count:92,
    wait_for_available:102, wait_all_complete:118, clear:134.
    """

    def acquire_slot(self, timeout: float | None = ...) -> int | None:
        """Acquire a processing slot, blocking until one is available."""

    def release_slot(self, slot_id: int) -> None:
        """Release a previously acquired slot."""

    def update_slot(self, slot_id: int, status: str) -> None:
        """Update the status of a slot."""

    def get_status(self) -> list[str | None]:
        """Get the current status of all slots."""

    def get_active_count(self) -> int:
        """Get the count of active slots."""

    def wait_for_available(self, timeout: float | None = ...) -> bool:
        """Wait until a slot becomes available."""

    def wait_all_complete(self, timeout: float | None = ...) -> bool:
        """Wait until all slots are released."""

    def clear(self) -> None:
        """Clear all slots."""


@runtime_checkable
class WorktreeManagerProtocol(Protocol):
    """Structural contract for git worktree lifecycle (WorktreeManager).

    Full public surface of WorktreeManager (worktree_manager.py): base_branch:107
    (property), refresh_base_branch:117, create_worktree:171, remove_worktree:552,
    get_worktree:605, cleanup_all:618, prune_worktrees:665, list_worktrees:699,
    ensure_branch_deleted:746.
    """

    @property
    def base_branch(self) -> str:
        """Get the base branch for worktree creation."""

    def refresh_base_branch(self) -> str:
        """Refresh the base branch and return its current name."""

    def create_worktree(self, *args: Any, **kwargs: Any) -> Any:
        """Create a new git worktree for an issue."""

    def remove_worktree(self, issue_number: int, force: bool = ...) -> None:
        """Remove a worktree for an issue."""

    def get_worktree(self, issue_number: int) -> Path | None:
        """Get the path to a worktree or None if not found."""

    def cleanup_all(self, force: bool = ...) -> None:
        """Clean up all worktrees."""

    def prune_worktrees(self) -> None:
        """Prune stale worktrees."""

    def list_worktrees(self, *, raise_on_error: bool = ...) -> list[dict[str, str]]:
        """List all worktrees."""

    def ensure_branch_deleted(self, branch_name: str) -> None:
        """Ensure a branch is deleted."""


@runtime_checkable
class PlannerStateProtocol(Protocol):
    """Structural contract for the planner-side state store.

    Full public surface of PlannerStateManager (planner_state.py): filter:79,
    get_cached_labels:167, prefetch_comments:178, get_cached_comments:209,
    has_existing_plan:224.
    """

    def filter(self) -> list[int]:
        """Filter and return matching issue numbers."""

    def get_cached_labels(self, issue_number: int) -> list[str] | None:
        """Get cached labels for an issue or None if not prefetched."""

    def prefetch_comments(self, issue_numbers: list[int]) -> None:
        """Prefetch comments for multiple issues."""

    def get_cached_comments(self, issue_number: int) -> list[dict[str, Any]] | None:
        """Get cached comments for an issue or None if not prefetched."""

    def has_existing_plan(self, issue_number: int) -> bool:
        """Check if a plan exists for an issue."""


@runtime_checkable
class ImplementerStateProtocol(Protocol):
    """Structural contract for the implementer-side state store.

    Full public surface of ImplementationStateManager (implementer_state.py):
    lock:54 (property), get_or_create:63, get:70, save:75, load_all:79.
    """

    @property
    def lock(self) -> Any:
        """Get the state store lock."""

    def get_or_create(self, issue_number: int) -> Any:
        """Get or create state for an issue."""

    def get(self, issue_number: int) -> Any | None:
        """Get state for an issue or None if not found."""

    def save(self, state: Any) -> None:
        """Save state."""

    def load_all(self) -> None:
        """Load all state."""


# The two state managers expose DISJOINT public surfaces (verified:
# planner=filter/get_cached_*; implementer=lock/get/save/load_all), so a single
# structural Protocol cannot match both. StateStoreProtocol is a real union
# TypeAlias — usable as a type hint (``def f(s: StateStoreProtocol)``) and in
# ``isinstance(x, (PlannerStateProtocol, ImplementerStateProtocol))``.
StateStoreProtocol: TypeAlias = "PlannerStateProtocol | ImplementerStateProtocol"
