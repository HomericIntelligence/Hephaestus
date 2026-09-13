"""Bounded finding journals and review receipt transitions."""

from collections.abc import Callable
from typing import cast

from hephaestus.automation.review_anchors import (
    ReviewAnchorCorrection,
)
from hephaestus.automation.review_finding_history import normalize_review_finding_batch_records

from ..github_jobs import PrReviewReconciled
from .base import Continue, Disposition, StageOutcome, StepResult, WorkItem
from .pr_review_threads import (
    _ANCHOR_CORRECTION_RETRY,
    _COMMENT_VALIDATION_ONLY,
    BLOCKING_SEVERITIES,
    EVAL,
    REVIEW_WAIT,
    _clear_round_review_state,
    _issue_number,
    logger,
)


def _finding_anchor(finding: dict[str, object]) -> dict[str, object]:
    """Return the host-owned anchor fields for one review finding."""
    return {
        "path": str(finding.get("path") or "").strip(),
        "line": finding.get("line"),
        "side": str(finding.get("side") or "RIGHT").strip(),
    }


def _finding_record(
    finding: dict[str, object],
    *,
    source_head: str,
    status: str,
    surface: str,
    original_anchor: dict[str, object],
    final_anchor: dict[str, object] | None,
    reason: str | None,
) -> dict[str, object]:
    """Build one bounded record without changing host-owned finding content."""
    record: dict[str, object] = {
        "finding_id": finding.get("finding_id"),
        "source_head": source_head,
        "severity": str(finding.get("severity") or "").strip().lower(),
        "body": finding.get("body"),
        "original_anchor": original_anchor,
        "final_anchor": final_anchor,
        "status": status,
        "surface": surface,
        "reason": reason,
    }
    if finding.get("evidence") is not None:
        record["evidence"] = finding["evidence"]
    if finding.get("scope_retraction_paths") is not None:
        record["scope_retraction_paths"] = finding["scope_retraction_paths"]
    return record


def _build_review_finding_records(
    *,
    source_head: str,
    initial_valid: list[dict[str, object]],
    corrections: list[ReviewAnchorCorrection],
    corrected_inline: list[dict[str, object]],
    corrected_audit: list[dict[str, object]],
    not_publishable: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Build the complete journal for all findings in one review response."""
    records: list[dict[str, object]] = []
    for finding in initial_valid:
        blocking = str(finding.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES
        records.append(
            _finding_record(
                finding,
                source_head=source_head,
                status="published" if blocking else "not_publishable",
                surface="inline" if blocking else "not_publishable",
                original_anchor=_finding_anchor(finding),
                final_anchor=_finding_anchor(finding) if blocking else None,
                reason=None if blocking else "audit_surface_unavailable",
            )
        )
    outcomes: dict[str, tuple[str, str, dict[str, object], dict[str, object] | None]] = {}
    for finding in corrected_inline:
        outcomes[str(finding.get("finding_id") or "")] = (
            "corrected",
            "inline",
            finding,
            _finding_anchor(finding),
        )
    for finding in corrected_audit:
        outcomes[str(finding.get("finding_id") or "")] = (
            "corrected",
            "audit",
            finding,
            None,
        )
    for finding in not_publishable:
        outcomes[str(finding.get("finding_id") or "")] = (
            "not_publishable",
            "not_publishable",
            finding,
            None,
        )
    for correction in corrections:
        outcome = outcomes.get(correction.finding_id)
        if outcome is None:
            raise ValueError("review finding correction outcome is incomplete")
        status, surface, finding, final_anchor = outcome
        records.append(
            _finding_record(
                finding,
                source_head=source_head,
                status=status,
                surface=surface,
                original_anchor={
                    "path": correction.path,
                    "line": correction.line,
                    "side": correction.side,
                },
                final_anchor=final_anchor,
                reason=correction.reason,
            )
        )
    return [dict(record) for record in normalize_review_finding_batch_records(records)]


def _review_receipt_lists(
    receipt: PrReviewReconciled,
) -> tuple[list[dict[str, object]], ...] | None:
    """Thaw and validate the lists carried by one review receipt."""
    values = (
        receipt.posted_receipts.thaw(),
        receipt.unresolved_threads.thaw(),
        receipt.remediation_threads.thaw(),
        receipt.anchor_corrections.thaw(),
        receipt.unpublishable_findings.thaw(),
    )
    if not all(isinstance(value, list) for value in values):
        return None
    lists = tuple(cast(list[object], value) for value in values)
    if not all(isinstance(entry, dict) for value in lists for entry in value):
        return None
    return tuple(cast(list[dict[str, object]], value) for value in lists)


def _apply_review_receipt(
    item: WorkItem,
    receipt: PrReviewReconciled,
    *,
    remediation_handoff: Callable[[], StepResult],
) -> StepResult:
    """Apply a successful review publication receipt to one work item."""
    lists = _review_receipt_lists(receipt)
    if lists is None:
        item.payload["review_audit_failure"] = True
        return Continue(next_state=EVAL)
    posted, unresolved, remediation, corrections, unpublishable = lists
    raw_findings = receipt.request.findings.thaw()
    if not isinstance(raw_findings, list) or not all(
        isinstance(value, dict) for value in raw_findings
    ):
        item.payload["review_audit_failure"] = True
        return Continue(next_state=EVAL)
    if corrections or unpublishable:
        item.payload["review_audit_failure"] = True
        return Continue(next_state=EVAL)
    item.payload["review_threads"] = [dict(value) for value in raw_findings]
    item.payload["posted_thread_ids"] = [str(value["id"]) for value in posted if "id" in value]
    item.payload["unresolved_threads"] = [dict(value) for value in unresolved]
    item.payload["remediation_threads"] = [dict(value) for value in remediation]
    item.payload["remediation_thread_snapshots"] = [dict(value) for value in unresolved]
    item.payload["unresolved_threads_before_address"] = len(remediation)
    item.payload.pop("review_anchor_corrections", None)
    item.payload.pop(_ANCHOR_CORRECTION_RETRY, None)
    if item.payload.pop(_COMMENT_VALIDATION_ONLY, None):
        # Thread validation can resolve comments but cannot manufacture
        # the reviewer-owned decision required to authorize GO.
        item.payload.pop("review_audit", None)
        return Continue(next_state=REVIEW_WAIT)
    if remediation:
        return remediation_handoff()
    return Continue(next_state=EVAL)


def empty_diff_outcome(item: WorkItem) -> StageOutcome | None:
    """Reject a thread-free review whose cumulative PR diff is empty."""
    if str(item.payload.get("pr_diff") or "").strip():
        return None
    _clear_round_review_state(item)
    item.payload["empty_diff_reimplementation"] = True
    logger.warning(
        "pr_review:%d: empty cumulative diff; failing back to implementation",
        _issue_number(item),
    )
    return StageOutcome(Disposition.FAIL_BACK, "empty_pr_diff")
