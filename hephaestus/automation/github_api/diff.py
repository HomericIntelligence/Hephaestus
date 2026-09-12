"""Pull-request diff position helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html import escape
from typing import Any, Literal, TypedDict

import hephaestus.automation.github_api as _api

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

ReviewAnchorCorrectionReason = Literal[
    "reviewed_diff_unavailable",
    "path_not_in_diff",
    "line_not_in_diff",
    "unsupported_side",
]

MAX_REVIEW_FINDINGS = 64
MAX_COMPACTED_REVIEW_FINDINGS = 256
MAX_REVIEW_FINDING_PATH_CHARS = 4_096
MAX_REVIEW_FINDING_BODY_CHARS = 16_384
MAX_REVIEW_FINDING_EVIDENCE_CHARS = 16_384
# GitHub limits an issue-comment body to 65,536 characters. The public audit
# also contains a base64 form of this collection and a short visible summary.
# This byte limit keeps all supported renderings below that transport limit.
MAX_REVIEW_FINDING_AGGREGATE_BYTES = 30_000
MAX_REVIEW_FINDING_COLLECTION_BYTES = 40_000
MAX_REVIEW_FINDING_BATCH_BYTES = 24_000
MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS = 39_000
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
_TERMINAL_FINDING_OUTCOMES = ("corrected", "not_publishable", "published")
_OUTCOME_CODES = {"c": "corrected", "n": "not_publishable", "p": "published"}


class ReviewFindingCompactedOutcomes(TypedDict):
    """Bounded identities and exact terminal finding counts."""

    counts: dict[str, int]
    identities: list[list[str]]


def _full_sha(value: object) -> bool:
    """Return whether a value is a full SHA-1 or SHA-256 commit ID."""
    return (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is not None
    )


def _normalize_finding_anchor(value: object, *, final: bool) -> dict[str, object] | None:
    """Normalize one original or final finding anchor."""
    if value is None and final:
        return None
    if not isinstance(value, dict) or set(value) != {"path", "line", "side"}:
        raise ValueError("review finding record anchor is invalid")
    path = value.get("path")
    line = value.get("line")
    side = value.get("side")
    if (
        not isinstance(path, str)
        or not path.strip()
        or len(path) > MAX_REVIEW_FINDING_PATH_CHARS
        or not isinstance(side, str)
        or not side.strip()
        or (final and side != "RIGHT")
        or (line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1))
    ):
        raise ValueError("review finding record anchor is invalid")
    if final and line is None:
        raise ValueError("review finding record final anchor is invalid")
    return {"path": path.strip(), "line": line, "side": side.strip()}


def normalize_review_finding_records(
    records: object,
) -> tuple[dict[str, object], ...]:
    """Validate and normalize one bounded finding-journal collection."""
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
        if (
            not isinstance(record, dict)
            or not required.issubset(record)
            or set(record) - (required | optional)
        ):
            raise ValueError("review finding record shape is invalid")
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
            or len(body) > MAX_REVIEW_FINDING_BODY_CHARS
            or (
                evidence is not None
                and (
                    not isinstance(evidence, str)
                    or not evidence.strip()
                    or len(evidence) > MAX_REVIEW_FINDING_EVIDENCE_CHARS
                )
            )
            or (
                scope_retraction_paths is not None
                and (
                    not isinstance(scope_retraction_paths, (list, tuple))
                    or not scope_retraction_paths
                    or any(
                        not isinstance(path, str)
                        or not path.strip()
                        or len(path) > MAX_REVIEW_FINDING_PATH_CHARS
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
        original_anchor = _normalize_finding_anchor(record.get("original_anchor"), final=False)
        final_anchor = _normalize_finding_anchor(record.get("final_anchor"), final=True)
        if surface == "inline" and final_anchor is None:
            raise ValueError("review finding record inline anchor is invalid")
        if surface != "inline" and final_anchor is not None:
            raise ValueError("review finding record non-inline anchor is invalid")
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
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > MAX_REVIEW_FINDING_AGGREGATE_BYTES:
        raise ValueError("review finding records exceed their aggregate size limit")
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
        raise ValueError("review finding records exceed their public rendering limit")
    return tuple(normalized)


def normalize_review_finding_batch_records(
    records: object,
) -> tuple[dict[str, object], ...]:
    """Normalize a new finding batch within its reserved transport space."""
    normalized = normalize_review_finding_records(records)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_REVIEW_FINDING_BATCH_BYTES:
        raise ValueError("review finding batch exceeds its aggregate size limit")
    visible_lines = ["## Retained review findings"]
    visible_lines.extend(
        "- "
        f"`{record['status']}` `{record['severity']}` from "
        f"`{record['source_head']}`: {escape(str(record['body']), quote=False)}"
        for record in normalized
    )
    encoded_payload_chars = ((len(encoded) + 2) // 3) * 4
    public_section_chars = (
        len("\n\n" + "\n".join(visible_lines) + "\n\n")
        + len("<!-- hephaestus-review-finding-records: -->")
        + encoded_payload_chars
    )
    if public_section_chars > MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS:
        raise ValueError("review finding batch exceeds its public rendering limit")
    return normalized


def empty_review_finding_compacted_outcomes() -> ReviewFindingCompactedOutcomes:
    """Return one normalized empty compacted-outcome collection."""
    return {
        "counts": dict.fromkeys(_TERMINAL_FINDING_OUTCOMES, 0),
        "identities": [],
    }


def normalize_review_finding_compacted_outcomes(
    value: object,
    *,
    retained_finding_ids: object = (),
) -> ReviewFindingCompactedOutcomes:
    """Validate bounded terminal outcomes that no longer need full records."""
    if value is None:
        value = empty_review_finding_compacted_outcomes()
    if not isinstance(value, dict) or set(value) != {"counts", "identities"}:
        raise ValueError("review finding compacted outcomes are invalid")
    raw_counts = value.get("counts")
    raw_identities = value.get("identities")
    if (
        not isinstance(raw_counts, dict)
        or set(raw_counts) != set(_TERMINAL_FINDING_OUTCOMES)
        or not isinstance(raw_identities, (list, tuple))
        or len(raw_identities) > MAX_COMPACTED_REVIEW_FINDINGS
    ):
        raise ValueError("review finding compacted outcomes are invalid")
    if not isinstance(retained_finding_ids, (list, tuple, set)) or not all(
        isinstance(finding_id, str) and re.fullmatch(r"[0-9a-f]{64}", finding_id)
        for finding_id in retained_finding_ids
    ):
        raise ValueError("review finding retained identities are invalid")
    retained_ids = set(retained_finding_ids)
    counts: dict[str, int] = {}
    for outcome in _TERMINAL_FINDING_OUTCOMES:
        count = raw_counts.get(outcome)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("review finding compacted outcome counts are invalid")
        counts[outcome] = count
    identities: list[list[str]] = []
    compacted_ids: set[str] = set()
    actual_counts = dict.fromkeys(_TERMINAL_FINDING_OUTCOMES, 0)
    for identity in raw_identities:
        if not isinstance(identity, (list, tuple)) or len(identity) != 4:
            raise ValueError("review finding compacted identity is invalid")
        finding_id, source_head, outcome_code, blocking_code = identity
        if (
            not isinstance(finding_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", finding_id) is None
            or finding_id in compacted_ids
            or finding_id in retained_ids
            or not isinstance(source_head, str)
            or not _full_sha(source_head)
            or not isinstance(outcome_code, str)
            or outcome_code not in _OUTCOME_CODES
            or not isinstance(blocking_code, str)
            or blocking_code not in {"a", "b"}
        ):
            raise ValueError("review finding compacted identity is invalid")
        identities.append([finding_id, source_head, outcome_code, blocking_code])
        compacted_ids.add(finding_id)
        outcome = _OUTCOME_CODES[outcome_code]
        actual_counts[outcome] += 1
    if counts != actual_counts:
        raise ValueError("review finding compacted outcome counts do not match identities")
    return {"counts": counts, "identities": identities}


def normalize_review_finding_collection(
    findings: object,
    *,
    compacted_outcomes: object = None,
) -> tuple[tuple[dict[str, object], ...], ReviewFindingCompactedOutcomes]:
    """Normalize one versioned or legacy cumulative finding history."""
    collection_format: int | None = None
    legacy_list = not isinstance(findings, dict) and compacted_outcomes is None
    if isinstance(findings, dict):
        if compacted_outcomes is not None:
            raise ValueError("review finding collection has duplicate compacted state")
        raw_format = findings.get("format")
        if (
            set(findings) != {"format", "findings", "compacted_outcomes"}
            or type(raw_format) is not int
            or raw_format not in {1, 2}
        ):
            raise ValueError("review finding collection is invalid")
        collection_format = raw_format
        compacted_outcomes = findings.get("compacted_outcomes")
        findings = findings.get("findings")
    records = normalize_review_finding_records(findings)
    if collection_format == 1 and any("publication_head" in record for record in records):
        raise ValueError("review finding collection version is invalid")
    compacted = normalize_review_finding_compacted_outcomes(
        compacted_outcomes,
        retained_finding_ids=[record["finding_id"] for record in records],
    )
    # Legacy lists were accepted against their list-only transport bounds.
    # Envelope expansion must not make a previously valid read fail. All new
    # writes call review_finding_collection_payload, which validates format 1
    # or format 2 after it adds the envelope.
    if legacy_list:
        return records, compacted
    payload: dict[str, object] = {
        "format": 2 if any("publication_head" in record for record in records) else 1,
        "findings": records,
        "compacted_outcomes": compacted,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > MAX_REVIEW_FINDING_COLLECTION_BYTES:
        raise ValueError("review finding collection exceeds its aggregate size limit")
    counts = compacted["counts"]
    visible_lines = ["## Retained review findings"]
    if compacted["identities"]:
        visible_lines.append(
            "Earlier outcomes: "
            f"published {counts['published']}, corrected {counts['corrected']}, "
            f"not publishable {counts['not_publishable']}"
        )
    visible_lines.extend(
        "- "
        f"`{record['status']}` `{record['severity']}` from "
        f"`{record['source_head']}`: {escape(str(record['body']), quote=False)}"
        for record in records
    )
    encoded_payload_chars = ((len(encoded_bytes) + 2) // 3) * 4
    public_section_chars = (
        len("\n\n" + "\n".join(visible_lines) + "\n\n")
        + len("<!-- hephaestus-review-finding-records: -->")
        + encoded_payload_chars
    )
    if public_section_chars > MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS:
        raise ValueError("review finding collection exceeds its public rendering limit")
    return records, compacted


def review_finding_compacted_outcomes_leave_batch_capacity(value: object) -> bool:
    """Return whether compact state leaves space for one maximum review batch."""
    compacted = normalize_review_finding_compacted_outcomes(value)
    if not compacted["identities"]:
        return True
    payload = {
        "format": 1,
        "findings": [],
        "compacted_outcomes": compacted,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope_bytes = len(encoded) - len(b"[]")
    # A publication head replaces prior state for the same finding identity.
    # The removed retained record or compact identity is larger than this field.
    if MAX_REVIEW_FINDING_BATCH_BYTES + envelope_bytes > MAX_REVIEW_FINDING_COLLECTION_BYTES:
        return False
    counts = compacted["counts"]
    outcome_line = (
        "Earlier outcomes: "
        f"published {counts['published']}, corrected {counts['corrected']}, "
        f"not publishable {counts['not_publishable']}"
    )
    encoded_growth_bound = ((envelope_bytes + 2) // 3) * 4
    return (
        MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS + encoded_growth_bound + len(outcome_line) + 1
        <= MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS
    )


def review_finding_collection_payload(
    findings: object,
    compacted_outcomes: object = None,
) -> dict[str, object]:
    """Return the normalized JSON form of one cumulative finding history."""
    records, compacted = normalize_review_finding_collection(
        findings,
        compacted_outcomes=compacted_outcomes,
    )
    payload: dict[str, object] = {
        "format": 2 if any("publication_head" in record for record in records) else 1,
        "findings": [dict(record) for record in records],
        "compacted_outcomes": compacted,
    }
    normalize_review_finding_collection(payload)
    return payload


def compact_terminal_review_finding_collection(
    findings: object,
    compacted_outcomes: object = None,
    *,
    proven_outcomes: object = None,
) -> tuple[tuple[dict[str, object], ...], ReviewFindingCompactedOutcomes]:
    """Compact terminal records until one versioned collection fits."""
    raw_compacted = normalize_review_finding_compacted_outcomes(compacted_outcomes)
    if not isinstance(findings, dict):
        records, _legacy_compacted = normalize_review_finding_collection(findings)
        compacted = normalize_review_finding_compacted_outcomes(
            raw_compacted,
            retained_finding_ids=[record["finding_id"] for record in records],
        )
    else:
        records, compacted = normalize_review_finding_collection(
            findings,
            compacted_outcomes=compacted_outcomes,
        )
    retained = [dict(record) for record in records]
    if proven_outcomes is not None:
        if not isinstance(proven_outcomes, dict) or not all(
            isinstance(finding_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", finding_id) is not None
            and outcome in _TERMINAL_FINDING_OUTCOMES
            for finding_id, outcome in proven_outcomes.items()
        ):
            raise ValueError("review finding proven outcomes are invalid")
        pending_ids = {
            str(record["finding_id"]) for record in retained if record["status"] == "pending"
        }
        if not set(proven_outcomes).issubset(pending_ids):
            raise ValueError("review finding proven outcomes are invalid")
        retained = [
            {
                **record,
                "status": proven_outcomes.get(str(record["finding_id"]), record["status"]),
            }
            for record in retained
        ]
    identities = [list(identity) for identity in compacted["identities"]]
    outcome_codes = {"corrected": "c", "not_publishable": "n", "published": "p"}
    while True:
        counts = dict.fromkeys(_TERMINAL_FINDING_OUTCOMES, 0)
        for identity in identities:
            counts[_OUTCOME_CODES[identity[2]]] += 1
        compacted = normalize_review_finding_compacted_outcomes(
            {"counts": counts, "identities": identities},
            retained_finding_ids=[record["finding_id"] for record in retained],
        )
        try:
            return normalize_review_finding_collection(
                retained,
                compacted_outcomes=compacted,
            )
        except ValueError:
            candidate_index = next(
                (index for index, record in enumerate(retained) if record["status"] != "pending"),
                None,
            )
            if candidate_index is None:
                raise ValueError(
                    "review finding collection has no compactable terminal record"
                ) from None
            candidate = retained.pop(candidate_index)
            identities.append(
                [
                    str(candidate["finding_id"]),
                    str(candidate["source_head"]),
                    outcome_codes[str(candidate["status"])],
                    "b" if candidate["severity"] in {"critical", "major"} else "a",
                ]
            )


def _validate_finding_bounds(finding: dict[str, Any]) -> None:
    """Reject one finding that exceeds the review transport limits."""
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
    """Separate valid review comments from findings that need correction."""

    valid: tuple[dict[str, Any], ...]
    corrections: tuple[ReviewAnchorCorrection, ...]


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


def _validate_comments_to_diff(
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
        Valid comments and typed correction records for invalid comments.

    """
    if len(comments) > MAX_REVIEW_FINDINGS:
        raise ValueError("review finding count exceeds its limit")
    for comment in comments:
        _validate_finding_bounds(comment)
    aggregate_size = len(
        json.dumps(
            comments, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    if aggregate_size > MAX_REVIEW_FINDING_AGGREGATE_BYTES:
        raise ValueError("review findings exceed their aggregate size limit")
    if not diff_text.strip() and fail_open_empty:
        return ReviewCommentValidation(tuple(comments), ())

    valid_positions = _valid_review_positions(diff_text)
    valid: list[dict[str, Any]] = []
    corrections: list[ReviewAnchorCorrection] = []
    for comment in comments:
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
        _api.logger.warning(
            "Preserving review finding on %s:%s (%s) for anchor correction: %s",
            path,
            line_value,
            side,
            corrections[-1].reason,
        )
    return ReviewCommentValidation(tuple(valid), tuple(corrections))


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
