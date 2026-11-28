"""Work item and history tracking. Pure data, zero I/O (epic #1809).

Thread-safety: a WorkItem and its associated StageQueue are only ever touched
by the coordinator thread. The bounded main and auxiliary completion queues
are the only cross-thread payload channels.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from hephaestus.automation.learning_intent import (
    LearningIntent as LearningIntent,
    LearningIntentKind as LearningIntentKind,
)

from .routing import StageName, budget_keys

#: Maximum retained history events per item (oldest dropped first).
HISTORY_CAP = 200

type PreservedWorktree = tuple[str, int, str]
"""Repository, issue/PR number, and path for a preserved worktree."""


def _utcnow() -> datetime:
    """Return the current tz-aware UTC time (automation-layer convention)."""
    return datetime.now(UTC)


def _default_attempts() -> dict[str, int]:
    """Return zeroed per-item-lifetime counters, one per budget key."""
    return dict.fromkeys(budget_keys(), 0)


class PhaseActivity(StrEnum):
    """Bounded classification of one work-item phase window."""

    ACTIVE = "active"
    WAITING = "waiting"


class RetryCause(StrEnum):
    """Closed set of reasons why a work item retries a phase."""

    NONE = "none"
    SOURCE_CHANGE = "source_change"
    REVIEW_FINDING = "review_finding"
    TEST_FAILURE = "test_failure"
    HOSTED_CHECK_FAILURE = "hosted_check_failure"
    REMOTE_MOVEMENT = "remote_movement"
    TOOL_FAILURE = "tool_failure"
    EXTERNAL_WAIT = "external_wait"


_FULL_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SOURCE_IDENTITY_KEYS = (
    "_impl_source_revision",
    "_direct_scope_base_sha",
    "_worktree_cleanup_head_sha",
)


def _utc_iso(value: datetime) -> str:
    """Format one timezone-aware datetime as ISO-8601 UTC milliseconds."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class TimingReceipt:
    """One bounded timing and retry fact record for a work item."""

    phase: str
    start: str
    end: str
    elapsed_s: float
    activity: PhaseActivity
    attempts: int
    retry_cause: RetryCause

    @classmethod
    def create(
        cls,
        *,
        phase: str,
        started: datetime,
        ended: datetime,
        activity: PhaseActivity | str,
        attempts: int,
        retry_cause: RetryCause | str,
    ) -> TimingReceipt:
        """Build one validated receipt from a start and an end timestamp.

        Args:
            phase: Pipeline stage name of the observed window.
            started: Timezone-aware start of the window.
            ended: Timezone-aware end of the window.
            activity: ``active`` or ``waiting`` classification.
            attempts: Total attempt count on the work item.
            retry_cause: One value from the closed :class:`RetryCause` set.

        Returns:
            One bounded receipt. ``elapsed_s`` is never negative.

        Raises:
            ValueError: If a field is outside its closed or bounded domain.

        """
        if phase not in {stage.value for stage in StageName}:
            raise ValueError("timing receipt phase is not a pipeline stage")
        if started.tzinfo is None or ended.tzinfo is None:
            raise ValueError("timing receipt timestamps must be timezone aware")
        try:
            activity_value = PhaseActivity(activity)
            cause_value = RetryCause(retry_cause)
        except ValueError as exc:
            raise ValueError(
                "timing receipt activity or retry cause is outside its closed set"
            ) from exc
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ValueError("timing receipt attempt count must be a nonnegative integer")
        elapsed_s = round(max(0.0, (ended - started).total_seconds()), 3)
        return cls(
            phase=phase,
            start=_utc_iso(started),
            end=_utc_iso(ended),
            elapsed_s=elapsed_s,
            activity=activity_value,
            attempts=attempts,
            retry_cause=cause_value,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the closed, machine-readable representation of this receipt."""
        return {
            "phase": self.phase,
            "start": self.start,
            "end": self.end,
            "elapsed_s": self.elapsed_s,
            "activity": self.activity.value,
            "attempts": self.attempts,
            "retry_cause": self.retry_cause.value,
        }


class ItemKind(StrEnum):
    """Work item type."""

    REPO = "repo"
    ISSUE = "issue"
    PR = "pr"


_POST_PROCESSING_PAYLOAD_KEYS = frozenset(
    {
        "_direct_scope_local_branch_cleanup",
        "_direct_scope_base_sha",
        "_direct_scope_reservation",
        "_impl_source_revision",
        "_learning_primary_reason",
        "_worktree_cleanup_head_sha",
        "_wave_lease",
        "detached_push_failure",
        "dirty_direct_preserve",
        "learning_failures",
    }
)


@dataclass(frozen=True)
class PostProcessingRecord:
    """Compact terminal state retained by the auxiliary lane."""

    result: ItemResult
    resume_stage: StageName
    intent_keys: tuple[str, ...]
    cleanup_payload: dict[str, Any]


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

    Note: `history` is a `deque` (not a list), supporting iteration, indexing
    (`[0]`, `[-1]`), and `append`, but not `sort()`.
    """

    repo: str
    kind: ItemKind
    issue: int | None = None
    pr: int | None = None
    stage: StageName = StageName.REPO
    state: str = ""
    attempts: dict[str, int] = field(default_factory=_default_attempts)
    history: deque[HistoryEvent] = field(default_factory=lambda: deque(maxlen=HISTORY_CAP))
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    worktree: str = ""
    branch: str = ""
    session_ids: dict[str, str] = field(default_factory=dict)
    session_selections: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Pi bindings are deliberately separate from legacy raw session ids: they
    # bind a provider session to its worktree, role, and model fingerprint.
    session_bindings: dict[str, Any] = field(default_factory=dict)
    labels_cache: dict[str, bool] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    learning_intents: list[LearningIntent] = field(default_factory=list)
    learning_resume_stage: StageName | None = None
    post_processing: PostProcessingRecord | None = None
    result: ItemResult | None = None
    timing_receipt: TimingReceipt | None = None

    def learning_journal_identity(self, intent: LearningIntent) -> dict[str, object]:
        """Return restart identity plus bounded terminal cleanup state."""
        identity = intent.journal_identity()
        if self.post_processing is None:
            return identity
        result = self.post_processing.result
        identity["post_processing"] = {
            "worktree": self.worktree,
            "branch": self.branch,
            "resume_stage": self.post_processing.resume_stage.value,
            "cleanup_payload": dict(self.post_processing.cleanup_payload),
            "result": {
                "passed": result.passed,
                "reason": result.reason,
                "final_stage": result.final_stage.value,
            },
        }
        return identity

    def restore_post_processing(self, record: dict[str, Any]) -> bool:
        """Restore validated terminal cleanup state from a journal record."""
        raw = record.get("post_processing")
        if not isinstance(raw, dict):
            return False
        cleanup = raw.get("cleanup_payload")
        result_raw = raw.get("result")
        if not isinstance(cleanup, dict) or not isinstance(result_raw, dict):
            raise ValueError("invalid post-processing journal state")
        unknown = set(cleanup).difference(_POST_PROCESSING_PAYLOAD_KEYS)
        if unknown:
            raise ValueError("post-processing journal has unsupported cleanup fields")
        passed = result_raw.get("passed")
        reason = result_raw.get("reason")
        final_stage = result_raw.get("final_stage")
        resume_stage_raw = raw.get("resume_stage")
        if (
            not isinstance(passed, bool)
            or not isinstance(reason, str)
            or not isinstance(final_stage, str)
            or not isinstance(resume_stage_raw, str)
        ):
            raise ValueError("post-processing journal has invalid result fields")
        result = ItemResult(
            passed=passed,
            reason=reason,
            final_stage=StageName(final_stage),
        )
        resume_stage = StageName(resume_stage_raw)
        self.worktree = str(raw.get("worktree") or "")
        self.branch = str(raw.get("branch") or "")
        self.payload.update(cleanup)
        self.result = result
        self.post_processing = PostProcessingRecord(
            result=result,
            resume_stage=resume_stage,
            intent_keys=tuple(intent.key for intent in self.learning_intents),
            cleanup_payload=dict(cleanup),
        )
        return True

    def compact_for_post_processing(self, result: ItemResult) -> None:
        """Drop stage-local payload after terminal main work completes."""
        cleanup_payload = {
            key: value
            for key, value in self.payload.items()
            if key in _POST_PROCESSING_PAYLOAD_KEYS
        }
        self.payload = cleanup_payload
        self.post_processing = PostProcessingRecord(
            result=result,
            resume_stage=StageName.FINISHED,
            intent_keys=tuple(intent.key for intent in self.learning_intents),
            cleanup_payload=dict(cleanup_payload),
        )

    def add_history_event(self, stage: StageName, state: str, note: str = "") -> None:
        """Record a stage transition in the history (capped at HISTORY_CAP events)."""
        event = HistoryEvent(timestamp=_utcnow(), stage=stage, state=state, note=note)
        self.history.append(event)
        self.updated_at = event.timestamp

    def record_timing(
        self,
        *,
        started: datetime,
        ended: datetime,
        activity: PhaseActivity | str = PhaseActivity.ACTIVE,
        retry_cause: RetryCause | str = RetryCause.NONE,
        phase: StageName | str | None = None,
    ) -> TimingReceipt:
        """Replace the item's single timing receipt with one bounded window."""
        phase_value = (
            phase.value if isinstance(phase, StageName) else str(phase or self.stage.value)
        )
        receipt = TimingReceipt.create(
            phase=phase_value,
            started=started,
            ended=ended,
            activity=activity,
            attempts=sum(self.attempts.values()),
            retry_cause=retry_cause,
        )
        self.timing_receipt = receipt
        return receipt


def exact_source_identity(item: WorkItem, *, source_revision: str | None = None) -> str | None:
    """Return one validated full commit SHA when an exact source identity exists."""
    candidates: list[object] = [source_revision]
    candidates.extend(item.payload.get(key) for key in _SOURCE_IDENTITY_KEYS)
    for candidate in candidates:
        if isinstance(candidate, str) and _FULL_SHA_PATTERN.fullmatch(candidate):
            return candidate
    return None


def sanitized_terminal_summary(
    item: WorkItem, *, source_revision: str | None = None
) -> dict[str, object]:
    """Return one bounded terminal fact record for a work item.

    The record names the issue, the PR, and the exact source identity. It
    carries only integers and a validated commit SHA. It never carries a
    prompt, a credential, a private path, a host name, an endpoint, source
    text, free-form output, or a timing receipt.
    """
    summary: dict[str, object] = {"issue": item.issue, "pr": item.pr}
    identity = exact_source_identity(item, source_revision=source_revision)
    if identity is not None:
        summary["source_identity"] = identity
    return summary
