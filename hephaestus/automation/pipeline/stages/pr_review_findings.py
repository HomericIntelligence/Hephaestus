"""Bounded finding journals and review receipt transitions."""

from collections.abc import Callable, Mapping, Sequence
from typing import cast

from hephaestus.automation.review_anchors import (
    MAX_REVIEW_FINDINGS,
    ReviewAnchorCorrection,
    ReviewAnchorCorrectionReason,
)
from hephaestus.automation.review_audit import MAX_REVIEW_WARNINGS
from hephaestus.automation.review_finding_history import normalize_review_finding_batch_records
from hephaestus.automation.review_warnings import (
    AnchorCorrectionWarningReason,
    ReviewWarning,
    make_review_warning,
    record_review_warning,
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
#: plain warning dict so the payload stays JSON serializable. The list never
#: holds more than :data:`MAX_CARRIED_REVIEW_WARNINGS` entries.
REVIEW_WARNINGS_PAYLOAD_KEY = "review_warnings"

#: Item payload key for the number of warnings that the bounded carried list
#: could not hold. The host clears this key when it starts a new review round.
REVIEW_WARNINGS_DROPPED_PAYLOAD_KEY = "review_warnings_dropped"

#: Reason recorded for an advisory finding. GitHub accepts no inline thread for
#: a finding without a blocking severity, so the host shows it in the body.
ADVISORY_REPORTED_REASON = "advisory"

#: Item payload key for the findings that the publication receipt demoted. Each
#: entry carries the finding text, its anchor, its anchor reason, and one
#: warning. The publication step renders this collection in the visible review
#: body, and the finding journal records each demotion as ``not_publishable``.
#: The host clears this key when it starts a new review round.
DEMOTED_FINDINGS_PAYLOAD_KEY = "review_demoted_findings"

#: Message and recovery text for the demotion reporting budget. The warning
#: states the problem and the action an operator must take.
_DEMOTION_BUDGET_WARNING_TEXT = (
    "The host did not show every finding that it demoted from the inline surface.",
    "Read the review body and the pipeline log for the complete demoted set.",
)

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

#: Anchor reasons that describe a demotion. A receipt record with any other
#: reason, or without a reason, is an integrity failure. It must not reach the
#: informational demotion path.
_DEMOTION_REASONS = frozenset(_ANCHOR_WARNING_CODES)

#: Maximum number of warning dicts that the item payload carries for one review
#: round. The host truncates the list and states the truncation in one warning.
MAX_CARRIED_REVIEW_WARNINGS = MAX_REVIEW_WARNINGS


def _warning_payload(warning: ReviewWarning) -> dict[str, str]:
    """Return one warning as a bounded plain dict."""
    return {
        "code": warning.code,
        "message": warning.message,
        "recovery": warning.recovery,
    }


def _carried_warning_payloads(item: WorkItem) -> list[dict[str, object]]:
    """Return the bounded warning dicts that the item payload already carries."""
    carried = item.payload.get(REVIEW_WARNINGS_PAYLOAD_KEY)
    if not isinstance(carried, list):
        return []
    return [dict(value) for value in carried if isinstance(value, dict)]


def carry_review_warnings(
    item: WorkItem,
    warnings: Sequence[ReviewWarning],
    *,
    note_truncation: bool = False,
) -> None:
    """Append bounded warning text to the item payload of one review round.

    The carried list never exceeds :data:`MAX_CARRIED_REVIEW_WARNINGS`. When
    the host drops warnings it states the truncation in one warning, so no
    caller loses the fact that the list is incomplete.

    Args:
        item: The review work item that carries the warnings.
        warnings: Bounded warnings that one step produced.
        note_truncation: The caller dropped warnings of its own. The host then
            states that fact in one warning even when every warning fits.

    """
    if not warnings and not note_truncation:
        return
    carried = _carried_warning_payloads(item)
    room = max(MAX_CARRIED_REVIEW_WARNINGS - len(carried), 0)
    if room < 1:
        _note_dropped_warnings(item, len(warnings))
        return
    kept = list(warnings[:room])
    truncated = note_truncation or len(kept) < len(warnings)
    if truncated:
        # Reserve one slot so the notice always fits inside the bound.
        kept = kept[: room - 1]
    payloads = [_warning_payload(warning) for warning in kept]
    if truncated:
        dropped = len(warnings) - len(kept)
        payloads.append(_warning_payload(_demotion_budget_warning()))
        _note_dropped_warnings(item, dropped)
    item.payload[REVIEW_WARNINGS_PAYLOAD_KEY] = [*carried, *payloads]


def _note_dropped_warnings(item: WorkItem, dropped: int) -> None:
    """Record how many warnings the bounded carried list could not hold.

    The host logs the first drop only. The counter keeps the total so an
    operator can see that the carried list is incomplete.
    """
    if dropped < 1:
        return
    previous = item.payload.get(REVIEW_WARNINGS_DROPPED_PAYLOAD_KEY)
    first = not isinstance(previous, int)
    total = (previous if isinstance(previous, int) else 0) + dropped
    item.payload[REVIEW_WARNINGS_DROPPED_PAYLOAD_KEY] = total
    if first:
        logger.warning(
            "pr_review:%d: the review warning list is full; the host dropped %d warning(s)",
            _issue_number(item),
            total,
        )


def _demotion_budget_warning() -> ReviewWarning:
    """Return the warning that states the host dropped reported findings."""
    message, recovery = _DEMOTION_BUDGET_WARNING_TEXT
    return make_review_warning(
        "anchor_demotion_budget_exhausted", message=message, recovery=recovery
    )


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


def _receipt_demotion(record: Mapping[str, object]) -> ReviewAnchorCorrection | None:
    """Return one typed demotion, or None when the record is not a demotion.

    The publication receipt states every demoted finding with a typed anchor
    reason. The stage reads that reason instead of guessing. A record without a
    typed anchor reason is not review feedback. The host must fail closed on it.

    Args:
        record: One frozen record from ``corrections`` or
            ``unpublishable_findings``.

    Returns:
        The typed demotion, or None when the record carries no supported anchor
        reason or an invalid anchor field.

    """
    finding = record.get("finding")
    path = record.get("path")
    line = record.get("line")
    side = record.get("side")
    reason = record.get("reason")
    finding_id = record.get("finding_id")
    if (
        not isinstance(finding, dict)
        or not isinstance(path, str)
        or not path.strip()
        or (line is not None and (isinstance(line, bool) or not isinstance(line, int)))
        or not isinstance(side, str)
        or not side.strip()
        or not isinstance(reason, str)
        or reason not in _DEMOTION_REASONS
        or not isinstance(finding_id, str)
        or not finding_id
    ):
        return None
    try:
        return ReviewAnchorCorrection(
            finding=dict(finding),
            path=path,
            line=line,
            side=side,
            reason=reason,
            finding_id=finding_id,
        )
    except ValueError:
        return None


def carry_demoted_findings(
    item: WorkItem,
    records: Sequence[Mapping[str, object]],
) -> bool:
    """Carry one receipt's demoted findings as visible, bounded review data.

    A demotion is a normal, expected publication outcome. The host keeps each
    demoted finding under :data:`DEMOTED_FINDINGS_PAYLOAD_KEY` so it stays
    visible and auditable, and records one warning with a real recovery
    instruction for each. The item keeps its normal post-publication path.

    Args:
        item: The review work item that holds the publication receipt.
        records: The frozen correction and unpublishable records of one receipt.

    Returns:
        False when any record is not a typed anchor demotion. The caller then
        keeps failing closed, because the receipt and this stage disagree. True
        when every record is a typed demotion.

    """
    demotions: list[ReviewAnchorCorrection] = []
    for record in records:
        demotion = _receipt_demotion(record)
        if demotion is None:
            return False
        demotions.append(demotion)
    if not demotions:
        return True

    carried = item.payload.get(DEMOTED_FINDINGS_PAYLOAD_KEY)
    existing = (
        [dict(value) for value in carried if isinstance(value, dict)]
        if isinstance(carried, list)
        else []
    )
    reported = build_reported_findings((), demotions)
    room = max(MAX_REVIEW_FINDINGS - len(existing), 0)
    kept = reported[:room]
    item.payload[DEMOTED_FINDINGS_PAYLOAD_KEY] = [*existing, *kept]
    warnings: list[ReviewWarning] = []
    for demotion in demotions:
        record_review_warning(
            warnings,
            _ANCHOR_WARNING_CODES[demotion.reason],
            message=(
                f"The host cannot publish finding {demotion.finding_id} as an "
                "inline comment because its anchor is not in the reviewed diff."
            ),
            recovery=_ANCHOR_RECOVERY[demotion.reason],
        )
    carry_review_warnings(item, warnings, note_truncation=len(kept) < len(reported))
    return True


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
    # A demotion is a normal, expected publication outcome. The receipt states
    # each demoted finding with a typed anchor reason. The host keeps the
    # demotion visible and warns about it. It does not fail the review round.
    # A record that carries no typed anchor reason is not a demotion. It means
    # the publication path and this stage disagree, so the host fails closed.
    if not carry_demoted_findings(item, [*corrections, *unpublishable]):
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
