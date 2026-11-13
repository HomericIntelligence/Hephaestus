"""Transactional canonical comments for implementation-plan revisions.

An issue has one current plan and one current review. Bounded fingerprints
in the plan metadata identify earlier revisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from hephaestus.automation.github_api.issues import issue_body_digest
from hephaestus.automation.protocol import PLAN_REVIEW_CANONICAL_MARKER
from hephaestus.automation.review_journal import (
    IssueComment,
    contains_raw_patch,
    is_pending_review,
    journal_snapshot,
    normalized_plan,
    plan_fingerprint,
    render_current_plan,
    render_pending_review,
)
from hephaestus.automation.state_labels import STATE_PLAN_NO_GO, is_exclusive_plan_state


class PlanJournalGitHub(Protocol):
    """Minimal GitHub mutation surface required by the journal transaction."""

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        """Return every issue comment in creation order."""
        pass

    def gh_issue_json(self, issue_number: int) -> dict[str, object]:
        """Return live issue metadata including the authoritative labels."""
        pass

    def upsert_plan_comment(self, issue_number: int, plan: str) -> None:
        """Replace the actor-owned canonical plan comment."""
        pass

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
    ) -> None:
        """Replace an actor-owned canonical issue comment."""
        pass


class PlanRevisionOwnershipError(RuntimeError):
    """A different pipeline item owns the authoritative plan revision."""


def _upsert_pending_review(
    issue_number: int,
    revision: int,
    github: PlanJournalGitHub,
) -> None:
    github.upsert_issue_comment(
        issue_number,
        PLAN_REVIEW_CANONICAL_MARKER,
        render_pending_review(revision=revision),
    )


def _confirm_publication(
    issue_number: int,
    expected_plan_body: str,
    expected_revision: int,
    github: PlanJournalGitHub,
    *,
    forced_planning_epoch: bool,
) -> None:
    """Fail closed when a concurrent writer replaced the just-published pointer."""
    snapshot = journal_snapshot(github.issue_comments(issue_number))
    if (
        snapshot.revision != expected_revision
        or snapshot.current_plan_body != expected_plan_body
        or snapshot.current_review_revision != expected_revision
        or not is_pending_review(snapshot.current_review, revision=expected_revision)
        or snapshot.forced_planning_epoch is not forced_planning_epoch
    ):
        raise PlanRevisionOwnershipError(
            f"concurrent plan journal write detected for revision {expected_revision}; "
            "another pipeline item owns the published revision"
        )


@dataclass(frozen=True)
class PlanPublication:
    """Result of publishing or rejecting one proposed plan revision."""

    revision: int
    plan: str
    changed: bool
    no_progress_reason: str = ""
    canonical_body: str = ""

    @property
    def is_stuck(self) -> bool:
        """Return whether another automated planning iteration would not progress."""
        return bool(self.no_progress_reason)


def reconcile_plan_journal(issue_number: int, github: PlanJournalGitHub) -> list[IssueComment]:
    """Repair a pending review after its current plan became durable."""
    comments = github.issue_comments(issue_number)
    snapshot = journal_snapshot(comments)
    if snapshot.current_plan and snapshot.current_review_revision != snapshot.revision:
        _upsert_pending_review(issue_number, snapshot.revision, github)
        return github.issue_comments(issue_number)
    return comments


def known_plan_fingerprints(comments: Sequence[IssueComment]) -> set[str]:
    """Return the current fingerprint and stored prior fingerprints."""
    snapshot = journal_snapshot(comments)
    return {
        *snapshot.prior_plan_fingerprints,
        *([plan_fingerprint(snapshot.current_plan)] if snapshot.current_plan.strip() else []),
    }


def publish_plan_revision(
    issue_number: int,
    candidate: str,
    github: PlanJournalGitHub,
    *,
    require_change: bool,
    forced_planning_epoch: bool = False,
) -> PlanPublication:
    """Publish a current plan, then its pending review, and confirm both.

    Args:
        issue_number: Issue whose plan journal is updated.
        candidate: Newly generated plan text.
        github: Injected GitHub comment accessor.
        require_change: Whether equality with the current plan means the
            planner is stuck (true for amendments/replans) or an idempotent
            replay (false for initial publication/restart verification).

    Returns:
        The durable revision and whether the proposal made progress.

    Raises:
        RuntimeError: If the current journal has conflicting identities.

    """
    comments = reconcile_plan_journal(issue_number, github)
    snapshot = journal_snapshot(comments)
    candidate_plan = normalized_plan(candidate)
    candidate_fingerprint = plan_fingerprint(candidate)
    current_fingerprint = plan_fingerprint(snapshot.current_plan)

    if contains_raw_patch(candidate):
        return PlanPublication(
            revision=snapshot.revision,
            plan=snapshot.current_plan,
            changed=False,
            no_progress_reason=(
                "The proposed plan contains a raw patch or diff hunk. Public plans must "
                "describe intended changes without embedding source diffs; regenerate the "
                "complete plan before publication."
            ),
        )

    if not candidate_plan:
        return PlanPublication(
            revision=snapshot.revision,
            plan=snapshot.current_plan,
            changed=False,
            no_progress_reason=(
                "The proposed plan is empty; another automated planning iteration would not "
                "make progress. An external actor must resolve the missing decision, "
                "requirement, or dependency and replace the blocked label to resume."
            ),
        )

    if snapshot.current_plan and candidate_fingerprint == current_fingerprint:
        if not require_change:
            return PlanPublication(
                revision=snapshot.revision,
                plan=snapshot.current_plan,
                changed=False,
            )
        return PlanPublication(
            revision=snapshot.revision,
            plan=snapshot.current_plan,
            changed=False,
            no_progress_reason=(
                "The proposed plan is identical to the current plan "
                f"(fingerprint {candidate_fingerprint}); another automated planning iteration "
                "would repeat. An external actor must resolve the missing decision, "
                "requirement, or dependency and replace the blocked label to resume."
            ),
        )

    if candidate_fingerprint in known_plan_fingerprints(comments):
        return PlanPublication(
            revision=snapshot.revision,
            plan=snapshot.current_plan,
            changed=False,
            no_progress_reason=(
                "The proposed plan repeats a previous plan "
                f"(fingerprint {candidate_fingerprint}); another automated planning iteration "
                "would oscillate. An external actor must resolve the missing decision, "
                "requirement, or dependency and replace the blocked label to resume."
            ),
        )

    if not snapshot.current_plan:
        canonical_body = render_current_plan(
            candidate_plan,
            revision=snapshot.revision,
            forced_planning_epoch=forced_planning_epoch,
        )
        github.upsert_plan_comment(
            issue_number,
            canonical_body,
        )
        _upsert_pending_review(issue_number, snapshot.revision, github)
        _confirm_publication(
            issue_number,
            canonical_body,
            snapshot.revision,
            github,
            forced_planning_epoch=forced_planning_epoch,
        )
        return PlanPublication(
            revision=snapshot.revision,
            plan=candidate_plan,
            changed=True,
            canonical_body=canonical_body,
        )

    issue_data = github.gh_issue_json(issue_number)
    raw_labels = issue_data.get("labels", [])
    if not isinstance(raw_labels, list):
        raw_labels = []
    labels = {
        str(label.get("name")) if isinstance(label, dict) else str(label)
        for label in raw_labels
        if isinstance(label, (dict, str))
    }
    if not is_exclusive_plan_state(labels, STATE_PLAN_NO_GO):
        raise PlanRevisionOwnershipError(
            f"cannot supersede plan revision {snapshot.revision} without an authoritative "
            f"exclusive {STATE_PLAN_NO_GO} label"
        )

    next_revision = snapshot.revision + 1
    prior_fingerprints = (
        *snapshot.prior_plan_fingerprints,
        current_fingerprint,
    )
    canonical_body = render_current_plan(
        candidate_plan,
        revision=next_revision,
        prior_fingerprints=prior_fingerprints,
        forced_planning_epoch=forced_planning_epoch,
    )
    github.upsert_plan_comment(
        issue_number,
        canonical_body,
    )
    _upsert_pending_review(issue_number, next_revision, github)
    _confirm_publication(
        issue_number,
        canonical_body,
        next_revision,
        github,
        forced_planning_epoch=forced_planning_epoch,
    )
    return PlanPublication(
        revision=next_revision,
        plan=candidate_plan,
        changed=True,
        canonical_body=canonical_body,
    )


# Durable decomposition records (issue #2982).
#
# Every record is an actor-owned comment with one role marker and a canonical
# JSON payload. Recovery re-reads GitHub and matches the role marker: an exact
# duplicate recovers, a conflicting payload for the same identity fails
# closed, and a foreign or malformed comment is never quarantined.

RECORD_SCHEMA_VERSION: Final[int] = 1

NOGO_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-nogo:v1:"
INTENT_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-decomposition-intent:v1:"
RECEIPT_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-decomposition-review:v1:"
SEAL_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-decomposition-seal:v1:"
BINDINGS_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-decomposition-bindings:v1:"
PARENT_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-decomposition-parent:v1:"

DRAFT_PART_MAX_CHARS: Final[int] = 40_000
FINDING_MAX_CHARS: Final[int] = 500
MAX_FINDINGS_PER_RECORD: Final[int] = 16

_EPOCH_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^(?:req|find)-[0-9a-f]{12}$")


class PlanRecordConflictError(RuntimeError):
    """A durable record for the same identity key carries another payload."""


def _render_record(marker: str, payload: dict[str, Any]) -> str:
    """Render one actor-owned record comment with its canonical payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{marker}\n```json\n{encoded}\n```"


def _parse_records(
    comments: Sequence[IssueComment],
    prefix: str,
) -> list[tuple[str, str]]:
    """Return every (identity key, payload JSON) parsed from role comments."""
    parsed: list[tuple[str, str]] = []
    for comment in comments:
        body = comment.body if isinstance(comment.body, str) else ""
        start = body.find(prefix)
        if start == -1:
            continue
        marker_line = body[start:]
        end = marker_line.find("\n")
        marker_line = marker_line if end == -1 else marker_line[:end]
        if not marker_line.rstrip().endswith("-->"):
            continue
        key = marker_line.rstrip()[len(prefix) : -3].strip()
        payload_start = body.find("```json", start + len(prefix))
        if payload_start == -1:
            continue
        payload_start = body.index("\n", payload_start) + 1
        payload_end = body.find("```", payload_start)
        if payload_end == -1:
            continue
        parsed.append((key, body[payload_start:payload_end].strip()))
    return parsed


def _decode_payload(key: str, raw: str) -> dict[str, Any] | None:
    """Decode one record payload, or None when it is not a JSON object."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["_key"] = key
    return payload


def _canonicalize(value: Any) -> Any:
    """Return *value* with every JSON list canonicalized back to a tuple."""
    if isinstance(value, list):
        return tuple(_canonicalize(item) for item in value)
    if isinstance(value, dict):
        return {key: _canonicalize(item) for key, item in value.items()}
    return value


def _upsert_record(
    issue_number: int,
    prefix: str,
    key: str,
    payload: dict[str, Any],
    github: PlanJournalGitHub,
) -> None:
    """Publish one record once and fail closed on a conflicting duplicate."""
    marker = f"{prefix}{key} -->"
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for existing_key, raw in _parse_records(github.issue_comments(issue_number), prefix):
        if existing_key != key:
            continue
        existing = _decode_payload(existing_key, raw)
        if existing is None:
            # A damaged same-key payload cannot prove ownership: keep the
            # foreign comment and write no duplicate.
            raise PlanRecordConflictError(f"durable record {prefix}{key} --> is unreadable")
        existing.pop("_key", None)
        existing_encoded = json.dumps(
            existing, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if existing_encoded != encoded:
            raise PlanRecordConflictError(
                f"durable record {prefix}{key} --> carries a conflicting payload"
            )
        return
    github.upsert_issue_comment(issue_number, marker, _render_record(marker, payload))
    confirmed = _decode_payload(
        key,
        next(
            (
                raw
                for existing_key, raw in _parse_records(github.issue_comments(issue_number), prefix)
                if existing_key == key
            ),
            "",
        ),
    )
    if confirmed is None:
        raise PlanRecordConflictError(
            f"durable record {prefix}{key} --> was not confirmed after publication"
        )


def requirements_epoch(issue_body: str) -> str:
    """Return the authoritative requirements-epoch identity for one issue."""
    return issue_body_digest(issue_body)


def normalize_record_findings(value: object) -> tuple[str, ...]:
    """Validate the bounded unresolved-finding manifest of a NOGO record."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_FINDINGS_PER_RECORD:
        return ()
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > FINDING_MAX_CHARS:
            return ()
        normalized.append(item.strip())
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class PlanNogoRecord:
    """One confirmed real NOGO verdict bound to its review identity."""

    repository: str
    issue: int
    requirements_epoch: str
    plan_revision: int
    review_cycle: int
    review_round: int
    review_digest: str
    findings: tuple[str, ...]
    recorded_at: str

    def payload(self) -> dict[str, Any]:
        """Return the canonical record payload."""
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "repository": self.repository,
            "issue": self.issue,
            "requirements_epoch": self.requirements_epoch,
            "plan_revision": self.plan_revision,
            "review_cycle": self.review_cycle,
            "review_round": self.review_round,
            "review_digest": self.review_digest,
            "findings": list(self.findings),
            "recorded_at": self.recorded_at,
        }


def _nogo_from_payload(key: str, payload: dict[str, Any]) -> PlanNogoRecord | None:
    """Validate one decoded NOGO payload against the closed schema."""
    required = {
        "schema_version",
        "repository",
        "issue",
        "requirements_epoch",
        "plan_revision",
        "review_cycle",
        "review_round",
        "review_digest",
        "findings",
        "recorded_at",
    }
    if set(payload) - {"_key"} != required:
        return None
    epoch = payload.get("requirements_epoch")
    digest = payload.get("review_digest")
    revision = payload.get("plan_revision")
    cycle = payload.get("review_cycle")
    round_value = payload.get("review_round")
    issue = payload.get("issue")
    if (
        payload.get("schema_version") != RECORD_SCHEMA_VERSION
        or not isinstance(payload.get("repository"), str)
        or not payload["repository"]
        or not isinstance(epoch, str)
        or _EPOCH_RE.fullmatch(epoch) is None
        or not isinstance(digest, str)
        or _DIGEST_RE.fullmatch(digest) is None
        or digest != key
        or type(revision) is not int
        or revision < 1
        or type(cycle) is not int
        or cycle < 0
        or type(round_value) is not int
        or round_value < 0
        or type(issue) is not int
        or issue < 1
        or not isinstance(payload.get("recorded_at"), str)
        or not payload["recorded_at"]
    ):
        return None
    raw_findings = payload.get("findings")
    findings = normalize_record_findings(raw_findings)
    if raw_findings is not None and len(findings) != len(raw_findings):
        return None
    return PlanNogoRecord(
        repository=payload["repository"],
        issue=issue,
        requirements_epoch=epoch,
        plan_revision=revision,
        review_cycle=cycle,
        review_round=round_value,
        review_digest=digest,
        findings=findings,
        recorded_at=payload["recorded_at"],
    )


def plan_nogo_records(comments: Sequence[IssueComment]) -> tuple[PlanNogoRecord, ...]:
    """Return every valid durable NOGO record in journal order."""
    records: list[PlanNogoRecord] = []
    for key, raw in _parse_records(comments, NOGO_MARKER_PREFIX):
        payload = _decode_payload(key, raw)
        if payload is None:
            continue
        record = _nogo_from_payload(key, payload)
        if record is not None:
            records.append(record)
    return tuple(records)


def plan_nogo_count(comments: Sequence[IssueComment], epoch: str) -> int:
    """Count confirmed NOGO records for one requirements epoch."""
    return sum(1 for record in plan_nogo_records(comments) if record.requirements_epoch == epoch)


def plan_nogo_digest(
    *,
    plan_revision: int,
    review_cycle: int,
    review_round: int,
    review_raw: str,
) -> str:
    """Return the bound review identity used as the record idempotency key."""
    identity = f"{plan_revision}\0{review_cycle}\0{review_round}\0{review_raw.strip()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def publish_plan_nogo(
    issue_number: int,
    record: PlanNogoRecord,
    github: PlanJournalGitHub,
) -> PlanNogoRecord:
    """Persist one NOGO record exactly once and confirm the durable write."""
    _upsert_record(
        issue_number,
        NOGO_MARKER_PREFIX,
        record.review_digest,
        record.payload(),
        github,
    )
    return record


@dataclass(frozen=True, slots=True)
class DecompositionIntent:
    """One immutable decomposition intent per parent and epoch."""

    repository: str
    parent: int
    requirements_epoch: str
    trigger: str
    source_plan_revision: int
    source_plan_fingerprint: str
    requirement_manifest: tuple[dict[str, str], ...]
    finding_manifest: tuple[dict[str, str], ...]
    created_at: str

    def payload(self) -> dict[str, Any]:
        """Return the canonical record payload."""
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "repository": self.repository,
            "parent": self.parent,
            "requirements_epoch": self.requirements_epoch,
            "trigger": self.trigger,
            "source_plan_revision": self.source_plan_revision,
            "source_plan_fingerprint": self.source_plan_fingerprint,
            "requirement_manifest": list(self.requirement_manifest),
            "finding_manifest": list(self.finding_manifest),
            "created_at": self.created_at,
        }


def _validate_manifest(value: object) -> tuple[dict[str, str], ...] | None:
    """Validate one bounded requirement/finding manifest."""
    if not isinstance(value, (list, tuple)) or len(value) > 128:
        return None
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"identifier", "digest"}:
            return None
        identifier = item["identifier"]
        digest = item["digest"]
        if (
            not isinstance(identifier, str)
            or _IDENTIFIER_RE.fullmatch(identifier) is None
            or not isinstance(digest, str)
            or _DIGEST_RE.fullmatch(digest) is None
        ):
            return None
        refs.append({"identifier": identifier, "digest": digest})
    return tuple(sorted(refs, key=lambda ref: ref["identifier"]))


def publish_decomposition_intent(
    issue_number: int,
    intent: DecompositionIntent,
    github: PlanJournalGitHub,
) -> DecompositionIntent:
    """Publish or recover the one immutable intent for this parent and epoch."""
    _upsert_record(
        issue_number,
        INTENT_MARKER_PREFIX,
        intent.requirements_epoch,
        intent.payload(),
        github,
    )
    return intent


def recover_decomposition_intent(
    comments: Sequence[IssueComment],
    epoch: str,
) -> DecompositionIntent | None:
    """Recover the intent record for one epoch, or None when absent."""
    for key, raw in _parse_records(comments, INTENT_MARKER_PREFIX):
        if key != epoch:
            continue
        payload = _decode_payload(key, raw)
        if payload is None:
            return None
        payload.pop("_key", None)
        required = {
            "schema_version",
            "repository",
            "parent",
            "requirements_epoch",
            "trigger",
            "source_plan_revision",
            "source_plan_fingerprint",
            "requirement_manifest",
            "finding_manifest",
            "created_at",
        }
        if set(payload) != required:
            return None
        requirements = _validate_manifest(payload.get("requirement_manifest"))
        findings = _validate_manifest(payload.get("finding_manifest"))
        parent = payload.get("parent")
        revision = payload.get("source_plan_revision")
        if (
            requirements is None
            or findings is None
            or type(parent) is not int
            or parent < 1
            or type(revision) is not int
            or revision < 1
            or payload.get("schema_version") != RECORD_SCHEMA_VERSION
            or payload.get("trigger") not in {"lossless-size", "convergence"}
            or not isinstance(payload.get("repository"), str)
            or not payload["repository"]
            or not isinstance(payload.get("source_plan_fingerprint"), str)
            or len(payload["source_plan_fingerprint"]) != 64
            or not isinstance(payload.get("created_at"), str)
            or not payload["created_at"]
        ):
            return None
        return DecompositionIntent(
            repository=payload["repository"],
            parent=parent,
            requirements_epoch=key,
            trigger=payload["trigger"],
            source_plan_revision=revision,
            source_plan_fingerprint=payload["source_plan_fingerprint"],
            requirement_manifest=requirements,
            finding_manifest=findings,
            created_at=payload["created_at"],
        )
    return None


@dataclass(frozen=True, slots=True)
class DecompositionReviewReceipt:
    """One independent-review receipt bound to the complete draft digest."""

    requirements_epoch: str
    draft_digest: str
    verdict: str
    recorded_at: str

    def payload(self) -> dict[str, Any]:
        """Return the canonical record payload."""
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "requirements_epoch": self.requirements_epoch,
            "draft_digest": self.draft_digest,
            "verdict": self.verdict,
            "recorded_at": self.recorded_at,
        }


def publish_decomposition_review_receipt(
    issue_number: int,
    receipt: DecompositionReviewReceipt,
    github: PlanJournalGitHub,
) -> DecompositionReviewReceipt:
    """Publish or recover the one review receipt for this epoch and draft."""
    _upsert_record(
        issue_number,
        RECEIPT_MARKER_PREFIX,
        receipt.requirements_epoch,
        receipt.payload(),
        github,
    )
    return receipt


def recover_decomposition_review_receipt(
    comments: Sequence[IssueComment],
    epoch: str,
    *,
    draft_digest: str | None = None,
) -> DecompositionReviewReceipt | None:
    """Recover the independent-review receipt for one epoch, or None."""
    for key, raw in _parse_records(comments, RECEIPT_MARKER_PREFIX):
        if key != epoch:
            continue
        payload = _decode_payload(key, raw)
        if payload is None:
            return None
        payload.pop("_key", None)
        bound = payload.get("draft_digest")
        verdict = payload.get("verdict")
        recorded_at = payload.get("recorded_at")
        if (
            payload.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(bound, str)
            or len(bound) != 64
            or (draft_digest is not None and bound != draft_digest)
            or verdict != "GO"
            or not isinstance(recorded_at, str)
            or not recorded_at
        ):
            return None
        return DecompositionReviewReceipt(
            requirements_epoch=key,
            draft_digest=bound,
            verdict=verdict,
            recorded_at=recorded_at,
        )
    return None


@dataclass(frozen=True, slots=True)
class DecompositionSeal:
    """One immutable approved-tree seal bound to its draft digest."""

    requirements_epoch: str
    draft_digest: str
    nodes: tuple[dict[str, Any], ...]

    @property
    def seal_digest(self) -> str:
        """Return the stable digest of the sealed tree payload."""
        encoded = json.dumps(
            list(self.nodes), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def payload(self) -> dict[str, Any]:
        """Return the canonical record payload."""
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "requirements_epoch": self.requirements_epoch,
            "draft_digest": self.draft_digest,
            "nodes": list(self.nodes),
        }


def publish_decomposition_seal(
    issue_number: int,
    seal: DecompositionSeal,
    github: PlanJournalGitHub,
) -> DecompositionSeal:
    """Publish or recover the immutable approved-tree seal."""
    _upsert_record(
        issue_number,
        SEAL_MARKER_PREFIX,
        seal.requirements_epoch,
        seal.payload(),
        github,
    )
    return seal


def recover_decomposition_seal(
    comments: Sequence[IssueComment],
    epoch: str,
    *,
    draft_digest: str | None = None,
) -> DecompositionSeal | None:
    """Recover the approved seal for one epoch, optionally bound to a draft."""
    for key, raw in _parse_records(comments, SEAL_MARKER_PREFIX):
        if key != epoch:
            continue
        payload = _decode_payload(key, raw)
        if payload is None:
            return None
        payload.pop("_key", None)
        bound = payload.get("draft_digest")
        nodes = payload.get("nodes")
        if (
            payload.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(bound, str)
            or len(bound) != 64
            or (draft_digest is not None and bound != draft_digest)
            or not isinstance(nodes, (list, tuple))
            or not nodes
            or any(not isinstance(node, dict) for node in nodes)
        ):
            return None
        return DecompositionSeal(
            requirements_epoch=key,
            draft_digest=bound,
            nodes=tuple(_canonicalize(node) for node in nodes),
        )
    return None


@dataclass(frozen=True, slots=True)
class DecompositionBindings:
    """One node-id to GitHub issue binding set bound to its seal."""

    requirements_epoch: str
    seal_digest: str
    bindings: tuple[tuple[str, int], ...]

    def payload(self) -> dict[str, Any]:
        """Return the canonical record payload."""
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "requirements_epoch": self.requirements_epoch,
            "seal_digest": self.seal_digest,
            "bindings": dict(self.bindings),
        }

    def issue_for(self, node_id: str) -> int | None:
        """Return the bound issue number for one node id."""
        for bound_node, issue in self.bindings:
            if bound_node == node_id:
                return issue
        return None


def publish_decomposition_bindings(
    issue_number: int,
    bindings: DecompositionBindings,
    github: PlanJournalGitHub,
) -> DecompositionBindings:
    """Publish or recover the child bindings for one seal."""
    _upsert_record(
        issue_number,
        BINDINGS_MARKER_PREFIX,
        bindings.requirements_epoch,
        bindings.payload(),
        github,
    )
    return bindings


def recover_decomposition_bindings(
    comments: Sequence[IssueComment],
    epoch: str,
    *,
    seal_digest: str | None = None,
) -> DecompositionBindings | None:
    """Recover the bindings for one epoch, optionally bound to one seal."""
    for key, raw in _parse_records(comments, BINDINGS_MARKER_PREFIX):
        if key != epoch:
            continue
        payload = _decode_payload(key, raw)
        if payload is None:
            return None
        payload.pop("_key", None)
        bound_seal = payload.get("seal_digest")
        raw_bindings = payload.get("bindings")
        if (
            payload.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(bound_seal, str)
            or len(bound_seal) != 64
            or (seal_digest is not None and bound_seal != seal_digest)
            or not isinstance(raw_bindings, dict)
            or not raw_bindings
            or any(
                not isinstance(node_id, str) or not node_id or type(issue) is not int or issue < 1
                for node_id, issue in raw_bindings.items()
            )
        ):
            return None
        return DecompositionBindings(
            requirements_epoch=key,
            seal_digest=bound_seal,
            bindings=tuple(sorted((str(node), issue) for node, issue in raw_bindings.items())),
        )
    return None


@dataclass(frozen=True, slots=True)
class DecompositionTransitionReceipt:
    """One parent-transition receipt with the confirmed label state."""

    requirements_epoch: str
    labels_added: tuple[str, ...]
    labels_removed: tuple[str, ...]
    needs_plan_confirmed: tuple[str, ...]
    summary_confirmed: bool

    def payload(self) -> dict[str, Any]:
        """Return the canonical record payload."""
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "requirements_epoch": self.requirements_epoch,
            "labels_added": list(self.labels_added),
            "labels_removed": list(self.labels_removed),
            "needs_plan_confirmed": list(self.needs_plan_confirmed),
            "summary_confirmed": self.summary_confirmed,
        }


def publish_decomposition_transition(
    issue_number: int,
    receipt: DecompositionTransitionReceipt,
    github: PlanJournalGitHub,
) -> DecompositionTransitionReceipt:
    """Publish or recover the parent-transition receipt for one epoch."""
    _upsert_record(
        issue_number,
        PARENT_MARKER_PREFIX,
        receipt.requirements_epoch,
        receipt.payload(),
        github,
    )
    return receipt


def recover_decomposition_transition(
    comments: Sequence[IssueComment],
    epoch: str,
) -> DecompositionTransitionReceipt | None:
    """Recover the parent-transition receipt for one epoch, or None."""
    for key, raw in _parse_records(comments, PARENT_MARKER_PREFIX):
        if key != epoch:
            continue
        payload = _decode_payload(key, raw)
        if payload is None:
            return None
        payload.pop("_key", None)
        added = payload.get("labels_added")
        removed = payload.get("labels_removed")
        confirmed = payload.get("needs_plan_confirmed")
        if (
            payload.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(added, (list, tuple))
            or not isinstance(removed, (list, tuple))
            or not isinstance(confirmed, (list, tuple))
            or any(not isinstance(name, str) or not name for name in (*added, *removed, *confirmed))
            or not isinstance(payload.get("summary_confirmed"), bool)
        ):
            return None
        return DecompositionTransitionReceipt(
            requirements_epoch=key,
            labels_added=tuple(added),
            labels_removed=tuple(removed),
            needs_plan_confirmed=tuple(confirmed),
            summary_confirmed=payload["summary_confirmed"],
        )
    return None


_DRAFT_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-decomposition-draft:v1:"


def _draft_part_marker(epoch: str, part: int, total: int) -> str:
    """Return the chunked draft marker for one part."""
    return f"{_DRAFT_MARKER_PREFIX}{epoch}:part={part}-{total} -->"


def _chunk_tree(tree_json: str) -> tuple[str, ...]:
    """Split one serialized tree into bounded canonical parts."""
    if len(tree_json) <= DRAFT_PART_MAX_CHARS:
        return (tree_json,)
    parts: list[str] = []
    remaining = tree_json
    while remaining:
        parts.append(remaining[:DRAFT_PART_MAX_CHARS])
        remaining = remaining[DRAFT_PART_MAX_CHARS:]
    return tuple(parts)


def publish_decomposition_draft(
    issue_number: int,
    epoch: str,
    draft_digest: str,
    nodes: Sequence[Mapping[str, Any]],
    github: PlanJournalGitHub,
) -> int:
    """Publish the complete normalized draft as chunked manifest parts."""
    tree_json = json.dumps(
        [dict(node) for node in nodes],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    parts = _chunk_tree(tree_json)
    total = len(parts)
    for part, chunk in enumerate(parts, start=1):
        payload = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "requirements_epoch": epoch,
            "draft_digest": draft_digest,
            "part": part,
            "parts": total,
            "tree_fragment": chunk,
        }
        _upsert_record(
            issue_number,
            _DRAFT_MARKER_PREFIX,
            f"{epoch}:part={part}-{total}",
            payload,
            github,
        )
    return total


def recover_decomposition_draft(
    comments: Sequence[IssueComment],
    epoch: str,
    draft_digest: str | None = None,
) -> tuple[dict[str, Any], ...] | None:
    """Recover the complete draft tree for one epoch from its parts."""
    fragments: dict[int, str] = {}
    total: int | None = None
    for key, raw in _parse_records(comments, _DRAFT_MARKER_PREFIX):
        payload = _decode_payload(key, raw)
        if payload is None or payload.get("requirements_epoch") != epoch:
            continue
        payload.pop("_key", None)
        bound = payload.get("draft_digest")
        if (
            payload.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(bound, str)
            or (draft_digest is not None and bound != draft_digest)
            or type(payload.get("part")) is not int
            or type(payload.get("parts")) is not int
            or not isinstance(payload.get("tree_fragment"), str)
        ):
            continue
        if total is None:
            total = payload["parts"]
        if total != payload["parts"]:
            return None
        fragments[payload["part"]] = payload["tree_fragment"]
    if total is None or sorted(fragments) != list(range(1, total + 1)):
        return None
    try:
        tree = json.loads("".join(fragments[part] for part in range(1, total + 1)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(tree, list) or any(not isinstance(node, dict) for node in tree):
        return None
    return tuple(_canonicalize(node) for node in tree)


def render_decomposition_summary_body(
    *,
    parent_issue: int,
    epoch: str,
    ordered_issues: Sequence[tuple[str, int]],
    layers: Sequence[Sequence[str]],
    bindings: Mapping[str, int],
) -> str:
    """Render the actor-owned parent summary with the dependency tree."""
    tree_lines = [
        f"  {node_id} -> #{bindings[node_id]}"
        for layer in layers
        for node_id in layer
        if node_id in bindings
    ]
    issue_lines = [f"- {node_id}: #{issue}" for node_id, issue in ordered_issues]
    payload = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "parent": parent_issue,
        "requirements_epoch": epoch,
        "ordered_issues": [
            {"node_id": node_id, "issue": issue} for node_id, issue in ordered_issues
        ],
        "layers": [list(layer) for layer in layers],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (
        f"Parent #{parent_issue} is now an epic of single-pull-request issues.\n\n"
        "## Ordered issues\n\n"
        + "\n".join(issue_lines)
        + "\n\n## Dependency tree\n\n"
        + "\n".join(tree_lines)
        + f"\n\n```json\n{encoded}\n```"
    )
