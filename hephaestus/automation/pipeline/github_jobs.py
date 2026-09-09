"""Closed immutable requests and receipts for worker-owned GitHub I/O.

Only the value objects in this module may cross the coordinator/worker GitHub
boundary.  Nested service data is represented as canonical JSON so neither a
stage nor a worker can retain a shared mutable response object.
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from hephaestus.automation.host_verification_bootstrap import (
    BootstrapProof,
    is_process_bootstrap_proof,
)
from hephaestus.automation.pipeline.scope_retraction import (
    scope_retraction_paths_from_body,
)
from hephaestus.automation.rebase_review_receipt import RebaseReviewRecord
from hephaestus.automation.scope_expansion_domain import (
    ScopeExpansion,
    normalize_scope_expansion,
)

from .rebase_review import RebaseReviewProof

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_JOURNAL_MARKER_RE = re.compile(
    r"<!-- hephaestus-implementation-reply-handoff:"
    r"pr=[1-9][0-9]*:head=[0-9a-f]{40}:batch=[0-9a-f]{32} -->"
)
_REMEDIATION_JOURNAL_MARKER_RE = re.compile(
    r"<!-- hephaestus-implementation-remediation-reply-handoff:"
    r"pr=[1-9][0-9]*:head=[0-9a-f]{40}(?:[0-9a-f]{24})?:"
    r"batch=[0-9a-f]{32}:seq=(?:0|[1-9][0-9]*) -->"
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


def _deadline(value: float | None) -> None:
    """Reject an invalid absolute monotonic operation deadline."""
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("deadline_s must be a finite positive monotonic time")


def _json_root(value: FrozenJson, expected: type[object], field_name: str) -> object:
    if not isinstance(value, FrozenJson):
        raise ValueError(f"{field_name} must contain a JSON {expected.__name__}")
    root = value.thaw()
    if not isinstance(root, expected):
        raise ValueError(f"{field_name} must contain a JSON {expected.__name__}")
    return root


@dataclass(frozen=True)
class ImplementationReplyProgress:
    """Store durable progress for one safe, incomplete reply batch."""

    phase: Literal[
        "create_review",
        "post_replies",
        "verify_reply",
        "submit_review",
        "verify_submission",
    ]
    pull_request_id: str
    pending_review_id: str | None = None
    replied_thread_ids: tuple[str, ...] = ()
    receipts: tuple[dict[str, Any], ...] = ()
    active_thread_id: str | None = None
    active_comment_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot for a handoff journal."""
        return {
            "phase": self.phase,
            "pull_request_id": self.pull_request_id,
            "pending_review_id": self.pending_review_id,
            "replied_thread_ids": list(self.replied_thread_ids),
            "receipts": [dict(receipt) for receipt in self.receipts],
            "active_thread_id": self.active_thread_id,
            "active_comment_id": self.active_comment_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> ImplementationReplyProgress | None:
        """Validate and restore progress that is in a handoff."""
        if not isinstance(value, dict):
            return None
        phase = value.get("phase")
        phases = {
            "create_review",
            "post_replies",
            "verify_reply",
            "submit_review",
            "verify_submission",
        }
        pull_request_id = value.get("pull_request_id")
        ids = value.get("replied_thread_ids", [])
        receipts = value.get("receipts", [])
        pending_review_id = value.get("pending_review_id")
        active_thread_id = value.get("active_thread_id")
        active_comment_id = value.get("active_comment_id")
        if (
            not isinstance(phase, str)
            or phase not in phases
            or not isinstance(pull_request_id, str)
            or not pull_request_id
            or not isinstance(ids, list)
            or not all(isinstance(item, str) and item for item in ids)
            or not isinstance(receipts, list)
            or not all(isinstance(item, dict) for item in receipts)
            or (pending_review_id is not None and not isinstance(pending_review_id, str))
            or (active_thread_id is not None and not isinstance(active_thread_id, str))
            or (active_comment_id is not None and not isinstance(active_comment_id, str))
        ):
            return None
        return cls(
            phase=phase,  # type: ignore[arg-type]
            pull_request_id=pull_request_id,
            pending_review_id=pending_review_id,
            replied_thread_ids=tuple(ids),
            receipts=tuple(dict(item) for item in receipts),
            active_thread_id=active_thread_id,
            active_comment_id=active_comment_id,
        )


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
class RecoverRemediationReplyJournalRequest:
    """Recover one format-3 journal for exact remediation identities."""

    issue_number: int
    pr_number: int
    repository: str
    branch: str
    current_remote_head: str
    threads: FrozenJson
    deadline_s: float

    def __post_init__(self) -> None:
        """Validate exact identities and the frozen thread snapshot."""
        _positive_identifier(self.issue_number, "issue_number")
        _positive_identifier(self.pr_number, "pr_number")
        if (
            not isinstance(self.repository, str)
            or self.repository != self.repository.casefold()
            or self.repository.count("/") != 1
            or not all(self.repository.split("/"))
        ):
            raise ValueError("repository must be canonical lowercase OWNER/REPOSITORY")
        if not isinstance(self.branch, str) or not self.branch:
            raise ValueError("branch must be a non-empty string")
        _full_sha(self.current_remote_head, "current_remote_head")
        _json_root(self.threads, list, "threads")
        if self.deadline_s is None:
            raise ValueError("deadline_s is required for remediation journal recovery")
        _deadline(self.deadline_s)


@dataclass(frozen=True)
class AppendReplyJournalRequest:
    """Append one replay-safe exact reply journal entry."""

    issue_number: int
    marker: str
    body: str
    deadline_s: float | None = None
    prepublication_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        """Validate the replay-safe journal marker and body."""
        _positive_identifier(self.issue_number, "issue_number")
        if not isinstance(self.marker, str) or not any(
            pattern.fullmatch(self.marker) is not None
            for pattern in (_JOURNAL_MARKER_RE, _REMEDIATION_JOURNAL_MARKER_RE)
        ):
            raise ValueError("marker must be an exact implementation reply journal marker")
        if not isinstance(self.body, str) or not self.body.startswith(f"{self.marker}\n<!-- "):
            raise ValueError("body must contain the exact journal marker and payload")
        _deadline(self.deadline_s)
        if self.prepublication_receipt_sha256 is not None and (
            not isinstance(self.prepublication_receipt_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.prepublication_receipt_sha256) is None
        ):
            raise ValueError("prepublication receipt digest must be lowercase SHA-256")


@dataclass(frozen=True)
class DeliverReplyHandoffRequest:
    """Deliver one already-journaled exact reply batch."""

    issue_number: int
    pr_number: int
    handoff: FrozenJson
    visibility_retries: int
    deadline_s: float | None = None

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
        _deadline(self.deadline_s)


def bind_delivery_request(
    pending: object,
    *,
    issue_number: int,
    pr_number: int,
    handoff: object,
    visibility_retries: int,
    deadline_s: float,
) -> DeliverReplyHandoffRequest:
    """Create or validate one immutable delivery request across retries."""
    frozen_handoff = FrozenJson.snapshot(handoff)
    if pending is None:
        return DeliverReplyHandoffRequest(
            issue_number=issue_number,
            pr_number=pr_number,
            handoff=frozen_handoff,
            visibility_retries=visibility_retries,
            deadline_s=deadline_s,
        )
    if (
        not isinstance(pending, DeliverReplyHandoffRequest)
        or pending.issue_number != issue_number
        or pending.pr_number != pr_number
        or pending.handoff != frozen_handoff
        or pending.visibility_retries != visibility_retries
        or pending.deadline_s != deadline_s
    ):
        raise ValueError("pending reply delivery request identity is invalid")
    return pending


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
    deadline_s: float
    issue_number: int | None = None
    host_verification_profile: str | None = None
    host_verification_receipts: FrozenJson = field(default_factory=lambda: FrozenJson.snapshot([]))

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
        _deadline(self.deadline_s)
        if self.issue_number is not None:
            _positive_identifier(self.issue_number, "issue_number")
        if self.host_verification_profile is not None and not isinstance(
            self.host_verification_profile, str
        ):
            raise ValueError("host_verification_profile must be a string or None")
        _json_root(self.host_verification_receipts, list, "host_verification_receipts")


@dataclass(frozen=True)
class RunMergeWaitCycleRequest:
    """Run one exact-head merge admission and conditional-request cycle."""

    pr_number: int
    reviewed_head_sha: str
    proof_generation: int
    declined_readiness_fingerprint: tuple[str, ...] | None
    deadline_s: float
    cancellation: threading.Event
    issue_number: int | None = None
    queue_admitted: bool = False
    bootstrap_proof: BootstrapProof | None = None
    rebase_proof: RebaseReviewProof | None = None
    rebase_record: RebaseReviewRecord | None = None

    @property
    def merge_head_sha(self) -> str:
        """Return the head that requires fresh checks and conditional merge."""
        return self.rebase_proof.resulting_head_sha if self.rebase_proof else self.reviewed_head_sha

    def _validate_rebase_evidence(self) -> None:
        """Keep the initial audit and complete record with the host proof."""
        if (self.rebase_proof is None) != (self.rebase_record is None):
            raise ValueError("rebase proof and initial record must both be present")
        if self.rebase_record is not None and (
            not isinstance(self.rebase_record, RebaseReviewRecord)
            or not isinstance(self.rebase_proof, RebaseReviewProof)
            or self.rebase_record.state != "active"
            or any(
                getattr(self.rebase_record, name) != getattr(self.rebase_proof, name)
                for name in RebaseReviewProof.__dataclass_fields__
            )
        ):
            raise ValueError("rebase record must match the host proof")
        if self.rebase_proof is not None and (
            not isinstance(self.rebase_proof, RebaseReviewProof)
            or self.rebase_proof.pr_number != self.pr_number
            or self.rebase_proof.issue_number != self.issue_number
            or self.rebase_proof.reviewed_head_sha != self.reviewed_head_sha
            or self.bootstrap_proof is not None
        ):
            raise ValueError("rebase proof must match the original review and merge target")

    def __post_init__(self) -> None:
        """Validate the exact-head merge proof and readiness fingerprint."""
        self._validate_rebase_evidence()
        if self.bootstrap_proof is not None and (
            not is_process_bootstrap_proof(self.bootstrap_proof)
            or self.bootstrap_proof.pr != self.pr_number
            or self.bootstrap_proof.issue != self.issue_number
            or self.bootstrap_proof.head_sha != self.reviewed_head_sha
        ):
            raise ValueError("bootstrap proof must match this process and merge target")
        _positive_identifier(self.pr_number, "pr_number")
        _full_sha(self.reviewed_head_sha, "reviewed_head_sha")
        if (
            isinstance(self.proof_generation, bool)
            or not isinstance(self.proof_generation, int)
            or self.proof_generation < 0
        ):
            raise ValueError("proof_generation must be a non-negative integer")
        if (
            isinstance(self.deadline_s, bool)
            or not isinstance(self.deadline_s, (int, float))
            or not math.isfinite(self.deadline_s)
            or self.deadline_s <= 0
        ):
            raise ValueError("deadline_s must be a finite positive monotonic deadline")
        if not isinstance(self.cancellation, threading.Event):
            raise ValueError("cancellation must be a threading.Event")
        fingerprint = self.declined_readiness_fingerprint
        if fingerprint is not None and (
            not isinstance(fingerprint, tuple)
            or not all(isinstance(part, str) for part in fingerprint)
        ):
            raise ValueError("declined_readiness_fingerprint must be a tuple of strings or None")
        if self.issue_number is not None:
            _positive_identifier(self.issue_number, "issue_number")
        if not isinstance(self.queue_admitted, bool):
            raise ValueError("queue_admitted must be a Boolean")


@dataclass(frozen=True)
class EnsureScopeExpansionChildrenRequest:
    """Ensure one durable child issue per validated scope expansion."""

    issue_number: int
    pr_number: int
    reviewed_head_sha: str
    scope_expansions: tuple[ScopeExpansion, ...]
    retraction_findings: FrozenJson = FrozenJson(encoded="[]")
    review_diff: str = ""

    def __post_init__(self) -> None:
        """Validate the immutable child-issue ensure request."""
        _positive_identifier(self.issue_number, "issue_number")
        _positive_identifier(self.pr_number, "pr_number")
        _full_sha(self.reviewed_head_sha, "reviewed_head_sha")
        if not isinstance(self.scope_expansions, tuple) or not self.scope_expansions:
            raise ValueError("scope_expansions must be a non-empty tuple")
        if len(self.scope_expansions) > 8:
            raise ValueError("scope_expansions must contain at most eight records")
        if not all(
            isinstance(expansion, ScopeExpansion)
            and normalize_scope_expansion(expansion.as_dict()) == expansion
            for expansion in self.scope_expansions
        ):
            raise ValueError("scope_expansions must contain scope-expansion records")
        retractions = _json_root(self.retraction_findings, list, "retraction_findings")
        if not isinstance(retractions, list) or any(
            not isinstance(finding, dict)
            or not scope_retraction_paths_from_body(finding.get("body"))
            for finding in retractions
        ):
            raise ValueError("retraction_findings must contain scope retractions")
        if bool(retractions) != bool(self.review_diff):
            raise ValueError("retraction findings and review_diff must be supplied together")
        if len(self.retraction_findings.encoded.encode()) + len(self.review_diff.encode()) > 40_000:
            raise ValueError("retraction projection is too large")


@dataclass(frozen=True)
class ReconcileScopeExpansionDependenciesRequest:
    """Reconcile durable scope-expansion dependencies for one source head."""

    issue_number: int
    pr_number: int
    source_head_sha: str

    def __post_init__(self) -> None:
        """Validate the immutable dependency request."""
        _positive_identifier(self.issue_number, "issue_number")
        _positive_identifier(self.pr_number, "pr_number")
        _full_sha(self.source_head_sha, "source_head_sha")


@dataclass(frozen=True)
class InspectDirtyDirectPrStateRequest:
    """Select the exact repository, issue, and interrupted direct branch."""

    repository: str
    issue_number: int
    branch: str

    def __post_init__(self) -> None:
        """Reject identities outside the closed direct-writer read."""
        _positive_identifier(self.issue_number, "issue_number")
        if (
            not isinstance(self.repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None
        ):
            raise ValueError("repository must be OWNER/REPOSITORY")
        if (
            not isinstance(self.branch, str)
            or re.fullmatch(rf"{self.issue_number}-auto-impl-direct-[0-9a-f]{{32}}", self.branch)
            is None
        ):
            raise ValueError("branch must name the exact direct issue writer")


@dataclass(frozen=True)
class DirtyDirectPrStateRead:
    """Retain the complete immutable open-PR evidence for one direct writer."""

    repository: str
    issue_number: int
    branch: str
    branch_prs: tuple[tuple[int, str], ...]
    issue_pr_number: int | None
    complete: bool = True
    plan_journal: FrozenJson | None = None
    issue_state: str = ""
    issue_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate identity, immutable rows, and complete-read classification."""
        InspectDirtyDirectPrStateRequest(self.repository, self.issue_number, self.branch)
        if self.plan_journal is not None and (
            not isinstance(self.plan_journal, FrozenJson)
            or len(self.plan_journal.encoded.encode("utf-8")) > 1024 * 1024
            or not isinstance(self.plan_journal.thaw(), list)
        ):
            raise ValueError("dirty direct plan journal is invalid or exceeds its bound")
        if not isinstance(self.issue_state, str) or not isinstance(self.issue_labels, tuple):
            raise ValueError("dirty direct issue state must be immutable")
        if any(not isinstance(label, str) for label in self.issue_labels):
            raise ValueError("dirty direct issue labels must be strings")
        if type(self.complete) is not bool:
            raise ValueError("complete must be a boolean")
        if not isinstance(self.branch_prs, tuple) or len(self.branch_prs) >= 1000:
            raise ValueError("branch PRs must be a bounded immutable tuple")
        numbers: set[int] = set()
        for row in self.branch_prs:
            if not isinstance(row, tuple) or len(row) != 2:
                raise ValueError("branch PR rows must be immutable number/base pairs")
            number, base = row
            _positive_identifier(number, "branch PR number")
            if not isinstance(base, str) or not base or base != base.strip():
                raise ValueError("branch PR base must be a nonblank exact string")
            if number in numbers:
                raise ValueError("branch PR numbers must be unique")
            numbers.add(number)
        if self.issue_pr_number is not None:
            _positive_identifier(self.issue_pr_number, "issue_pr_number")

    @property
    def absent(self) -> bool:
        """Return absence only from a complete read with no open PR."""
        return self.complete and not self.branch_prs and self.issue_pr_number is None


@dataclass(frozen=True)
class InspectAdoptedRemediationPrStateRequest:
    """Read the current adopted PR and every unresolved thread."""

    repository: str
    issue_number: int
    pr_number: int
    branch: str
    expected_head: str
    expected_thread_snapshot_json: str

    def __post_init__(self) -> None:
        """Validate exact bounded request pins."""
        from hephaestus.automation.remediation_recovery import RemediationReviewInput

        _positive_identifier(self.issue_number, "issue_number")
        _positive_identifier(self.pr_number, "pr_number")
        _full_sha(self.expected_head, "expected_head")
        if (
            not isinstance(self.repository, str)
            or re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", self.repository) is None
        ):
            raise ValueError("adopted repository is invalid")
        if (
            not isinstance(self.branch, str)
            or not self.branch
            or self.branch.startswith(("-", "/"))
            or ".." in self.branch
            or any(ord(c) < 32 for c in self.branch)
        ):
            raise ValueError("adopted branch is invalid")
        value = self.expected_thread_snapshot_json
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > 1024 * 1024
            or RemediationReviewInput.canonical_thread_snapshot(json.loads(value)) != value
        ):
            raise ValueError("adopted thread snapshot is invalid")


@dataclass(frozen=True)
class AdoptedRemediationPrStateRead:
    """Retain bounded current PR facts without mutation authority."""

    repository: str
    issue_number: int
    pr_number: int
    branch: str
    head: str
    state: str
    origin_writable: bool
    thread_snapshot_json: str
    complete: bool

    def __post_init__(self) -> None:
        """Validate the immutable readback envelope."""
        InspectAdoptedRemediationPrStateRequest(
            self.repository,
            self.issue_number,
            self.pr_number,
            self.branch,
            self.head,
            self.thread_snapshot_json,
        )
        if (
            self.state not in {"OPEN", "CLOSED", "MERGED"}
            or type(self.origin_writable) is not bool
            or type(self.complete) is not bool
        ):
            raise ValueError("adopted PR state is invalid")


@dataclass(frozen=True)
class InspectRebaseConflictRequest:
    """Read GO and conflict state for the exact replay inputs."""

    repository: str
    pr_number: int
    reviewed_head_sha: str
    base_sha: str

    def __post_init__(self) -> None:
        """Validate the repository, PR, and exact commits."""
        if not isinstance(self.repository, str) or len(self.repository.split("/")) != 2:
            raise ValueError("rebase repository must be OWNER/REPOSITORY")
        if not all(self.repository.split("/")):
            raise ValueError("rebase repository must be OWNER/REPOSITORY")
        _positive_identifier(self.pr_number, "pr_number")
        _full_sha(self.reviewed_head_sha, "reviewed_head_sha")
        _full_sha(self.base_sha, "base_sha")


@dataclass(frozen=True)
class RebaseConflictInspected:
    """Return read-only admission for one exact head and base."""

    request: InspectRebaseConflictRequest
    admitted: bool
    reason: str

    def __post_init__(self) -> None:
        """Require a typed admission result."""
        if not isinstance(self.request, InspectRebaseConflictRequest):
            raise TypeError("rebase admission request is invalid")
        if type(self.admitted) is not bool or not isinstance(self.reason, str) or not self.reason:
            raise ValueError("rebase admission result is invalid")


@dataclass(frozen=True)
class InspectRebaseReviewRequest:
    """Read retained review facts after a rebase push."""

    record: RebaseReviewRecord

    def __post_init__(self) -> None:
        """Require typed active review facts."""
        if not isinstance(self.record, RebaseReviewRecord) or self.record.state != "active":
            raise ValueError("rebase inspection record is invalid")

    @property
    def repository(self) -> str:
        """Return the repository bound to the record."""
        return self.record.repository

    @property
    def issue_number(self) -> int:
        """Return the issue bound to the record."""
        return self.record.issue_number

    @property
    def pr_number(self) -> int:
        """Return the PR bound to the record."""
        return self.record.pr_number


@dataclass(frozen=True)
class RebaseReviewInspected:
    """Report fresh authenticated facts for one exact request."""

    request: InspectRebaseReviewRequest
    verified: bool

    def __post_init__(self) -> None:
        """Reject untyped inspection results."""
        if not isinstance(self.request, InspectRebaseReviewRequest):
            raise TypeError("rebase inspection request is invalid")
        if type(self.verified) is not bool:
            raise ValueError("rebase inspection result is invalid")


@dataclass(frozen=True)
class PublishRebaseReviewRequest:
    """Publish retained review facts before a rebase push."""

    record: RebaseReviewRecord

    def __post_init__(self) -> None:
        """Require typed active review facts."""
        if not isinstance(self.record, RebaseReviewRecord) or self.record.state != "active":
            raise ValueError("rebase publication record is invalid")

    @property
    def repository(self) -> str:
        """Return the repository bound to the record."""
        return self.record.repository

    @property
    def issue_number(self) -> int:
        """Return the issue bound to the record."""
        return self.record.issue_number

    @property
    def pr_number(self) -> int:
        """Return the PR bound to the record."""
        return self.record.pr_number


@dataclass(frozen=True)
class RebaseReviewPublished:
    """Report authenticated publication for one exact request."""

    request: PublishRebaseReviewRequest
    published: bool

    def __post_init__(self) -> None:
        """Reject untyped publication results."""
        if not isinstance(self.request, PublishRebaseReviewRequest):
            raise TypeError("rebase publication request is invalid")
        if type(self.published) is not bool:
            raise ValueError("rebase publication result is invalid")


type GitHubRequest = (
    InspectAdoptedRemediationPrStateRequest
    | InspectDirtyDirectPrStateRequest
    | InspectRebaseConflictRequest
    | PublishRebaseReviewRequest
    | InspectRebaseReviewRequest
    | RecoverReplyJournalRequest
    | RecoverRemediationReplyJournalRequest
    | AppendReplyJournalRequest
    | DeliverReplyHandoffRequest
    | ReconcilePrReviewRequest
    | RunMergeWaitCycleRequest
    | EnsureScopeExpansionChildrenRequest
    | ReconcileScopeExpansionDependenciesRequest
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
                InspectAdoptedRemediationPrStateRequest,
                InspectDirtyDirectPrStateRequest,
                InspectRebaseConflictRequest,
                PublishRebaseReviewRequest,
                InspectRebaseReviewRequest,
                RecoverReplyJournalRequest,
                RecoverRemediationReplyJournalRequest,
                AppendReplyJournalRequest,
                DeliverReplyHandoffRequest,
                ReconcilePrReviewRequest,
                RunMergeWaitCycleRequest,
                EnsureScopeExpansionChildrenRequest,
                ReconcileScopeExpansionDependenciesRequest,
            ),
        ):
            raise TypeError("request must be a supported GitHub request")
        if isinstance(
            self.request,
            (
                InspectDirtyDirectPrStateRequest,
                InspectAdoptedRemediationPrStateRequest,
                InspectRebaseConflictRequest,
                PublishRebaseReviewRequest,
                InspectRebaseReviewRequest,
            ),
        ) and (self.request.repository.rsplit("/", 1)[-1].casefold() != self.repo.casefold()):
            raise ValueError("dirty direct request repository does not match the job")
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
class RemediationReplyJournalRecovered:
    """Receipt for one format-3 remediation journal recovery read."""

    request: RecoverRemediationReplyJournalRequest
    handoff: FrozenJson | None

    def __post_init__(self) -> None:
        """Validate the optional recovered remediation handoff."""
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


@dataclass(frozen=True)
class ScopeExpansionChildrenEnsured:
    """Immutable result of idempotently ensuring scope-expansion children."""

    request: EnsureScopeExpansionChildrenRequest
    status: Literal["blocked", "operator_required", "dry_run"]
    child_issue_numbers: tuple[int, ...]

    def __post_init__(self) -> None:
        """Validate the closed child-issue result."""
        if self.status not in {
            "blocked",
            "operator_required",
            "dry_run",
        }:
            raise ValueError("status must be a supported scope-expansion outcome")
        if not isinstance(self.child_issue_numbers, tuple) or not all(
            isinstance(issue_number, int) and issue_number > 0
            for issue_number in self.child_issue_numbers
        ):
            raise ValueError("child_issue_numbers must be a tuple of positive integers")


@dataclass(frozen=True)
class ScopeExpansionDependenciesReconciled:
    """Immutable result of a source dependency lifecycle read."""

    request: ReconcileScopeExpansionDependenciesRequest
    status: Literal[
        "none",
        "retraction_required",
        "parked",
        "sync_required",
        "fresh_review",
        "operator_required",
    ]
    child_issue_numbers: tuple[int, ...]
    merge_shas: tuple[str, ...] = ()
    retraction_threads: FrozenJson = FrozenJson(encoded="[]")
    retraction_snapshots: FrozenJson = FrozenJson(encoded="[]")

    def __post_init__(self) -> None:
        """Validate the closed dependency result."""
        if self.status not in {
            "none",
            "retraction_required",
            "parked",
            "sync_required",
            "fresh_review",
            "operator_required",
        }:
            raise ValueError("status must be a supported dependency outcome")
        if not isinstance(self.child_issue_numbers, tuple) or not all(
            isinstance(issue_number, int) and issue_number > 0
            for issue_number in self.child_issue_numbers
        ):
            raise ValueError("child_issue_numbers must be a tuple of positive integers")
        if not isinstance(self.merge_shas, tuple) or not all(
            isinstance(sha, str) and _FULL_SHA_RE.fullmatch(sha) is not None
            for sha in self.merge_shas
        ):
            raise ValueError("merge_shas must be a tuple of full lowercase commit SHAs")
        _json_root(self.retraction_threads, list, "retraction_threads")
        _json_root(self.retraction_snapshots, list, "retraction_snapshots")
        retractions = self.retraction_threads.thaw()
        snapshots = self.retraction_snapshots.thaw()
        if self.status == "retraction_required" and (
            not isinstance(retractions, list)
            or not retractions
            or not isinstance(snapshots, list)
            or len(retractions) != len(snapshots)
        ):
            raise ValueError("retraction_required must contain matching durable snapshots")
        if self.status in {"sync_required", "fresh_review"} and (
            not self.child_issue_numbers or not self.merge_shas
        ):
            raise ValueError("merged dependency outcomes require child issues and merge SHAs")


type GitHubReceipt = (
    AdoptedRemediationPrStateRead
    | DirtyDirectPrStateRead
    | RebaseConflictInspected
    | RebaseReviewPublished
    | RebaseReviewInspected
    | ReplyJournalRecovered
    | RemediationReplyJournalRecovered
    | ReplyJournalAppended
    | ReplyHandoffAttempted
    | PrReviewReconciled
    | MergeWaitCycleCompleted
    | ScopeExpansionChildrenEnsured
    | ScopeExpansionDependenciesReconciled
)


class GitHubJobRunner(Protocol):
    """Executes closed GitHub requests with job-scoped accessors."""

    def run(self, job: GitHubJob) -> GitHubReceipt:
        """Execute one closed GitHub request and return its immutable receipt."""
        raise NotImplementedError
