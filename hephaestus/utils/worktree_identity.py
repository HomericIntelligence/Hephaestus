"""Canonical names and cleanup identities for managed source worktrees."""

from __future__ import annotations

from pathlib import Path

SOURCE_WORKTREE_LANES = frozenset({"impl", "review"})


def source_worktree_name(item_number: int, lane: str) -> str:
    """Return the deterministic name for one source-reading lane."""
    if item_number <= 0:
        raise ValueError("source worktree item number must be positive")
    if lane not in SOURCE_WORKTREE_LANES:
        raise ValueError("source worktree lane must be 'impl' or 'review'")
    return f"auto-{item_number}-{lane}"


def is_expected_managed_worktree_path(
    path: Path,
    *,
    repo_root: Path,
    issue_number: int,
) -> bool:
    """Return whether ``path`` is a cleanup-safe managed worktree identity."""
    if issue_number <= 0:
        return False
    try:
        resolved = path.resolve()
        root = repo_root.resolve()
    except OSError:
        return False
    issue_name = f"issue-{issue_number}"
    direct_prefix = f"{issue_name}-direct-"
    review_name = f"review-pr-{issue_number}"
    review_generation = resolved.name.removeprefix(f"{review_name}-")
    is_review_path = resolved.name == review_name or (
        resolved.name.startswith(f"{review_name}-")
        and review_generation.isdecimal()
        and int(review_generation) > 0
    )
    is_source_path = resolved.name in {
        source_worktree_name(issue_number, lane) for lane in SOURCE_WORKTREE_LANES
    }
    if (
        resolved.name != issue_name
        and not resolved.name.startswith(direct_prefix)
        and not is_review_path
        and not is_source_path
    ):
        return False
    return resolved.parent in {root, root / "build" / ".worktrees"}
