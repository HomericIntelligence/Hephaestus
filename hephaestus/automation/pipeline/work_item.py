"""Work item and history tracking. Pure data, zero I/O (epic #1809).

Thread-safety: a WorkItem and its associated StageQueue are only ever touched
by the coordinator thread. The single cross-thread channel is CompletionQueue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from .routing import StageName

if TYPE_CHECKING:
    pass


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
    attempts: dict[str, int] = field(
        default_factory=lambda: {
            "plan": 0,
            "plan_review_iter": 0,
            "plan_cycles": 0,
            "pr_review_iter": 0,
            "pr_review_hard": 0,
            "test_fix": 0,
            "ci_fix": 0,
            "blocked_address": 0,
            "rebase": 0,
            "merge": 0,
        }
    )
    history: list[HistoryEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    worktree: str = ""
    branch: str = ""
    session_ids: list[str] = field(default_factory=list)
    labels_cache: dict[str, bool] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    result: ItemResult | None = None

    def add_history_event(self, stage: StageName, state: str, note: str = "") -> None:
        """Record a stage transition in the history (capped at 200 events)."""
        event = HistoryEvent(timestamp=datetime.now(), stage=stage, state=state, note=note)
        self.history.append(event)
        if len(self.history) > 200:
            self.history.pop(0)
