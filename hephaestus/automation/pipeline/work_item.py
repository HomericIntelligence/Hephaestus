"""Work item and history tracking. Pure data, zero I/O (epic #1809).

Thread-safety: a WorkItem and its associated StageQueue are only ever touched
by the coordinator thread. The single cross-thread channel is CompletionQueue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .routing import StageName

#: Maximum retained history events per item (oldest dropped first).
HISTORY_CAP = 200


def _utcnow() -> datetime:
    """Return the current tz-aware UTC time (automation-layer convention)."""
    return datetime.now(timezone.utc)


def _default_attempts() -> dict[str, int]:
    """Return zeroed per-item-lifetime counters, one per ROUTES budget key.

    Keys mirror the union of budget keys in ``routing.ROUTES``;
    ``tests/unit/automation/pipeline/test_work_item.py`` pins the two in
    lockstep so they cannot drift.
    """
    return {
        "clone": 0,
        "plan": 0,
        "plan_review_iter": 0,
        "plan_cycles": 0,
        "implement": 0,
        "test_fix": 0,
        "pr_review_iter": 0,
        "pr_review_hard": 0,
        "ci_fix": 0,
        "rebase": 0,
        "blocked_address": 0,
        "merge": 0,
    }


class ItemKind(str, Enum):
    """Work item type."""

    REPO = "repo"
    ISSUE = "issue"
    PR = "pr"


@dataclass(frozen=True)
class HistoryEvent:
    """A point-in-time stage-state snapshot."""

    timestamp: datetime
    stage: StageName
    state: str
    note: str = ""


@dataclass(frozen=True)
class ItemResult:
    """Final outcome of a work item."""

    passed: bool
    reason: str
    final_stage: StageName


@dataclass
class WorkItem:
    """A unit of work flowing through the pipeline.

    A WorkItem represents a repo, issue, or PR being processed through the
    pipeline. All access is single-threaded (coordinator thread only).
    """

    repo: str
    kind: ItemKind
    issue: int | None = None
    pr: int | None = None
    stage: StageName = StageName.REPO
    state: str = ""
    attempts: dict[str, int] = field(default_factory=_default_attempts)
    history: list[HistoryEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    worktree: str = ""
    branch: str = ""
    session_ids: dict[str, str] = field(default_factory=dict)
    labels_cache: dict[str, bool] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    result: ItemResult | None = None

    def add_history_event(self, stage: StageName, state: str, note: str = "") -> None:
        """Record a stage transition in the history (capped at HISTORY_CAP events)."""
        event = HistoryEvent(timestamp=_utcnow(), stage=stage, state=state, note=note)
        self.history.append(event)
        if len(self.history) > HISTORY_CAP:
            self.history.pop(0)
        self.updated_at = event.timestamp
