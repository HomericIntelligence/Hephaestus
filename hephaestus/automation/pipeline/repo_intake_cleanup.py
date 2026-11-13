"""Typed cleanup results for launch-owned repository intakes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RepoIntakeCleanupFailure:
    """One launch intake that cleanup preserved instead of removing.

    The coordinator reports the retained path and the failure without
    changing the computed run exit code. Operators recover the retained
    path with the corrupted-worktree runbook.
    """

    path: str
    error: str


__all__ = ["RepoIntakeCleanupFailure"]
