"""Bounded finding journals and review receipt transitions."""

from collections.abc import Callable, Mapping, Sequence
from typing import cast

from hephaestus.automation.review_anchors import (
    ReviewAnchorCorrection,
    ReviewAnchorCorrectionReason,
)
from hephaestus.automation.review_finding_history import normalize_review_finding_batch_records
from hephaestus.automation.review_warnings import (
    AnchorCorrectionWarningReason,
    ReviewWarning,
    make_review_warning,
)

from ..github_jobs import PrReviewReconciled
from .base import Continue, Disposition, StageOutcome, StepResult, WorkItem
from .pr_review_threads import (
    _COMMENT_VALIDATION_ONLY,
    BLOCKING_SEVERITIES,
    EVAL,
    REVIEW_WAIT,
    _clear_round_review_state,
    _issue_number,
    logger,
)

#: Item payload key for the findings that the review body must show. Each entry
#: carries the finding text, its anchor, the reason, and its warnings. The
#: publication step renders this collection when it builds the visible review.
REPORTED_FINDINGS_PAYLOAD_KEY = "review_reported_findings"

#: Item payload key for the host warnings of one review round. Each entry is a
#: plain warning dict so the payload stays JSON serializable.
REVIEW_WARNINGS_PAYLOAD_KEY = "review_warnings"

#: Reason recorded for an advisory finding. GitHub accepts no inline thread for
#: a finding without a blocking severity, so the host shows it in the body.
ADVISORY_REPORTED_REASON = "advisory"

#: Warning code and recovery text for each anchor reason. Every warning states
#: the action a reviewer must take to publish the finding inline later.
_ANCHOR_WARNING_CODES: dict[ReviewAnchorCorrectionReason, AnchorCorrectionWarningReason] = {
    "reviewed_diff_unavailable": "anchor_reviewed_diff_unavailable",
    "path_not_in_diff": "anchor_path_not_in_diff",
    "line_not_in_diff": "anchor_line_not_in_diff",
    "unsupported_side": "anchor_unsupported_side",
}
_ANCHOR_RECOVERY: dict[ReviewAnchorCorrectionReason, str] = {
    "reviewed_diff_unavailable": (
        "Read the reviewed diff again and send the reviewed lines with the review."
    ),
    "path_not_in_diff": (
        "Rebase the pull request so the reviewed diff contains this file. "
        "Review again to publish this finding inline."
    ),
    "line_not_in_diff": (
        "Rebase the pull request so the reviewed diff contains this line. "
        "Review again to publish this finding inline."
    ),
    "unsupported_side": (
        "Review this finding again and send side as RIGHT. The host publishes no other side inline."
    ),
}


def _warning_payload(warning: ReviewWarning) -> dict[str, str]:
    """Return one warning as a bounded plain dict."""
    return {
        "code": warning.code,
        "message": warning.message,
        "recovery": warning.recovery,
    }


def _unanchored_reported_finding(
    correction: ReviewAnchorCorrection,
) -> dict[str, object]:
    """Build one visible reported record for a finding outside the diff."""
    code = _ANCHOR_WARNING_CODES[correction.reason]
    finding = dict(correction.finding)
    return {
        "finding": finding,
        "finding_id": correction.finding_id,
        "severity": str(finding.get("severity") or "").strip().lower(),
        "path": correction.path,
        "line": correction.line,
        "side": correction.side,
        "reason": correction.reason,
        "warnings": [
            _warning_payload(
                make_review_warning(
                    code,
                    message=(
                        f"The host cannot publish finding {correction.finding_id} "
                        "inline because its anchor is not in the reviewed diff."
                    ),
                    recovery=_ANCHOR_RECOVERY[correction.reason],
                )
            )
        ],
    }


def _advisory_reported_finding(finding: Mapping[str, object]) -> dict[str, object]:
    """Build one visible reported record for an advisory finding."""
    value = dict(finding)
    return {
        "finding": value,
        "finding_id": str(value.get("finding_id") or ""),
        "severity": str(value.get("severity") or "").strip().lower(),
        "path": str(value.get("path") or "").strip(),
        "line": value.get("line"),
        "side": str(value.get("side") or "").strip(),
        "reason": ADVISORY_REPORTED_REASON,
        "warnings": [],
    }


def build_reported_findings(
    advisory: Sequence[dict[str, object]],
    corrections: Sequence[ReviewAnchorCorrection],
) -> list[dict[str, object]]:
    """Build every finding that the review body must show.

    Args:
        advisory: Findings that the reviewed diff accepts but that carry no
            blocking severity.
        corrections: Findings whose anchor is not in the reviewed diff.

    Returns:
        One reported record for each finding.

    """
    return [
        *(_advisory_reported_finding(finding) for finding in advisory),
        *(_unanchored_reported_finding(correction) for correction in corrections),
    ]


def _finding_anchor(finding: Mapping[str, object]) -> dict[str, object]:
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
    unanchored: list[ReviewAnchorCorrection],
) -> list[dict[str, object]]:
    """Build the complete journal for all findings in one review response.

    A blocking finding with an anchor in the reviewed diff publishes inline.
    An advisory finding publishes on the review body surface. A finding whose
    anchor is not in the reviewed diff records its anchor problem, so the
    review body can show it with a warning.

    Args:
        source_head: The reviewed head that owns every finding.
        initial_valid: Every finding whose anchor the reviewed diff accepts.
        unanchored: Findings that the reviewed diff does not accept.

    Returns:
        One normalized journal record for every finding.

    """
    records: list[dict[str, object]] = []
    for finding in initial_valid:
        blocking = str(finding.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES
        records.append(
            _finding_record(
                finding,
                source_head=source_head,
                status="published",
                surface="inline" if blocking else "audit",
                original_anchor=_finding_anchor(finding),
                final_anchor=_finding_anchor(finding) if blocking else None,
                reason=None,
            )
        )
    for correction in unanchored:
        records.append(
            _finding_record(
                dict(correction.finding),
                source_head=source_head,
                status="not_publishable",
                surface="not_publishable",
                original_anchor={
                    "path": correction.path,
                    "line": correction.line,
                    "side": correction.side,
                },
                final_anchor=None,
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
        receipt.corrections.thaw(),
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
        # The host runs no reviewer correction turn now. A receipt that still
        # carries a typed correction or an unpublishable finding means the
        # publication path did not agree with this stage.
        item.payload["review_audit_failure"] = True
        return Continue(next_state=EVAL)
    item.payload["review_threads"] = [dict(value) for value in raw_findings]
    item.payload["posted_thread_ids"] = [str(value["id"]) for value in posted if "id" in value]
    item.payload["unresolved_threads"] = [dict(value) for value in unresolved]
    item.payload["remediation_threads"] = [dict(value) for value in remediation]
    item.payload["remediation_thread_snapshots"] = [dict(value) for value in unresolved]
    item.payload["unresolved_threads_before_address"] = len(remediation)
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
