"""Bind admitted validation evidence to one immutable Comet review plan.

These data checks do not prove source, CI, or runtime authority. The host
adapters must establish that authority before they construct these records.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding, WorkspaceKind

type SourceDigests = tuple[tuple[str, int, str], ...]
type EvidenceKind = Literal["ci", "local"]
type ReceiptStatus = Literal["success", "failed"]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _sha(value: object, length: int) -> bool:
    return type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _path(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 1024
        and "\0" not in value
        and "\\" not in value
        and ":" not in value
        and not PurePosixPath(value).is_absolute()
        and all(part not in {"", ".", "..", ".git"} for part in value.split("/"))
    )


def _canonical(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical(getattr(value, item.name)) for item in fields(value)}
    if type(value) is tuple:
        return [_canonical(item) for item in value]
    if value is None or type(value) in {str, int, bool}:
        return value
    raise ValueError("The validation record contains an unsupported value.")


def _digest(value: object, identity_field: str) -> str:
    if not is_dataclass(value) or isinstance(value, type):
        raise ValueError("A record is necessary.")
    payload = {
        "kind": type(value).__name__,
        "value": {
            item.name: _canonical(getattr(value, item.name))
            for item in fields(value)
            if item.name != identity_field
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_command(argv: tuple[str, ...], source_digests: SourceDigests) -> None:
    _require(type(argv) is tuple and 0 < len(argv) <= 64, "The command vector is invalid.")
    _require(
        all(type(arg) is str and 0 < len(arg) <= 4096 and "\0" not in arg for arg in argv),
        "The command argument is invalid.",
    )
    _require(
        type(source_digests) is tuple and 0 < len(source_digests) <= 64,
        "The command source inventory is invalid.",
    )
    names: set[str] = set()
    for source in source_digests:
        _require(type(source) is tuple and len(source) == 3, "The source record is invalid.")
        path, size, digest = source
        _require(_path(path) and path not in names, "The source path is invalid or repeated.")
        _require(type(size) is int and 0 <= size <= 16 * 1024 * 1024, "The source size is invalid.")
        _require(_sha(digest, 64), "The source digest is invalid.")
        names.add(path)


@dataclass(frozen=True, slots=True)
class RepositoryValidationCheck:
    """Describe one check selected by a host-owned workflow profile."""

    check_id: str
    argv: tuple[str, ...]
    source_digests: SourceDigests

    def __post_init__(self) -> None:
        """Reject an invalid check description."""
        _validate_check(self)


def _validate_check(check: RepositoryValidationCheck) -> None:
    _require(type(check) is RepositoryValidationCheck, "The check type is invalid.")
    _require(
        type(check.check_id) is str
        and re.fullmatch(r"comet\.[a-z0-9.-]{1,100}", check.check_id) is not None,
        "The check identifier is invalid.",
    )
    _validate_command(check.argv, check.source_digests)


@dataclass(frozen=True, slots=True)
class RepositoryValidationPlan:
    """Keep the complete identity and selection of one review attempt."""

    repository: str
    issue_number: int | None
    pr_number: int
    reviewed_head: str
    reviewed_base: str
    source_workspace: WorkspaceBinding
    changes: tuple[tuple[str, str], ...]
    profile_id: str
    profile_digest: str
    checks: tuple[RepositoryValidationCheck, ...]
    diff_base_sha: str | None = None
    execution_allowed: bool = True
    coverage_reason: str = ""
    schema_version: int = 1
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the plan and store its complete identity."""
        _validate_plan(self)
        object.__setattr__(self, "plan_id", _digest(self, "plan_id"))


def repository_workspace_name_matches(repository: str, workspace_repository: object) -> bool:
    """Accept the full repository name or its local workspace name."""
    return type(workspace_repository) is str and workspace_repository.casefold() in {
        repository.casefold(),
        "comet" if repository == "LLM360/comet" else repository.casefold(),
    }


def _validate_workspace(plan: RepositoryValidationPlan) -> None:
    workspace = plan.source_workspace
    _require(type(workspace) is WorkspaceBinding, "The source binding type is invalid.")
    _require(
        workspace.kind is WorkspaceKind.SOURCE
        and workspace.lane is SourceLane.REVIEW
        and workspace.detached is True
        and type(workspace.schema_version) is int
        and workspace.schema_version == 1
        and workspace.dirty_claim is None,
        "An immutable review source binding is necessary.",
    )
    _require(
        repository_workspace_name_matches(plan.repository, workspace.repository)
        and workspace.revision == plan.reviewed_head
        and type(workspace.item_number) is int
        and workspace.item_number == (plan.issue_number or plan.pr_number),
        "The source binding does not match the review.",
    )
    _require(
        isinstance(workspace.cwd, Path)
        and workspace.cwd.is_absolute()
        and isinstance(workspace.reusable_root, Path)
        and workspace.reusable_root.is_absolute()
        and type(workspace.ownership_key) is str
        and bool(workspace.ownership_key)
        and type(workspace.generation) is int
        and workspace.generation >= 0,
        "The source ownership binding is incomplete.",
    )


def _validate_plan(plan: RepositoryValidationPlan) -> None:
    _require(type(plan) is RepositoryValidationPlan, "The validation plan type is invalid.")
    _require(type(plan.schema_version) is int and plan.schema_version == 1, "Unknown plan schema.")
    _require(plan.repository == "LLM360/comet", "The validation repository is unsupported.")
    _require(_positive(plan.pr_number), "The pull-request number is invalid.")
    _require(
        plan.issue_number is None or _positive(plan.issue_number), "The issue number is invalid."
    )
    _require(
        _sha(plan.reviewed_head, 40) and _sha(plan.reviewed_base, 40), "Invalid review revision."
    )
    _validate_workspace(plan)
    _require(
        type(plan.profile_id) is str
        and re.fullmatch(r"comet-[a-z0-9-]{1,64}", plan.profile_id) is not None
        and _sha(plan.profile_digest, 64),
        "The workflow profile binding is invalid.",
    )
    _require(type(plan.changes) is tuple and len(plan.changes) <= 4096, "Invalid change inventory.")
    paths: set[str] = set()
    for change in plan.changes:
        _require(type(change) is tuple and len(change) == 2, "The change record is invalid.")
        status, path = change
        _require(type(status) is str and status in {"A", "M", "D"}, "Unsupported change status.")
        _require(_path(path) and path not in paths, "The change path is invalid or repeated.")
        paths.add(path)
    _require(
        type(plan.checks) is tuple and len(plan.checks) <= 32, "The check selection is invalid."
    )
    identifiers: set[str] = set()
    for check in plan.checks:
        _validate_check(check)
        _require(
            check.check_id not in identifiers, "The check selection has a repeated identifier."
        )
        identifiers.add(check.check_id)
    _require(type(plan.execution_allowed) is bool, "The execution decision is invalid.")
    _require(
        _sha(plan.diff_base_sha, 40) or (plan.diff_base_sha is None and not plan.execution_allowed),
        "The diff base is invalid or missing from an executable plan.",
    )
    _require(
        type(plan.coverage_reason) is str
        and len(plan.coverage_reason) <= 256
        and (plan.execution_allowed or bool(plan.coverage_reason)),
        "The coverage reason is invalid.",
    )


@dataclass(frozen=True, slots=True)
class RepositoryValidationReceipt:
    """Keep normalized evidence after its host adapter admits the provenance."""

    repository: str
    pr_number: int
    plan_id: str
    check_id: str
    reviewed_head: str
    reviewed_base: str
    argv: tuple[str, ...]
    source_digests: SourceDigests
    evidence_kind: EvidenceKind
    status: ReceiptStatus
    schema_version: int = 1
    receipt_id: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the receipt and store its complete identity."""
        _validate_receipt(self)
        object.__setattr__(self, "receipt_id", _digest(self, "receipt_id"))


def _validate_receipt(receipt: RepositoryValidationReceipt) -> None:
    _require(type(receipt) is RepositoryValidationReceipt, "The receipt type is invalid.")
    _require(
        type(receipt.schema_version) is int and receipt.schema_version == 1,
        "Unknown receipt schema.",
    )
    _require(receipt.repository == "LLM360/comet", "The receipt repository is unsupported.")
    _require(_positive(receipt.pr_number) and _sha(receipt.plan_id, 64), "Invalid receipt context.")
    _require(
        _sha(receipt.reviewed_head, 40) and _sha(receipt.reviewed_base, 40),
        "Invalid receipt revision.",
    )
    RepositoryValidationCheck(receipt.check_id, receipt.argv, receipt.source_digests)
    _require(
        type(receipt.evidence_kind) is str and receipt.evidence_kind in {"ci", "local"},
        "The evidence kind is invalid.",
    )
    _require(
        type(receipt.status) is str and receipt.status in {"success", "failed"}, "Invalid result."
    )


@dataclass(frozen=True, slots=True)
class RepositoryValidationGap:
    """Identify one check or plan boundary without acceptable evidence."""

    check_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RepositoryValidationCoverage:
    """Return admitted records, unresolved checks, and blocking evidence gaps."""

    status: Literal["complete", "gap"]
    receipts: tuple[RepositoryValidationReceipt, ...] = ()
    gaps: tuple[RepositoryValidationGap, ...] = ()
    uncovered_check_ids: tuple[str, ...] = ()


def _receipt_matches(
    plan: RepositoryValidationPlan,
    receipt: RepositoryValidationReceipt,
    check: RepositoryValidationCheck,
) -> bool:
    return (
        receipt.repository == plan.repository
        and receipt.pr_number == plan.pr_number
        and receipt.plan_id == plan.plan_id
        and receipt.reviewed_head == plan.reviewed_head
        and receipt.reviewed_base == plan.reviewed_base
        and receipt.argv == check.argv
        and receipt.source_digests == check.source_digests
    )


def merge_repository_validation_receipts(
    plan: RepositoryValidationPlan,
    receipts: tuple[RepositoryValidationReceipt, ...],
    *,
    reviewed_head: str,
    reviewed_base: str,
) -> RepositoryValidationCoverage:
    """Combine admitted evidence without changing the plan or querying a runtime."""
    try:
        _validate_plan(plan)
        _require(plan.plan_id == _digest(plan, "plan_id"), "The validation plan changed.")
    except (AttributeError, TypeError, ValueError):
        return RepositoryValidationCoverage(
            "gap", gaps=(RepositoryValidationGap("*", "validation_plan_invalid"),)
        )
    checks = {check.check_id: check for check in plan.checks}

    def blocked(reason: str) -> RepositoryValidationCoverage:
        return RepositoryValidationCoverage(
            "gap",
            gaps=(RepositoryValidationGap("*", reason),),
            uncovered_check_ids=tuple(checks),
        )

    if reviewed_head != plan.reviewed_head or reviewed_base != plan.reviewed_base:
        return blocked("reviewed_revision_mismatch")
    if not plan.execution_allowed or plan.coverage_reason:
        return blocked(plan.coverage_reason or "validation_execution_not_allowed")
    if not checks or not plan.changes:
        return blocked("validation_selection_empty")
    if type(receipts) is not tuple or len(receipts) > 128:
        return blocked("validation_receipt_inventory_invalid")

    gaps: list[RepositoryValidationGap] = []
    admitted: list[RepositoryValidationReceipt] = []
    seen: set[tuple[EvidenceKind, str]] = set()
    duplicates: set[tuple[EvidenceKind, str]] = set()
    for receipt in receipts:
        try:
            _validate_receipt(receipt)
            _require(receipt.receipt_id == _digest(receipt, "receipt_id"), "The receipt changed.")
            check = checks.get(receipt.check_id)
            if check is None:
                raise ValueError("The receipt has no selected check.")
            _require(_receipt_matches(plan, receipt, check), "The receipt context changed.")
        except (AttributeError, TypeError, ValueError):
            # A rejected record does not identify an authoritative evidence kind.
            gaps.append(RepositoryValidationGap("*", "validation_receipt_invalid"))
            continue
        key = (receipt.evidence_kind, receipt.check_id)
        if key in seen:
            duplicates.add(key)
            gaps.append(RepositoryValidationGap(receipt.check_id, "validation_receipt_duplicate"))
        seen.add(key)
        if receipt.status == "failed":
            gaps.append(RepositoryValidationGap(receipt.check_id, "validation_check_failed"))
        admitted.append(receipt)

    retained = tuple(
        receipt
        for receipt in admitted
        if (receipt.evidence_kind, receipt.check_id) not in duplicates or receipt.status == "failed"
    )
    covered = {receipt.check_id for receipt in retained if receipt.status == "success"}
    uncovered = tuple(check_id for check_id in checks if check_id not in covered)
    gaps.extend(
        RepositoryValidationGap(check_id, "validation_check_uncovered") for check_id in uncovered
    )
    return RepositoryValidationCoverage(
        "gap" if gaps else "complete", retained, tuple(gaps), uncovered
    )


@dataclass(frozen=True, slots=True)
class RepositoryValidationInvocation:
    """Bind one requested evidence source and check set to a live attempt."""

    plan: RepositoryValidationPlan
    generation: int
    request_nonce: str
    evidence_kind: EvidenceKind
    check_ids: tuple[str, ...]
    request_id: str = field(init=False)

    def __post_init__(self) -> None:
        """Reject incomplete request identity and calculate its digest."""
        _validate_invocation(self)
        object.__setattr__(self, "request_id", _digest(self, "request_id"))


def _validate_bound_plan(plan: RepositoryValidationPlan) -> None:
    _validate_plan(plan)
    _require(plan.plan_id == _digest(plan, "plan_id"), "The validation plan changed.")


def _validate_invocation(invocation: RepositoryValidationInvocation) -> None:
    _require(type(invocation) is RepositoryValidationInvocation, "The invocation type is invalid.")
    _validate_bound_plan(invocation.plan)
    _require(_positive(invocation.generation), "The attempt generation is invalid.")
    _require(_sha(invocation.request_nonce, 32), "The request nonce is invalid.")
    _require(
        type(invocation.evidence_kind) is str and invocation.evidence_kind in {"ci", "local"},
        "The requested evidence kind is invalid.",
    )
    _require(
        type(invocation.check_ids) is tuple
        and 0 < len(invocation.check_ids) <= 32
        and all(type(check_id) is str for check_id in invocation.check_ids),
        "The requested check inventory is invalid.",
    )
    _require(
        len(set(invocation.check_ids)) == len(invocation.check_ids)
        and set(invocation.check_ids) <= {check.check_id for check in invocation.plan.checks},
        "The requested checks do not match the plan.",
    )


def _checked_invocation(invocation: RepositoryValidationInvocation) -> None:
    _validate_invocation(invocation)
    _require(invocation.request_id == _digest(invocation, "request_id"), "The invocation changed.")


def validate_repository_validation_invocation(invocation: RepositoryValidationInvocation) -> None:
    """Check the complete identity of an existing validation invocation."""
    _checked_invocation(invocation)


def _validate_terminal_gap(gap: RepositoryValidationGap, plan: RepositoryValidationPlan) -> None:
    _require(type(gap) is RepositoryValidationGap, "The terminal gap type is invalid.")
    _require(
        type(gap.check_id) is str
        and gap.check_id in {"*", *(check.check_id for check in plan.checks)}
        and type(gap.reason) is str
        and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", gap.reason) is not None,
        "The terminal gap code is invalid.",
    )


@dataclass(frozen=True, slots=True)
class RepositoryValidationAttempt:
    """Keep pending ownership, admitted evidence, and permanent live gaps."""

    plan: RepositoryValidationPlan
    generation: int
    request_nonce: str
    pending: RepositoryValidationInvocation | None = None
    receipts: tuple[RepositoryValidationReceipt, ...] = ()
    gaps: tuple[RepositoryValidationGap, ...] = ()
    consumed_request_ids: tuple[str, ...] = ()
    invalidated_slots: tuple[tuple[EvidenceKind, str], ...] = ()
    attempt_id: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the complete state before it can replace a prior attempt."""
        _validate_attempt(self)
        object.__setattr__(self, "attempt_id", _digest(self, "attempt_id"))


def _invocation_matches_attempt(
    invocation: RepositoryValidationInvocation, attempt: RepositoryValidationAttempt
) -> bool:
    return (
        invocation.plan.plan_id == attempt.plan.plan_id
        and invocation.generation == attempt.generation
        and invocation.request_nonce == attempt.request_nonce
    )


def _validate_attempt(attempt: RepositoryValidationAttempt) -> None:
    _require(type(attempt) is RepositoryValidationAttempt, "The attempt type is invalid.")
    _validate_bound_plan(attempt.plan)
    _require(_positive(attempt.generation), "The attempt generation is invalid.")
    _require(_sha(attempt.request_nonce, 32), "The attempt nonce is invalid.")
    _require(
        type(attempt.consumed_request_ids) is tuple
        and len(attempt.consumed_request_ids) <= 64
        and all(_sha(value, 64) for value in attempt.consumed_request_ids)
        and len(set(attempt.consumed_request_ids)) == len(attempt.consumed_request_ids),
        "The consumed request inventory is invalid.",
    )
    if attempt.pending is not None:
        _checked_invocation(attempt.pending)
        _require(
            _invocation_matches_attempt(attempt.pending, attempt)
            and attempt.pending.request_id not in attempt.consumed_request_ids,
            "The pending request does not belong to this attempt.",
        )
    _require(
        type(attempt.receipts) is tuple and len(attempt.receipts) <= 128,
        "The attempt receipt inventory is invalid.",
    )
    checks = {check.check_id: check for check in attempt.plan.checks}
    _require(
        type(attempt.invalidated_slots) is tuple and len(attempt.invalidated_slots) <= 64,
        "The invalidated evidence inventory is invalid.",
    )
    for slot in attempt.invalidated_slots:
        _require(
            type(slot) is tuple
            and len(slot) == 2
            and type(slot[0]) is str
            and slot[0] in {"ci", "local"}
            and type(slot[1]) is str
            and slot[1] in checks,
            "The invalidated evidence slot is invalid.",
        )
    _require(
        len(set(attempt.invalidated_slots)) == len(attempt.invalidated_slots),
        "The invalidated evidence inventory repeats a slot.",
    )
    for receipt in attempt.receipts:
        _validate_receipt(receipt)
        _require(
            receipt.receipt_id == _digest(receipt, "receipt_id")
            and receipt.check_id in checks
            and _receipt_matches(attempt.plan, receipt, checks[receipt.check_id])
            and (
                receipt.status == "failed"
                or (receipt.evidence_kind, receipt.check_id) not in attempt.invalidated_slots
            ),
            "The attempt receipt does not match its plan.",
        )
    _require(
        type(attempt.gaps) is tuple and len(attempt.gaps) <= 64,
        "The attempt gap inventory is invalid.",
    )
    for gap in attempt.gaps:
        _validate_terminal_gap(gap, attempt.plan)


def _checked_attempt(attempt: RepositoryValidationAttempt) -> None:
    _validate_attempt(attempt)
    _require(attempt.attempt_id == _digest(attempt, "attempt_id"), "The attempt changed.")


def _terminal_gaps(
    *groups: tuple[RepositoryValidationGap, ...],
) -> tuple[RepositoryValidationGap, ...]:
    retained: list[RepositoryValidationGap] = []
    for group in groups:
        for gap in group:
            if gap not in retained:
                if len(retained) == 63:
                    return (*retained, RepositoryValidationGap("*", "validation_gap_limit"))
                retained.append(gap)
    return tuple(retained)


def begin_validation_request(
    attempt: RepositoryValidationAttempt,
    evidence_kind: EvidenceKind,
    check_ids: tuple[str, ...],
) -> tuple[RepositoryValidationAttempt, RepositoryValidationInvocation]:
    """Set pending ownership before the coordinator submits a worker job."""
    _checked_attempt(attempt)
    _require(attempt.pending is None, "A validation request is already pending.")
    _require(
        attempt.plan.execution_allowed and not attempt.plan.coverage_reason,
        "The validation plan does not permit execution.",
    )
    invocation = RepositoryValidationInvocation(
        attempt.plan, attempt.generation, attempt.request_nonce, evidence_kind, check_ids
    )
    _require(
        invocation.request_id not in attempt.consumed_request_ids
        and len(attempt.consumed_request_ids) < 64,
        "The validation request was consumed or the request limit was reached.",
    )
    return replace(attempt, pending=invocation), invocation


def _retain_attempt_receipts(
    attempt: RepositoryValidationAttempt,
    accepted: list[RepositoryValidationReceipt],
    *,
    duplicate: bool,
) -> tuple[tuple[RepositoryValidationReceipt, ...], tuple[tuple[EvidenceKind, str], ...]]:
    """Keep a duplicate evidence slot invalid for the rest of the attempt."""
    invalidated = set(attempt.invalidated_slots)
    seen: set[tuple[EvidenceKind, str]] = set()
    for receipt in (*attempt.receipts, *accepted):
        slot = (receipt.evidence_kind, receipt.check_id)
        if slot in seen:
            invalidated.add(slot)
        seen.add(slot)
    if duplicate:
        invalidated.update((receipt.evidence_kind, receipt.check_id) for receipt in accepted)
    retained = tuple(
        receipt
        for receipt in (*attempt.receipts, *accepted)
        if receipt.status == "failed"
        or (receipt.evidence_kind, receipt.check_id) not in invalidated
    )
    return retained, tuple(sorted(invalidated))


def consume_validation_result(
    attempt: RepositoryValidationAttempt,
    invocation: object,
    receipts: object,
    gaps: object = (),
) -> RepositoryValidationAttempt:
    """Consume a correlated callback and preserve each terminal failure."""
    _checked_attempt(attempt)

    def rejected(reason: str) -> RepositoryValidationAttempt:
        return replace(
            attempt, gaps=_terminal_gaps(attempt.gaps, (RepositoryValidationGap("*", reason),))
        )

    try:
        if not isinstance(invocation, RepositoryValidationInvocation):
            raise ValueError("The callback type is invalid.")
        _checked_invocation(invocation)
        _require(_invocation_matches_attempt(invocation, attempt), "Stale callback identity.")
    except (AttributeError, TypeError, ValueError):
        return rejected("validation_callback_identity_invalid")
    duplicate = invocation.request_id in attempt.consumed_request_ids
    if not duplicate and (
        attempt.pending is None or attempt.pending.request_id != invocation.request_id
    ):
        return rejected("validation_callback_out_of_order")
    new_gaps: list[RepositoryValidationGap] = []
    if duplicate:
        new_gaps.append(RepositoryValidationGap("*", "validation_callback_duplicate"))
    accepted: list[RepositoryValidationReceipt] = []
    if type(receipts) is not tuple or len(receipts) + len(attempt.receipts) > 128:
        new_gaps.append(RepositoryValidationGap("*", "validation_callback_receipts_invalid"))
    else:
        for receipt in receipts:
            try:
                _validate_receipt(receipt)
                _require(
                    receipt.evidence_kind == invocation.evidence_kind
                    and receipt.check_id in invocation.check_ids
                    and receipt.receipt_id == _digest(receipt, "receipt_id")
                    and _receipt_matches(
                        attempt.plan,
                        receipt,
                        next(c for c in attempt.plan.checks if c.check_id == receipt.check_id),
                    ),
                    "The receipt was not requested.",
                )
                accepted.append(receipt)
            except (AttributeError, TypeError, ValueError):
                new_gaps.append(RepositoryValidationGap("*", "validation_callback_receipt_invalid"))
    if type(gaps) is not tuple or len(gaps) > 64:
        new_gaps.append(RepositoryValidationGap("*", "validation_callback_gaps_invalid"))
    else:
        for gap in gaps:
            try:
                _validate_terminal_gap(gap, attempt.plan)
                new_gaps.append(gap)
            except (AttributeError, TypeError, ValueError):
                new_gaps.append(RepositoryValidationGap("*", "validation_callback_gap_invalid"))
    retained, invalidated = _retain_attempt_receipts(attempt, accepted, duplicate=duplicate)
    new_gaps.extend(
        RepositoryValidationGap(check_id, "validation_receipt_duplicate")
        for _, check_id in invalidated
    )
    coverage = merge_repository_validation_receipts(
        attempt.plan,
        retained,
        reviewed_head=attempt.plan.reviewed_head,
        reviewed_base=attempt.plan.reviewed_base,
    )
    terminal = tuple(gap for gap in coverage.gaps if gap.reason != "validation_check_uncovered")
    return replace(
        attempt,
        pending=attempt.pending if duplicate else None,
        receipts=coverage.receipts,
        invalidated_slots=tuple(sorted(invalidated)),
        gaps=_terminal_gaps(attempt.gaps, tuple(new_gaps), terminal),
        consumed_request_ids=(
            attempt.consumed_request_ids
            if duplicate
            else (*attempt.consumed_request_ids, invocation.request_id)
        ),
    )


def validation_attempt_coverage(
    attempt: RepositoryValidationAttempt, *, reviewed_head: str, reviewed_base: str
) -> RepositoryValidationCoverage:
    """Recheck live evidence without discarding callback or provider gaps."""
    try:
        _checked_attempt(attempt)
    except (AttributeError, TypeError, ValueError):
        return RepositoryValidationCoverage(
            "gap", gaps=(RepositoryValidationGap("*", "validation_attempt_invalid"),)
        )
    coverage = merge_repository_validation_receipts(
        attempt.plan, attempt.receipts, reviewed_head=reviewed_head, reviewed_base=reviewed_base
    )
    pending = (
        (RepositoryValidationGap("*", "validation_request_pending"),)
        if attempt.pending is not None
        else ()
    )
    combined = _terminal_gaps(attempt.gaps, coverage.gaps, pending)
    return RepositoryValidationCoverage(
        "complete" if coverage.status == "complete" and not combined else "gap",
        coverage.receipts,
        combined,
        coverage.uncovered_check_ids,
    )


@dataclass(frozen=True, slots=True)
class RepositoryValidationExecution:
    """Bind one local check to its attempt and admitted runtime identities.

    These data checks do not establish runtime or source authority. The worker
    must recheck both before it starts a process.
    """

    plan: RepositoryValidationPlan
    check_id: str
    attempt_generation: int
    request_nonce: str
    runtime_root: Path
    runtime_manifest_sha256: str
    runtime_tree_sha256: str

    def __post_init__(self) -> None:
        """Reject incomplete or conflicting execution metadata."""
        validate_repository_validation_execution(self)


def validate_repository_validation_execution(
    execution: RepositoryValidationExecution,
) -> RepositoryValidationCheck:
    """Recheck the frozen value and return its selected command."""
    _require(type(execution) is RepositoryValidationExecution, "The execution type is invalid.")
    _require(
        type(execution.plan) is RepositoryValidationPlan, "The execution plan type is invalid."
    )
    plan = execution.plan
    _validate_bound_plan(plan)
    _require(plan.execution_allowed, "The validation plan does not permit execution.")
    _require(type(execution.check_id) is str, "The execution check identifier is invalid.")
    checks = tuple(check for check in plan.checks if check.check_id == execution.check_id)
    _require(len(checks) == 1, "The execution check is not selected by the plan.")
    check = checks[0]
    _require(_positive(execution.attempt_generation), "The execution generation is invalid.")
    _require(_sha(execution.request_nonce, 32), "The execution request nonce is invalid.")
    _require(
        _sha(execution.runtime_manifest_sha256, 64) and _sha(execution.runtime_tree_sha256, 64),
        "The execution runtime digests are invalid.",
    )
    sources = {path: digest for path, _, digest in check.source_digests}
    _require(
        "pyproject.toml" in sources and "uv.lock" in sources,
        "The execution dependency bindings are missing.",
    )
    root = execution.runtime_root
    _require(
        isinstance(root, Path)
        and root.is_absolute()
        and ".." not in root.parts
        and root.parts[-4:]
        == ("build", "hephaestus-review-validation", "comet", sources["uv.lock"]),
        "The execution runtime path does not match the capability layout.",
    )
    return check


@dataclass(frozen=True, slots=True)
class RepositoryValidationLocalRead:
    """Keep one local result with its complete execution request."""

    execution: RepositoryValidationExecution
    receipt: RepositoryValidationReceipt | None = None
    gap: RepositoryValidationGap | None = None

    def __post_init__(self) -> None:
        """Reject evidence from another command or validation plan."""
        check = validate_repository_validation_execution(self.execution)
        _require((self.receipt is None) != (self.gap is None), "One result is required.")
        if self.receipt is not None:
            _validate_receipt(self.receipt)
            _require(
                self.receipt.evidence_kind == "local"
                and self.receipt.check_id == check.check_id
                and self.receipt.receipt_id == _digest(self.receipt, "receipt_id")
                and _receipt_matches(self.execution.plan, self.receipt, check),
                "The local receipt does not match the execution request.",
            )
        else:
            _require(
                type(self.gap) is RepositoryValidationGap
                and self.gap.check_id == check.check_id
                and type(self.gap.reason) is str
                and 0 < len(self.gap.reason) <= 256,
                "The local gap does not match the execution request.",
            )
