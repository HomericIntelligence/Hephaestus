"""Shared merge-wait admission checks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MergeWaitBaseSnapshot:
    """Validated default and pull-request base branches for one admission."""

    default_branch: str
    base_branch: str


def validate_merge_wait_base(
    pr_state: object, default_branch: object
) -> MergeWaitBaseSnapshot | str:
    """Validate the PR base against the verified repository default branch.

    Args:
        pr_state: Live PR state returned by the repository-scoped accessor.
        default_branch: Live repository default branch metadata.

    Returns:
        A validated base snapshot, or the shared terminal admission reason.

    """
    if (
        not isinstance(default_branch, str)
        or not default_branch
        or default_branch != default_branch.strip()
    ):
        return "default_branch_unavailable"
    if not isinstance(pr_state, dict):
        return "pr_state_unverified"
    base_branch = pr_state.get("baseRefName")
    if not isinstance(base_branch, str) or not base_branch or base_branch != base_branch.strip():
        return "pr_state_unverified"
    if base_branch != default_branch:
        return "non_default_base"
    return MergeWaitBaseSnapshot(default_branch, base_branch)


__all__ = ["MergeWaitBaseSnapshot", "validate_merge_wait_base"]
