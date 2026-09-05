"""Provider-neutral Git job specifications for pipeline workers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

GIT_OPS: frozenset[str] = frozenset(
    {
        "clone",
        "sync_checkout",
        "verify_issue_wave_ancestry",
        "create_worktree",
        "inspect_implementation_worktree",
        "recover_dirty_worktree",
        "verify_pr_review_checkout",
        "remove_worktree",
        "rebase",
        "continue_rebase",
        "push",
        "commit_push",
        "release_branch_reservation",
    }
)

WORKTREE_MATERIALIZED_KEY = "worktree_materialized"

# These limits bound data from an implementation writer before the data enters
# coordinator state or an agent prompt.
IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES = 64 * 1024
IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES = 256 * 1024
DIRTY_SNAPSHOT_GIT_MAX_BYTES = 4 * 1024 * 1024
DIRTY_SNAPSHOT_CONTENT_MAX_BYTES = 8 * 1024 * 1024
DIRTY_SNAPSHOT_CHANGED_FILE_MAX = 512


@dataclass(frozen=True)
class GitJob:
    """Request one allowlisted Git operation."""

    repo: str
    op: str
    timeout_s: int
    kwargs: dict[str, Any] = field(default_factory=dict)
    descr: str = ""
    # Pipeline scheduling uses a repository-local key.  Authenticated Git
    # transport validates a separate canonical OWNER/REPOSITORY identity.
    expected_repository: str | None = None

    def __post_init__(self) -> None:
        """Reject an operation outside the closed Git vocabulary."""
        if self.op not in GIT_OPS:
            raise ValueError(f"unknown git op {self.op!r}; expected one of {sorted(GIT_OPS)}")

    @property
    def transport_repository(self) -> str:
        """Return the canonical identity required for authenticated Git transport."""
        return self.expected_repository or self.repo
