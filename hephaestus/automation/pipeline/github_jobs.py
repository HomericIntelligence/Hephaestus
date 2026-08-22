"""Closed immutable requests and receipts for worker-owned GitHub I/O.

Only the value objects in this module may cross the coordinator/worker GitHub
boundary.  Nested service data is represented as canonical JSON so neither a
stage nor a worker can retain a shared mutable response object.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_JOURNAL_MARKER_RE = re.compile(
    r"<!-- hephaestus-implementation-reply-handoff:"
    r"pr=[1-9][0-9]*:head=[0-9a-f]{40}:batch=[0-9a-f]{32} -->"
)


@dataclass(frozen=True)
class FrozenJson:
    """Canonical immutable snapshot of JSON-compatible service data."""

    encoded: str

    @classmethod
    def snapshot(cls, value: object) -> Self:
        """Deep-copy *value* into a deterministic immutable representation."""
        return cls(
            encoded=json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def __post_init__(self) -> None:
        """Reject invalid or non-canonical encoded values."""
        try:
            value = json.loads(self.encoded)
            canonical = json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("FrozenJson must contain canonical JSON") from error
        if canonical != self.encoded:
            raise ValueError("FrozenJson must contain canonical JSON")

    def thaw(self) -> object:
        """Return a fresh mutable decode without exposing shared state."""
        return json.loads(self.encoded)


def _positive_identifier(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _full_sha(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a full lowercase commit SHA")


def _json_root(value: FrozenJson, expected: type[object], field_name: str) -> None:
    if not isinstance(value, FrozenJson) or not isinstance(value.thaw(), expected):
        raise ValueError(f"{field_name} must contain a JSON {expected.__name__}")


@dataclass(frozen=True)
class RecoverReplyJournalRequest:
    """Recover a version-one reply journal for exact source threads."""

    issue_number: int
    pr_number: int
    threads: FrozenJson

    def __post_init__(self) -> None:
        """Validate identifiers and the frozen thread snapshot."""
        _positive_identifier(self.issue_number, "issue_number")
        _positive_identifier(self.pr_number, "pr_number")
        _json_root(self.threads, list, "threads")


@dataclass(frozen=True)
class AppendReplyJournalRequest:
    """Append one replay-safe exact reply journal entry."""

    issue_number: int
    marker: str
    body: str

    def __post_init__(self) -> None:
        """Validate the replay-safe journal marker and body."""
        _positive_identifier(self.issue_number, "issue_number")
        if not isinstance(self.marker, str) or _JOURNAL_MARKER_RE.fullmatch(self.marker) is None:
            raise ValueError("marker must be an exact implementation reply journal marker")
        if not isinstance(self.body, str) or not self.body.startswith(f"{self.marker}\n<!-- "):
            raise ValueError("body must contain the exact journal marker and payload")


@dataclass(frozen=True)
class DeliverReplyHandoffRequest:
    """Deliver one already-journaled exact reply batch."""

    issue_number: int
    pr_number: int
    handoff: FrozenJson
    visibility_retries: int

    def __post_init__(self) -> None:
        """Validate identifiers, handoff shape, and retry count."""
        _positive_identifier(self.issue_number, "issue_number")
        _positive_identifier(self.pr_number, "pr_number")
        _json_root(self.handoff, dict, "handoff")
        if (
            isinstance(self.visibility_retries, bool)
            or not isinstance(self.visibility_retries, int)
            or self.visibility_retries < 0
        ):
            raise ValueError("visibility_retries must be a non-negative integer")


@dataclass(frozen=True)
class ReconcilePrReviewRequest:
    """Reconcile one exact-head review using fresh GitHub facts."""

    pr_number: int
    reviewed_head_sha: str
    validated_receipt_fingerprints: FrozenJson | None
    validated_metadata_fingerprint: str | None
    resolved_thread_ids: tuple[str, ...]
    feedback: FrozenJson
    findings: FrozenJson
    review_diff: str
    issue_number: int | None = None

    def __post_init__(self) -> None:
        """Validate the exact-head reconciliation request."""
        _positive_identifier(self.pr_number, "pr_number")
        _full_sha(self.reviewed_head_sha, "reviewed_head_sha")
        if self.validated_receipt_fingerprints is not None:
            _json_root(
                self.validated_receipt_fingerprints,
                dict,
                "validated_receipt_fingerprints",
            )
        if self.validated_metadata_fingerprint is not None and not isinstance(
            self.validated_metadata_fingerprint, str
        ):
            raise ValueError("validated_metadata_fingerprint must be a string or None")
        if not isinstance(self.resolved_thread_ids, tuple) or not all(
            isinstance(thread_id, str) and thread_id for thread_id in self.resolved_thread_ids
        ):
            raise ValueError("resolved_thread_ids must be a tuple of non-empty strings")
        _json_root(self.feedback, dict, "feedback")
        _json_root(self.findings, list, "findings")
        if not isinstance(self.review_diff, str):
            raise ValueError("review_diff must be a string")
        if self.issue_number is not None:
            _positive_identifier(self.issue_number, "issue_number")


@dataclass(frozen=True)
class RunMergeWaitCycleRequest:
    """Run one exact-head merge admission and conditional-request cycle."""

    pr_number: int
    reviewed_head_sha: str
    proof_generation: int
    declined_readiness_fingerprint: tuple[str, ...] | None
    issue_number: int | None = None

    def __post_init__(self) -> None:
        """Validate the exact-head merge proof and readiness fingerprint."""
        _positive_identifier(self.pr_number, "pr_number")
        _full_sha(self.reviewed_head_sha, "reviewed_head_sha")
        if (
            isinstance(self.proof_generation, bool)
            or not isinstance(self.proof_generation, int)
            or self.proof_generation < 0
        ):
            raise ValueError("proof_generation must be a non-negative integer")
        fingerprint = self.declined_readiness_fingerprint
        if fingerprint is not None and (
            not isinstance(fingerprint, tuple)
            or not all(isinstance(part, str) for part in fingerprint)
        ):
            raise ValueError("declined_readiness_fingerprint must be a tuple of strings or None")
        if self.issue_number is not None:
            _positive_identifier(self.issue_number, "issue_number")


type GitHubRequest = (
    RecoverReplyJournalRequest
    | AppendReplyJournalRequest
    | DeliverReplyHandoffRequest
    | ReconcilePrReviewRequest
    | RunMergeWaitCycleRequest
)


@dataclass(frozen=True)
class GitHubJob:
    """One closed GitHub operation submitted to the worker pool."""

    repo: str
    repo_root: Path
    request: GitHubRequest
    descr: str

    def __post_init__(self) -> None:
        """Validate the closed job envelope."""
        if not isinstance(self.repo, str) or not self.repo:
            raise ValueError("repo must be a non-empty string")
        if not isinstance(self.repo_root, Path) or not self.repo_root.is_absolute():
            raise ValueError("repo_root must be an absolute Path")
        if not isinstance(
            self.request,
            (
                RecoverReplyJournalRequest,
                AppendReplyJournalRequest,
                DeliverReplyHandoffRequest,
                ReconcilePrReviewRequest,
                RunMergeWaitCycleRequest,
            ),
        ):
            raise TypeError("request must be a supported GitHub request")
        if not isinstance(self.descr, str) or not self.descr:
            raise ValueError("descr must be a non-empty string")


def github_request_issue(request: GitHubRequest) -> int | None:
    """Return the issue target carried by a closed request, if present."""
    value = getattr(request, "issue_number", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def github_request_pr(request: GitHubRequest) -> int | None:
    """Return the pull-request target carried by a closed request, if present."""
    value = getattr(request, "pr_number", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class ReplyJournalRecovered:
    """Receipt for a journal recovery read."""

    request: RecoverReplyJournalRequest
    handoff: FrozenJson | None

    def __post_init__(self) -> None:
        """Validate the optional recovered handoff snapshot."""
        if self.handoff is not None:
            _json_root(self.handoff, dict, "handoff")


@dataclass(frozen=True)
class ReplyJournalAppended:
    """Receipt for a durable replay-safe journal append."""

    request: AppendReplyJournalRequest


@dataclass(frozen=True)
class ReplyHandoffAttempted:
    """Detached receipt for one exact reply-delivery attempt."""

    request: DeliverReplyHandoffRequest
    status: Literal["completed", "visibility_wait", "stale", "invalid", "blocked", "retry"]
    remaining_handoff: FrozenJson | None
    visibility_retries: int
    retry_delay_s: float | None

    def __post_init__(self) -> None:
        """Validate detached retry and handoff state."""
        if self.status not in {
            "completed",
            "visibility_wait",
            "stale",
            "invalid",
            "blocked",
            "retry",
        }:
            raise ValueError("status must be a supported reply-handoff outcome")
        if self.remaining_handoff is not None:
            _json_root(self.remaining_handoff, dict, "remaining_handoff")
        if self.status in {"completed", "stale", "blocked"} and self.remaining_handoff is not None:
            raise ValueError("terminal handoff outcomes cannot retain mutation work")
        if (
            isinstance(self.visibility_retries, bool)
            or not isinstance(self.visibility_retries, int)
            or self.visibility_retries < 0
        ):
            raise ValueError("visibility_retries must be a non-negative integer")
        if self.retry_delay_s is not None and (
            isinstance(self.retry_delay_s, bool)
            or not isinstance(self.retry_delay_s, (int, float))
            or self.retry_delay_s < 0
        ):
            raise ValueError("retry_delay_s must be a non-negative number or None")


@dataclass(frozen=True)
class PrReviewReconciled:
    """Immutable result of fresh PR-review reconciliation."""

    request: ReconcilePrReviewRequest
    action: Literal["apply", "revalidate", "fresh_review", "audit_failure"]
    posted_receipts: FrozenJson
    unresolved_threads: FrozenJson
    remediation_threads: FrozenJson

    def __post_init__(self) -> None:
        """Validate immutable review response snapshots."""
        if self.action not in {"apply", "revalidate", "fresh_review", "audit_failure"}:
            raise ValueError("action must be a supported PR-review outcome")
        _json_root(self.posted_receipts, list, "posted_receipts")
        _json_root(self.unresolved_threads, list, "unresolved_threads")
        _json_root(self.remediation_threads, list, "remediation_threads")


@dataclass(frozen=True)
class MergeWaitCycleCompleted:
    """Immutable outcome of one merge admission/request cycle."""

    request: RunMergeWaitCycleRequest
    outcome: str
    attempted: bool
    readiness_fingerprint: tuple[str, ...] | None = None
    retryable: bool = False
    merge_sha: str | None = None

    def __post_init__(self) -> None:
        """Validate merge-cycle outcome metadata."""
        if not isinstance(self.outcome, str) or not self.outcome:
            raise ValueError("outcome must be a non-empty string")
        if not isinstance(self.attempted, bool) or not isinstance(self.retryable, bool):
            raise ValueError("attempted and retryable must be booleans")
        if self.readiness_fingerprint is not None and (
            not isinstance(self.readiness_fingerprint, tuple)
            or not all(isinstance(part, str) for part in self.readiness_fingerprint)
        ):
            raise ValueError("readiness_fingerprint must be a tuple of strings or None")
        if self.merge_sha is not None and (
            not isinstance(self.merge_sha, str)
            or len(self.merge_sha) not in (40, 64)
            or any(character not in "0123456789abcdef" for character in self.merge_sha)
        ):
            raise ValueError("merge_sha must be a full commit SHA or None")


type GitHubReceipt = (
    ReplyJournalRecovered
    | ReplyJournalAppended
    | ReplyHandoffAttempted
    | PrReviewReconciled
    | MergeWaitCycleCompleted
)


class GitHubJobRunner(Protocol):
    """Executes closed GitHub requests with job-scoped accessors."""

    def run(self, job: GitHubJob) -> GitHubReceipt:
        """Execute one closed GitHub request and return its immutable receipt."""
        raise NotImplementedError
