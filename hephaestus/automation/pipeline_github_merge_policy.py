"""Immutable effective merge-policy values and derived queue limits."""

from __future__ import annotations

from dataclasses import dataclass

from .pipeline_github_merge_rules import RequiredCheck


@dataclass(frozen=True)
class EffectiveMergePolicy:
    """Stable effective merge policy for one exact repository base branch.

    ``strict_update_enforced`` is true only when at least one applicable policy
    source enforces strict updates for the current actor.
    """

    base_branch: str
    default_branch: str
    required_checks: tuple[RequiredCheck, ...]
    conversation_resolution_enforced: bool
    bypassable_ruleset_ids: tuple[int, ...]
    strict_update_enforced: bool = False
    merge_queue_method: str | None = None
    check_response_timeout_minutes: int | None = None
    min_entries_to_merge_wait_minutes: int | None = None

    @property
    def merge_queue_required(self) -> bool:
        """Return whether server policy requires the merge queue."""
        return self.merge_queue_method is not None

    @property
    def merge_queue_residence_timeout_s(self) -> float | None:
        """Return the complete server queue wait window in seconds."""
        check_timeout = self.check_response_timeout_minutes
        minimum_wait = self.min_entries_to_merge_wait_minutes
        if (
            type(check_timeout) is not int
            or check_timeout <= 0
            or type(minimum_wait) is not int
            or minimum_wait < 0
        ):
            return None
        return float((check_timeout + minimum_wait) * 60)
