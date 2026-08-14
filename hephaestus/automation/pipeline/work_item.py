"""Work item and history tracking. Pure data, zero I/O (epic #1809).

Thread-safety: a WorkItem and its associated StageQueue are only ever touched
by the coordinator thread. The bounded main and auxiliary completion queues
are the only cross-thread payload channels.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any

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


class ItemKind(StrEnum):
    """Work item type."""

    REPO = "repo"
    ISSUE = "issue"
    PR = "pr"


class LearningIntentKind(StrEnum):
    """Supported auxiliary learning sources."""

    APPROVED_PLAN = "approved_plan"
    POST_MERGE = "post_merge"


@dataclass(frozen=True)
class LearningIntent:
    """Immutable identity and bounded input for one learning operation."""

    kind: LearningIntentKind
    repo: str
    issue: int
    pr: int | None = None
    plan_revision: int | None = None
    plan_fingerprint: str = ""

    @property
    def key(self) -> str:
        """Return a deterministic identity key without persisting content."""
        identity = {
            "issue": self.issue,
            "kind": self.kind.value,
            "plan_fingerprint": self.plan_fingerprint,
            "plan_revision": self.plan_revision,
            "pr": self.pr,
            "repo": self.repo,
        }
        digest = sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        return f"{self.kind.value}:{digest}"

    def journal_identity(self) -> dict[str, object]:
        """Return the bounded identity fields needed for restart recovery."""
        return {
            "repo": self.repo,
            "issue": self.issue,
            "pr": self.pr,
            "plan_revision": self.plan_revision,
            "plan_fingerprint": self.plan_fingerprint,
        }

    def to_payload(self) -> dict[str, object]:
        """Return the closed semantic request accepted by the learning host."""
        payload: dict[str, object] = {
            "kind": self.kind.value,
            "repo": self.repo,
            "issue": self.issue,
            "intent_key": self.key,
        }
        if self.kind is LearningIntentKind.APPROVED_PLAN:
            payload.update(
                {
                    "plan_revision": self.plan_revision,
                    "plan_fingerprint": self.plan_fingerprint,
                }
            )
        else:
            payload["pr"] = self.pr
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> LearningIntent:
        """Parse a complete learning intent without accepting extra fields."""
        kind_raw = payload.get("kind")
        try:
            kind = LearningIntentKind(kind_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("learning intent payload has invalid kind") from exc
        common = {"kind", "repo", "issue", "intent_key"}
        expected = common | (
            {"plan_revision", "plan_fingerprint"}
            if kind is LearningIntentKind.APPROVED_PLAN
            else {"pr"}
        )
        if set(payload) != expected:
            raise ValueError("learning intent payload has unsupported or missing fields")
        repo = payload.get("repo")
        issue = payload.get("issue")
        intent_key = payload.get("intent_key")
        if (
            not isinstance(repo, str)
            or len(repo) > 200
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None
            or isinstance(issue, bool)
            or not isinstance(issue, int)
            or issue <= 0
            or not isinstance(intent_key, str)
            or not intent_key
        ):
            raise ValueError("learning intent payload has invalid identity fields")
        if kind is LearningIntentKind.APPROVED_PLAN:
            revision = payload.get("plan_revision")
            fingerprint = payload.get("plan_fingerprint")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision <= 0
                or not isinstance(fingerprint, str)
                or not fingerprint
            ):
                raise ValueError("learning intent payload has invalid approved-plan fields")
            intent = cls.approved_plan(
                repo=repo,
                issue=issue,
                plan_revision=revision,
                plan_fingerprint=fingerprint,
            )
        else:
            pr = payload.get("pr")
            if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
                raise ValueError("learning intent payload has invalid post-merge fields")
            intent = cls.post_merge(repo=repo, issue=issue, pr=pr)
        if intent.key != intent_key:
            raise ValueError("learning intent payload identity does not match its key")
        return intent

    @classmethod
    def from_journal(cls, record: dict[str, Any]) -> LearningIntent:
        """Rebuild an intent from a validated journal record."""
        intent = cls(
            kind=LearningIntentKind(str(record["kind"])),
            repo=str(record["repo"]),
            issue=int(record["issue"]),
            pr=int(record["pr"]) if record.get("pr") is not None else None,
            plan_revision=(
                int(record["plan_revision"]) if record.get("plan_revision") is not None else None
            ),
            plan_fingerprint=str(record.get("plan_fingerprint", "")),
        )
        if intent.key != record.get("key"):
            raise ValueError("learning journal identity does not match its key")
        return intent

    @classmethod
    def approved_plan(
        cls,
        *,
        repo: str,
        issue: int,
        plan_revision: int,
        plan_fingerprint: str,
    ) -> LearningIntent:
        """Build an approved-plan learning identity."""
        return cls(
            kind=LearningIntentKind.APPROVED_PLAN,
            repo=repo,
            issue=issue,
            plan_revision=plan_revision,
            plan_fingerprint=plan_fingerprint,
        )

    @classmethod
    def post_merge(cls, *, repo: str, issue: int, pr: int) -> LearningIntent:
        """Build a post-merge learning identity."""
        return cls(kind=LearningIntentKind.POST_MERGE, repo=repo, issue=issue, pr=pr)


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
    # Pi bindings are deliberately separate from legacy raw session ids: they
    # bind a provider session to its worktree, role, and model fingerprint.
    session_bindings: dict[str, Any] = field(default_factory=dict)
    labels_cache: dict[str, bool] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    learning_intents: list[LearningIntent] = field(default_factory=list)
    learning_resume_stage: StageName | None = None
    post_processing: PostProcessingRecord | None = None
    result: ItemResult | None = None

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
