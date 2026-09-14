"""Validate repository and branch identity for one merge-wait cycle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeGuard


def _exact_nonblank(value: object) -> TypeGuard[str]:
    """Return whether a value is a nonblank string without outer padding."""
    return isinstance(value, str) and bool(value) and value == value.strip()


class RequiredChecksDeferred(Enum):
    """Required checks need another bounded read before admission."""

    PENDING = "pending"
    UNSTABLE = "unstable"


@dataclass(frozen=True)
class VerifiedRepositoryDefaultBranch:
    """Repository identity and default branch from one validated GitHub read."""

    owner: str
    name: str
    name_with_owner: str
    default_branch: str

    def __post_init__(self) -> None:
        """Reject incomplete or inconsistent repository metadata."""
        if (
            not _exact_nonblank(self.owner)
            or not _exact_nonblank(self.name)
            or "/" in self.owner
            or "/" in self.name
            or self.name_with_owner != f"{self.owner}/{self.name}"
            or not _exact_nonblank(self.default_branch)
        ):
            raise ValueError("repository default-branch metadata was malformed")


@dataclass(frozen=True)
class MergeWaitAdmissionSnapshot:
    """Immutable repository, branch, and head facts for merge admission."""

    repository: VerifiedRepositoryDefaultBranch
    base_branch: str
    merge_head_sha: str


def validate_merge_wait_admission(
    pr_state: object,
    repository: object,
    expected_head_sha: str,
    *,
    initial: MergeWaitAdmissionSnapshot | None = None,
) -> MergeWaitAdmissionSnapshot | str:
    """Validate initial admission or compare a final snapshot with it."""
    if not isinstance(repository, VerifiedRepositoryDefaultBranch):
        return "default_branch_unavailable"
    if not isinstance(pr_state, dict):
        return "pr_state_unverified"
    base_branch = pr_state.get("baseRefName")
    if not _exact_nonblank(base_branch):
        return "pr_state_unverified"
    merge_head_sha = pr_state.get("headRefOid")
    if not _exact_nonblank(merge_head_sha):
        return "missing_pr_head"
    snapshot = MergeWaitAdmissionSnapshot(repository, base_branch, merge_head_sha)
    if initial is None:
        if base_branch != repository.default_branch:
            return "non_default_base"
        if merge_head_sha != expected_head_sha:
            return "reviewed_head_drift"
        return snapshot
    if (
        repository.owner,
        repository.name,
        repository.name_with_owner,
    ) != (
        initial.repository.owner,
        initial.repository.name,
        initial.repository.name_with_owner,
    ):
        return "repository_identity_drift"
    if repository.default_branch != initial.repository.default_branch:
        return "default_branch_drift"
    if base_branch != initial.base_branch:
        return "pr_base_drift"
    if merge_head_sha != initial.merge_head_sha or merge_head_sha != expected_head_sha:
        return "reviewed_head_drift"
    return snapshot


__all__ = [
    "MergeWaitAdmissionSnapshot",
    "RequiredChecksDeferred",
    "VerifiedRepositoryDefaultBranch",
    "validate_merge_wait_admission",
]
