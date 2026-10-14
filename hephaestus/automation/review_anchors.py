"""Review finding values and diff validation without transport dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any, Literal, cast

from hephaestus.automation.review_warnings import (
    AnchorCorrectionWarningReason,
    ReviewWarning,
    bounded_review_text,
    make_review_warning,
)

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

ReviewAnchorCorrectionReason = Literal[
    "reviewed_diff_unavailable",
    "path_not_in_diff",
    "line_not_in_diff",
    "unsupported_side",
]

MAX_REVIEW_FINDINGS = 64
MAX_REVIEW_FINDING_PATH_CHARS = 4_096
MAX_REVIEW_FINDING_BODY_CHARS = 16_384
MAX_REVIEW_FINDING_EVIDENCE_CHARS = 16_384
# GitHub limits an issue-comment body to 65,536 characters. The public audit
# also contains a base64 form of this collection and a short visible summary.
# This byte limit keeps all supported renderings below that transport limit.
MAX_REVIEW_FINDING_AGGREGATE_BYTES = 30_000
MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS = 60_000
_FINDING_REASONS = frozenset(
    {
        "reviewed_diff_unavailable",
        "path_not_in_diff",
        "line_not_in_diff",
        "unsupported_side",
        "audit_surface_unavailable",
    }
)
# Per-finding content bounds are informational. The host truncates the content
# and warns. Each warning text states what the reviewer must send instead.
_FINDING_FIELD_LIMITS: tuple[tuple[str, int, str], ...] = (
    ("path", MAX_REVIEW_FINDING_PATH_CHARS, "path"),
    ("body", MAX_REVIEW_FINDING_BODY_CHARS, "body"),
    ("evidence", MAX_REVIEW_FINDING_EVIDENCE_CHARS, "evidence"),
)
_TRUNCATION_RECOVERY = "Send a shorter finding field so the host does not truncate the finding."
_ANCHOR_WARNING_CODES: dict[ReviewAnchorCorrectionReason, AnchorCorrectionWarningReason] = {
    "reviewed_diff_unavailable": "anchor_reviewed_diff_unavailable",
    "path_not_in_diff": "anchor_path_not_in_diff",
    "line_not_in_diff": "anchor_line_not_in_diff",
    "unsupported_side": "anchor_unsupported_side",
}
_ANCHOR_WARNING_TEXT: dict[AnchorCorrectionWarningReason, tuple[str, str]] = {
    "anchor_reviewed_diff_unavailable": (
        "The reviewed diff was unavailable. The host moved the finding to the correction list.",
        "Send the reviewed diff with the review so the host can check each anchor.",
    ),
    "anchor_path_not_in_diff": (
        "The review finding path is not in the reviewed diff.",
        "Send a path that the reviewed diff changes.",
    ),
    "anchor_line_not_in_diff": (
        "The review finding line is not in the reviewed diff.",
        "Send a line number that the reviewed diff adds or keeps on the RIGHT side.",
    ),
    "anchor_unsupported_side": (
        "The review finding side is not RIGHT.",
        "Send side as RIGHT. The host cannot publish the other sides inline.",
    ),
}


def _full_sha(value: object) -> bool:
    """Return whether a value is a full SHA-1 or SHA-256 commit ID."""
    return (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is not None
    )


class ReviewFindingSizeError(ValueError):
    """Report that one finding record exceeds a transport size limit.

    This is a size failure, not a corrupt record. A size failure must reach the
    caller so the compaction loop can reduce the collection to a bounded
    identity. A caller must never quarantine a record because it is too large.
    """


def _normalize_finding_anchor(
    value: object, *, final: bool, enforce_size: bool = True
) -> dict[str, object] | None:
    """Normalize one original or final finding anchor."""
    if value is None and final:
        return None
    if not isinstance(value, dict) or set(value) != {"path", "line", "side"}:
        raise ValueError("review finding record anchor is invalid")
    path = value.get("path")
    line = value.get("line")
    side = value.get("side")
    if enforce_size and isinstance(path, str) and len(path) > MAX_REVIEW_FINDING_PATH_CHARS:
        raise ReviewFindingSizeError("review finding record path exceeds its size limit")
    if (
        not isinstance(path, str)
        or not path.strip()
        or not isinstance(side, str)
        or not side.strip()
        or (final and side != "RIGHT")
        or (line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1))
    ):
        raise ValueError("review finding record anchor is invalid")
    if final and line is None:
        raise ValueError("review finding record final anchor is invalid")
    return {"path": path.strip(), "line": line, "side": side.strip()}


def _check_record_values(record: Mapping[str, object], ids: set[str]) -> str:
    """Check every value of one retained finding record.

    The size limits are not checked here. :func:`_check_record_field_sizes`
    owns them, so a size failure and a corrupt value stay separate.

    Args:
        record: One retained finding record with a valid shape.
        ids: Finding identities the host already accepted.

    Returns:
        The validated finding identity of this record.

    Raises:
        ValueError: If any value is invalid, or if the identity is a duplicate.

    """
    finding_id = record.get("finding_id")
    source_head = record.get("source_head")
    publication_head = record.get("publication_head")
    severity = record.get("severity")
    body = record.get("body")
    evidence = record.get("evidence")
    scope_retraction_paths = record.get("scope_retraction_paths")
    status = record.get("status")
    surface = record.get("surface")
    reason = record.get("reason")
    if (
        not isinstance(finding_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", finding_id) is None
        or finding_id in ids
        or not _full_sha(source_head)
        or (publication_head is not None and not _full_sha(publication_head))
        or severity not in {"critical", "major", "minor", "nitpick"}
        or not isinstance(body, str)
        or not body.strip()
        or (evidence is not None and (not isinstance(evidence, str) or not evidence.strip()))
        or (
            scope_retraction_paths is not None
            and (
                not isinstance(scope_retraction_paths, (list, tuple))
                or not scope_retraction_paths
                or any(
                    not isinstance(path, str)
                    or not path.strip()
                    or any(ord(character) < 32 or ord(character) == 127 for character in path)
                    for path in scope_retraction_paths
                )
            )
        )
        or status not in {"pending", "published", "corrected", "not_publishable"}
        or surface not in {"inline", "audit", "not_publishable"}
        or (reason is not None and reason not in _FINDING_REASONS)
        or (status in {"corrected", "not_publishable"} and reason is None)
        or (surface == "audit" and severity not in {"minor", "nitpick"})
        or (status == "not_publishable") != (surface == "not_publishable")
        or (status == "pending" and surface != "inline")
    ):
        raise ValueError("review finding record value is invalid")
    return finding_id


def _check_record_field_sizes(record: Mapping[str, object]) -> None:
    """Raise when one record field exceeds its own size limit.

    A size failure is not corrupt data. The host compacts the record into a
    bounded identity. The host never drops a record because it is too large.

    Raises:
        ReviewFindingSizeError: If one bounded field is too large.

    """
    for field, limit, _label in _FINDING_FIELD_LIMITS:
        field_value = record.get(field)
        if isinstance(field_value, str) and len(field_value) > limit:
            raise ReviewFindingSizeError(f"review finding record {field} exceeds its size limit")


def normalize_review_finding_records(
    records: object,
    *,
    enforce_size: bool = True,
) -> tuple[dict[str, object], ...]:
    """Validate and normalize one bounded finding-journal collection.

    Args:
        records: Candidate retained finding records.
        enforce_size: The compaction intake passes False so it can read a
            collection that is only too large. Every other check stays in
            force. The compaction loop then reads the reduced collection with
            the default value, so the final size decision is always made there.

    Raises:
        ValueError: If a record is invalid, or if the collection exceeds a size
            limit and ``enforce_size`` is True.
        ReviewFindingSizeError: If the collection exceeds a size limit.

    """
    if not isinstance(records, (list, tuple)) or len(records) > MAX_REVIEW_FINDINGS:
        raise ValueError("review finding record count is invalid")
    normalized: list[dict[str, object]] = []
    ids: set[str] = set()
    required = {
        "finding_id",
        "source_head",
        "severity",
        "body",
        "original_anchor",
        "final_anchor",
        "status",
        "surface",
        "reason",
    }
    optional = {"evidence", "publication_head", "scope_retraction_paths"}
    for record in records:
        if isinstance(record, dict) and enforce_size:
            _check_record_field_sizes(record)
        if (
            not isinstance(record, dict)
            or not required.issubset(record)
            or set(record) - (required | optional)
        ):
            raise ValueError("review finding record shape is invalid")
        finding_id = _check_record_values(record, ids)
        source_head = record.get("source_head")
        publication_head = record.get("publication_head")
        severity = record.get("severity")
        body = cast("str", record.get("body"))
        evidence = record.get("evidence")
        scope_retraction_paths = record.get("scope_retraction_paths")
        status = record.get("status")
        surface = record.get("surface")
        reason = record.get("reason")
        original_anchor = _normalize_finding_anchor(
            record.get("original_anchor"), final=False, enforce_size=enforce_size
        )
        final_anchor = _normalize_finding_anchor(
            record.get("final_anchor"), final=True, enforce_size=enforce_size
        )
        if (surface == "inline") != (final_anchor is not None):
            # Only an inline finding keeps a final anchor. A finding on the
            # audit or not-publishable surface has no inline position.
            raise ValueError("review finding record final anchor does not match its surface")
        value: dict[str, object] = {
            "finding_id": finding_id,
            "source_head": source_head,
            "severity": severity,
            "body": body.strip(),
            "original_anchor": original_anchor,
            "final_anchor": final_anchor,
            "status": status,
            "surface": surface,
            "reason": reason,
        }
        if evidence is not None:
            value["evidence"] = evidence.strip()
        if publication_head is not None and publication_head != source_head:
            value["publication_head"] = publication_head
        if scope_retraction_paths is not None:
            value["scope_retraction_paths"] = sorted(
                {str(path).strip() for path in scope_retraction_paths}
            )
        normalized.append(value)
        ids.add(finding_id)
    if not enforce_size:
        # The compaction intake only needs a readable record set. The loop
        # re-reads the reduced collection with the default value.
        return tuple(normalized)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > MAX_REVIEW_FINDING_AGGREGATE_BYTES:
        raise ReviewFindingSizeError("review finding records exceed their aggregate size limit")
    visible_lines = ["## Retained review findings"]
    visible_lines.extend(
        "- "
        f"`{record['status']}` `{record['severity']}` from "
        f"`{record['source_head']}`: {escape(str(record['body']), quote=False)}"
        for record in normalized
    )
    encoded_payload_chars = ((len(encoded_bytes) + 2) // 3) * 4
    public_section_chars = (
        len("\n\n" + "\n".join(visible_lines) + "\n\n")
        + len("<!-- hephaestus-review-finding-records: -->")
        + encoded_payload_chars
    )
    if public_section_chars > MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS:
        raise ReviewFindingSizeError("review finding records exceed their public rendering limit")
    return tuple(normalized)


def _bound_finding_content(finding: dict[str, Any]) -> tuple[ReviewWarning, ...]:
    """Truncate oversized finding fields in place and warn for each field.

    Per-finding content bounds are informational. The host keeps the finding and
    reports one warning for each field that the host truncates.

    Args:
        finding: One candidate finding. The host updates it in place.

    Returns:
        One warning for each truncated field.

    """
    warnings: list[ReviewWarning] = []
    for key, limit, label in _FINDING_FIELD_LIMITS:
        value = finding.get(key)
        if isinstance(value, str) and len(value) > limit:
            finding[key] = bounded_review_text(value, limit)
            warnings.append(
                make_review_warning(
                    "finding_content_truncated",
                    message=f"The review finding {label} was truncated at {limit} characters.",
                    recovery=_TRUNCATION_RECOVERY,
                )
            )
    return tuple(warnings)


def _validate_finding_bounds(finding: dict[str, Any]) -> None:
    """Reject one host-owned finding that exceeds the review transport limits.

    The reviewer intake path truncates and warns. A correction record and a
    durable journal record carry host-owned content that the host bounded
    before, so these checks stay fail-closed.

    Raises:
        ValueError: If the path, body, or evidence exceeds its size limit.

    """
    path = finding.get("path")
    body = finding.get("body")
    evidence = finding.get("evidence")
    if isinstance(path, str) and len(path) > MAX_REVIEW_FINDING_PATH_CHARS:
        raise ValueError("review finding path exceeds its size limit")
    if isinstance(body, str) and len(body) > MAX_REVIEW_FINDING_BODY_CHARS:
        raise ValueError("review finding body exceeds its size limit")
    if isinstance(evidence, str) and len(evidence) > MAX_REVIEW_FINDING_EVIDENCE_CHARS:
        raise ValueError("review finding evidence exceeds its size limit")


def _review_finding_id(finding: dict[str, Any]) -> str:
    """Return the stable identity of one finding before anchor correction."""
    scope_paths = finding.get("scope_retraction_paths")
    canonical = {
        "path": str(finding.get("path") or "").strip(),
        "line": finding.get("line"),
        "side": str(finding.get("side") or "RIGHT").strip().upper(),
        "severity": str(finding.get("severity") or "").strip().lower(),
        "body": str(finding.get("body") or "").strip(),
        "evidence": str(finding.get("evidence") or "").strip(),
        "scope_retraction_paths": (
            [str(path).strip() for path in scope_paths]
            if isinstance(scope_paths, (list, tuple))
            else []
        ),
    }
    encoded = json.dumps(canonical, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewAnchorCorrection:
    """Preserve one finding that needs a different review anchor."""

    finding: dict[str, Any]
    path: str
    line: int | None
    side: str
    reason: ReviewAnchorCorrectionReason
    finding_id: str = ""

    def __post_init__(self) -> None:
        """Validate the bounded correction fields."""
        if not isinstance(self.finding, dict):
            raise ValueError("finding must be a dictionary")
        finding_id = self.finding_id or _review_finding_id(self.finding)
        if re.fullmatch(r"[0-9a-f]{64}", finding_id) is None:
            raise ValueError("finding_id must be a lowercase SHA-256 digest")
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path must be a non-empty string")
        if self.line is not None and (
            isinstance(self.line, bool) or not isinstance(self.line, int)
        ):
            raise ValueError("line must be an integer or None")
        if not isinstance(self.side, str) or not self.side:
            raise ValueError("side must be a non-empty string")
        if not isinstance(self.reason, str) or self.reason not in {
            "reviewed_diff_unavailable",
            "path_not_in_diff",
            "line_not_in_diff",
            "unsupported_side",
        }:
            raise ValueError("reason must be a supported anchor-correction reason")
        finding = dict(self.finding)
        _validate_finding_bounds(finding)
        finding["finding_id"] = finding_id
        object.__setattr__(self, "finding", finding)
        object.__setattr__(self, "finding_id", finding_id)


@dataclass(frozen=True)
class ReviewCommentValidation:
    """Separate valid review comments from findings that need correction.

    Attributes:
        valid: Comments that GitHub accepts for the reviewed diff.
        corrections: Findings that the host moved out of the valid set.
        warnings: One warning for each bounded field and for each correction.

    """

    valid: tuple[dict[str, Any], ...]
    corrections: tuple[ReviewAnchorCorrection, ...]
    warnings: tuple[ReviewWarning, ...] = ()


def _valid_review_positions(diff_text: str) -> dict[str, set[tuple[int, str]]]:
    """Map each changed file to the ``(line, side)`` positions GitHub will accept.

    GitHub's review API rejects (HTTP 422) any inline comment whose ``line``/
    ``side`` does not fall on a line present in the PR diff. A ``RIGHT`` comment
    must target an added (``+``) or context (`` ``) line in the new file; a
    ``LEFT`` comment must target a removed (``-``) or context line in the old
    file. This parses the unified diff once into the set of accepted positions.

    Args:
        diff_text: Unified diff (``gh pr diff <n>`` output).

    Returns:
        ``{path: {(line_number, side), ...}}`` for every changed file.

    """
    positions: dict[str, set[tuple[int, str]]] = {}
    current_path: str | None = None
    old_line = 0
    new_line = 0

    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            # ``+++ b/path`` (or ``+++ /dev/null``); strip the ``b/`` prefix.
            target = raw[4:].strip()
            if target == "/dev/null":
                current_path = None
            else:
                current_path = target[2:] if target.startswith("b/") else target
                positions.setdefault(current_path, set())
            continue
        if raw.startswith("--- "):
            # Old-file header; new-file header (+++) sets the path.
            continue

        header = _HUNK_HEADER_RE.match(raw)
        if header:
            old_line = int(header.group(1))
            new_line = int(header.group(2))
            continue

        if current_path is None or not raw:
            continue

        marker = raw[0]
        if marker == "+":
            positions[current_path].add((new_line, "RIGHT"))
            new_line += 1
        elif marker == "-":
            positions[current_path].add((old_line, "LEFT"))
            old_line += 1
        elif marker == " ":
            # Context line is valid on both sides.
            positions[current_path].add((new_line, "RIGHT"))
            positions[current_path].add((old_line, "LEFT"))
            old_line += 1
            new_line += 1
        # Any other marker (e.g. ``\`` for "No newline") is ignored.

    return positions


def validate_comments_to_diff(
    comments: list[dict[str, Any]],
    diff_text: str,
    *,
    fail_open_empty: bool = False,
    preserve_finding_ids: bool = False,
) -> ReviewCommentValidation:
    """Validate every comment against one immutable unified diff.

    Args:
        comments: Candidate inline review comments.
        diff_text: The diff that the reviewer inspected.
        fail_open_empty: Keep the legacy behavior for callers that cannot
            provide a diff. The pipeline passes a bound diff and does not use
            this compatibility mode.

    Returns:
        Valid comments, typed correction records for invalid comments, and one
        warning for each bounded field and each correction.

    Raises:
        ValueError: If the finding count or the aggregate size exceeds the
            GitHub transport limits.

    """
    if len(comments) > MAX_REVIEW_FINDINGS:
        raise ValueError("review finding count exceeds its limit")
    # Per-finding content bounds are informational. Truncate first, then measure
    # the aggregate, so a truncation can bring a collection under its cap.
    warnings: list[ReviewWarning] = []
    bounded: list[dict[str, Any]] = []
    for comment in comments:
        finding = dict(comment)
        warnings.extend(_bound_finding_content(finding))
        bounded.append(finding)
    aggregate_size = len(
        json.dumps(
            bounded, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    if aggregate_size > MAX_REVIEW_FINDING_AGGREGATE_BYTES:
        raise ValueError("review findings exceed their aggregate size limit")
    if not diff_text.strip() and fail_open_empty:
        return ReviewCommentValidation(tuple(bounded), (), tuple(warnings))

    valid_positions = _valid_review_positions(diff_text)
    valid: list[dict[str, Any]] = []
    corrections: list[ReviewAnchorCorrection] = []
    for comment in bounded:
        finding = dict(comment)
        supplied_id = finding.get("finding_id")
        if preserve_finding_ids:
            if (
                not isinstance(supplied_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", supplied_id) is None
            ):
                raise ValueError("trusted review finding ID is invalid")
            finding_id = supplied_id
        else:
            finding_id = _review_finding_id(finding)
        finding["finding_id"] = finding_id
        path_value = comment.get("path")
        path = path_value.strip() if isinstance(path_value, str) else str(path_value or "")
        line_value = comment.get("line")
        line = (
            line_value if isinstance(line_value, int) and not isinstance(line_value, bool) else None
        )
        side_value = comment.get("side", "RIGHT")
        side = side_value if isinstance(side_value, str) else str(side_value or "")
        if side == "RIGHT" and path in valid_positions and (line, side) in valid_positions[path]:
            valid.append(finding)
            continue
        if not diff_text.strip():
            reason: ReviewAnchorCorrectionReason = "reviewed_diff_unavailable"
        elif side != "RIGHT":
            reason = "unsupported_side"
        elif path not in valid_positions:
            reason = "path_not_in_diff"
        else:
            reason = "line_not_in_diff"
        corrections.append(
            ReviewAnchorCorrection(
                finding=finding,
                path=path,
                line=line,
                side=side,
                reason=reason,
            )
        )
        # Every partition needs an explanation. A correction without a warning
        # is a silent correction, so the host reports each one.
        anchor_code = _ANCHOR_WARNING_CODES[reason]
        anchor_message, anchor_recovery = _ANCHOR_WARNING_TEXT[anchor_code]
        warnings.append(
            make_review_warning(anchor_code, message=anchor_message, recovery=anchor_recovery)
        )
    return ReviewCommentValidation(tuple(valid), tuple(corrections), tuple(warnings))


class ReviewPublicationResult(list[dict[str, Any]]):
    """Return published receipts and preserve findings that need correction."""

    def __init__(
        self,
        receipts: Sequence[dict[str, Any]],
        *,
        validated_findings: Sequence[dict[str, Any]] = (),
        corrections: Sequence[ReviewAnchorCorrection] = (),
        unpublishable: Sequence[ReviewAnchorCorrection] = (),
    ) -> None:
        """Create a result for one immutable review publication attempt."""
        super().__init__(dict(receipt) for receipt in receipts)
        self.validated_findings = tuple(dict(finding) for finding in validated_findings)
        self.corrections = tuple(corrections)
        self.unpublishable = tuple(unpublishable)

    @property
    def published_receipts(self) -> tuple[dict[str, Any], ...]:
        """Return the immutable view of receipts accepted by the host."""
        return tuple(self)

    @property
    def not_publishable(self) -> tuple[ReviewAnchorCorrection, ...]:
        """Return findings that the current review cannot publish inline."""
        return self.unpublishable
