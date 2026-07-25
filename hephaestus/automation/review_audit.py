"""Structural PR-review audit parsing.

The implementation-review queue consumes this value object instead of parsing
decision-shaped prose. GitHub implementation-state labels remain the only
durable transition authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape

_JSON_BLOCK_RE = re.compile(
    r"^[ \t]*```json[ \t]*\r?\n(.*?)\r?\n^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
_VALID_GRADES = frozenset("ABCDEF")
_VALID_SEVERITIES = frozenset({"critical", "major", "minor", "nitpick"})
_RESERVED_AUTHORITY_CLAIM_RE = re.compile(
    r"\b(?:verdict|decision|approval|rejection)\s*:\s*[^\r\n]*"
    r"|\bimplementation[\s_-]+(?:approval|rejection)\b[^\r\n]*"
    r"|\bimplementation\s+(?:is\s+)?(?:approved|rejected|go|no[-\s]?go|nogo)\b[^\r\n]*"
    r"|\bstate\s*:\s*implementation-(?:no-)?go(?:\s+applied)?\b",
    re.IGNORECASE,
)
_SEVERITY_MARKER_RE = re.compile(r"(?im)^[ \t]*<!--\s*hephaestus-severity\s*:")
MAX_REVIEW_SUMMARY_CHARS = 200
MAX_RAW_FEEDBACK_CHARS = 4000
_INVALID_SUMMARY = "No structured reviewer summary was provided."


@dataclass(frozen=True)
class ReviewAudit:
    """Sanitized structural output from one implementation review."""

    grade: str | None
    summary: str
    findings: tuple[dict[str, object], ...]
    raw_feedback: str
    valid: bool


def parse_review_audit(response: str | Mapping[str, object]) -> ReviewAudit:
    """Parse one strict structural review response.

    The response may be Claude's JSON envelope, Codex stdout, a fenced JSON
    block following reviewer prose, or a raw JSON object. Text outside the
    object is retained only as bounded supplemental feedback; it never affects
    validity or a label transition.
    """
    source, payload = _response_payload(response)
    raw_feedback = _bounded_feedback(source, payload)
    if payload is None:
        return _invalid_audit(raw_feedback)

    grade = payload.get("grade")
    summary = payload.get("summary")
    comments = payload.get("comments")
    if not isinstance(grade, str) or grade.strip().upper() not in _VALID_GRADES:
        return _invalid_audit(raw_feedback)
    if not isinstance(summary, str) or not isinstance(comments, list):
        return _invalid_audit(raw_feedback)

    findings: list[dict[str, object]] = []
    for comment in comments:
        normalized = _normalize_finding(comment)
        if normalized is None:
            return _invalid_audit(raw_feedback)
        findings.append(normalized)

    return ReviewAudit(
        grade=grade.strip().upper(),
        summary=_sanitize_summary(summary),
        findings=tuple(findings),
        raw_feedback=raw_feedback,
        valid=True,
    )


def _response_payload(response: str | Mapping[str, object]) -> tuple[str, dict[str, object] | None]:
    """Extract exactly one structural JSON object from an agent response."""
    if isinstance(response, Mapping):
        if response.get("is_error"):
            return str(response.get("result") or ""), None
        result = response.get("result")
        if isinstance(result, str):
            return _response_payload(result)
        return "", dict(response)
    if not isinstance(response, str):
        return "", None

    matches = _JSON_BLOCK_RE.findall(response)
    if len(matches) > 1:
        return response, None
    if matches:
        candidate = matches[0].strip()
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return response, None
        return response, dict(payload) if isinstance(payload, dict) else None

    stripped = response.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return response, None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return response, None
    return response, dict(payload) if isinstance(payload, dict) else None


def _normalize_finding(comment: object) -> dict[str, object] | None:
    """Validate and normalize one finding that must become a GitHub thread."""
    if not isinstance(comment, Mapping):
        return None
    path = comment.get("path")
    line = comment.get("line")
    side = comment.get("side")
    severity = comment.get("severity")
    body = comment.get("body")
    if (
        not isinstance(path, str)
        or not path.strip()
        or isinstance(line, bool)
        or not isinstance(line, int)
        or line < 1
        or side != "RIGHT"
        or not isinstance(severity, str)
        or severity.lower() not in _VALID_SEVERITIES
        or not isinstance(body, str)
        or not body.strip()
        or has_reserved_finding_control(body)
    ):
        return None
    return {
        "path": path.strip(),
        "line": line,
        "side": "RIGHT",
        "severity": severity.lower(),
        "body": body.strip(),
    }


def has_reserved_finding_control(body: str) -> bool:
    """Return whether a finding body contains a pipeline-owned control."""
    return bool(_SEVERITY_MARKER_RE.search(body) or _RESERVED_AUTHORITY_CLAIM_RE.search(body))


def _sanitize_summary(summary: str) -> str:
    """Bound and HTML-escape the reviewer-controlled summary."""
    compact = " ".join(_RESERVED_AUTHORITY_CLAIM_RE.sub("", summary).split())
    if not compact:
        return _INVALID_SUMMARY
    escaped = escape(compact, quote=False)
    if len(escaped) <= MAX_REVIEW_SUMMARY_CHARS:
        return escaped
    return f"{escaped[: MAX_REVIEW_SUMMARY_CHARS - 3].rstrip()}..."


def _bounded_feedback(source: str, payload: dict[str, object] | None) -> str:
    """Return only bounded supplemental prose, excluding the JSON artifact."""
    if payload is not None and (
        not source or (source.strip().startswith("{") and source.strip().endswith("}"))
    ):
        return ""
    if not source:
        return ""
    match = _JSON_BLOCK_RE.search(source)
    feedback = source[: match.start()] if match else source
    feedback = feedback.strip()
    if len(feedback) <= MAX_RAW_FEEDBACK_CHARS:
        return feedback
    return f"{feedback[: MAX_RAW_FEEDBACK_CHARS - 15].rstrip()}... [truncated]"


def _invalid_audit(raw_feedback: str) -> ReviewAudit:
    """Build a fail-closed audit result."""
    return ReviewAudit(
        grade=None,
        summary=_INVALID_SUMMARY,
        findings=(),
        raw_feedback=raw_feedback,
        valid=False,
    )


def render_review_audit(audit: ReviewAudit) -> str:
    """Render the bounded informational PR comment for a review audit.

    The implementation-state label is intentionally not represented as a
    decision field in this comment. Callers must obtain authorization from a
    fresh, confirmed GitHub label transition instead.
    """
    summary = _sanitize_summary(audit.summary)
    return (
        "## Automated PR review\n\n"
        f"Total grade: {audit.grade or 'ungraded'}\n\n"
        f"Review summary: {summary}\n\n"
        "Eligibility is represented only by the live GitHub implementation-state label; "
        "this audit comment is informational."
    )
