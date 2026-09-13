"""Compatibility exports and diagnostics for GitHub review diff validation."""

from typing import Any

import hephaestus.automation.github_api as _api
from hephaestus.automation.review_anchors import (
    _FINDING_REASONS as _FINDING_REASONS,
    _HUNK_HEADER_RE as _HUNK_HEADER_RE,
    MAX_REVIEW_FINDING_AGGREGATE_BYTES as MAX_REVIEW_FINDING_AGGREGATE_BYTES,
    MAX_REVIEW_FINDING_BODY_CHARS as MAX_REVIEW_FINDING_BODY_CHARS,
    MAX_REVIEW_FINDING_EVIDENCE_CHARS as MAX_REVIEW_FINDING_EVIDENCE_CHARS,
    MAX_REVIEW_FINDING_PATH_CHARS as MAX_REVIEW_FINDING_PATH_CHARS,
    MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS as MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS,
    MAX_REVIEW_FINDINGS as MAX_REVIEW_FINDINGS,
    ReviewAnchorCorrection as ReviewAnchorCorrection,
    ReviewAnchorCorrectionReason as ReviewAnchorCorrectionReason,
    ReviewCommentValidation as ReviewCommentValidation,
    _full_sha as _full_sha,
    _normalize_finding_anchor as _normalize_finding_anchor,
    _review_finding_id as _review_finding_id,
    _valid_review_positions as _valid_review_positions,
    _validate_finding_bounds as _validate_finding_bounds,
    normalize_review_finding_records as normalize_review_finding_records,
    validate_comments_to_diff,
)
from hephaestus.automation.review_finding_history import (
    _OUTCOME_CODES as _OUTCOME_CODES,
    _TERMINAL_FINDING_OUTCOMES as _TERMINAL_FINDING_OUTCOMES,
    MAX_COMPACTED_REVIEW_FINDINGS as MAX_COMPACTED_REVIEW_FINDINGS,
    MAX_REVIEW_FINDING_BATCH_BYTES as MAX_REVIEW_FINDING_BATCH_BYTES,
    MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS as MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS,
    MAX_REVIEW_FINDING_COLLECTION_BYTES as MAX_REVIEW_FINDING_COLLECTION_BYTES,
    ReviewFindingCompactedOutcomes as ReviewFindingCompactedOutcomes,
    compact_terminal_review_finding_collection as compact_terminal_review_finding_collection,
    empty_review_finding_compacted_outcomes as empty_review_finding_compacted_outcomes,
    normalize_review_finding_batch_records as normalize_review_finding_batch_records,
    normalize_review_finding_collection as normalize_review_finding_collection,
    normalize_review_finding_compacted_outcomes as normalize_review_finding_compacted_outcomes,
    review_finding_collection_payload as review_finding_collection_payload,
    review_finding_compacted_outcomes_leave_batch_capacity as _leave_batch_capacity,
)

__all__ = [
    "MAX_COMPACTED_REVIEW_FINDINGS",
    "MAX_REVIEW_FINDINGS",
    "MAX_REVIEW_FINDING_AGGREGATE_BYTES",
    "MAX_REVIEW_FINDING_BATCH_BYTES",
    "MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS",
    "MAX_REVIEW_FINDING_BODY_CHARS",
    "MAX_REVIEW_FINDING_COLLECTION_BYTES",
    "MAX_REVIEW_FINDING_EVIDENCE_CHARS",
    "MAX_REVIEW_FINDING_PATH_CHARS",
    "MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS",
    "_FINDING_REASONS",
    "_HUNK_HEADER_RE",
    "ReviewAnchorCorrection",
    "ReviewAnchorCorrectionReason",
    "ReviewCommentValidation",
    "ReviewFindingCompactedOutcomes",
    "_full_sha",
    "_normalize_finding_anchor",
    "_review_finding_id",
    "_valid_review_positions",
    "_validate_finding_bounds",
    "compact_terminal_review_finding_collection",
    "empty_review_finding_compacted_outcomes",
    "normalize_review_finding_batch_records",
    "normalize_review_finding_collection",
    "normalize_review_finding_compacted_outcomes",
    "normalize_review_finding_records",
    "review_finding_collection_payload",
    "review_finding_compacted_outcomes_leave_batch_capacity",
    "validate_comments_to_diff",
]

review_finding_compacted_outcomes_leave_batch_capacity = _leave_batch_capacity


def _validate_comments_to_diff(
    comments: list[dict[str, Any]],
    diff_text: str,
    *,
    fail_open_empty: bool = False,
    preserve_finding_ids: bool = False,
) -> ReviewCommentValidation:
    """Validate findings and emit the existing transport diagnostics."""
    validation = validate_comments_to_diff(
        comments,
        diff_text,
        fail_open_empty=fail_open_empty,
        preserve_finding_ids=preserve_finding_ids,
    )
    for correction in validation.corrections:
        _api.logger.warning(
            "Preserving review finding on %s:%s (%s) for anchor correction: %s",
            correction.path,
            correction.finding.get("line"),
            correction.side,
            correction.reason,
        )
    return validation


def _filter_comments_to_diff(
    comments: list[dict[str, Any]], diff_text: str
) -> list[dict[str, Any]]:
    """Return valid comments for callers that use the legacy list contract.

    The pipeline uses :func:`_validate_comments_to_diff` so it can preserve
    invalid findings in typed correction records. This wrapper keeps the
    historical list-only result for other callers.

    Fails open: if ``diff_text`` is empty (the diff could not be fetched), the
    comments are returned unchanged — losing a comment because the diff was
    unavailable would be worse than a possible 422.

    Args:
        comments: Inline comment dicts with ``path``/``line``/``side``/``body``.
        diff_text: Unified diff to validate against.

    Returns:
        The subset of ``comments`` that target a line present in the diff.

    """
    return list(_validate_comments_to_diff(comments, diff_text, fail_open_empty=True).valid)
