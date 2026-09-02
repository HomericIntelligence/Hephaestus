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
