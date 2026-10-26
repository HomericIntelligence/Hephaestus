"""Read the review a reviewer agent returns.

A review is free text with an optional JSON block. The block lists comments and
may carry a verdict, a grade and a severity per comment. The grade and
severity are informational. The host reads it on a best-effort
basis: a field it cannot use is dropped, never a reason to reject the review.
GitHub review threads decide whether the pull request is approved.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any

from hephaestus.automation.pipeline.scope_retraction import (
    SCOPE_RETRACTION_MARKER_PREFIX,
    is_scope_retraction_finding,
    normalize_scope_retraction_paths,
)
from hephaestus.automation.review_json_blocks import json_block_bodies, json_block_spans

MAX_REVIEW_SUMMARY_CHARS = 200
MAX_RAW_FEEDBACK_CHARS = 4000
_NO_SUMMARY = "The reviewer gave no summary."
_REDACTED_CONTROL_TEXT = "[redacted control text]"


@dataclass(frozen=True)
class ReviewAudit:
    """What the host read from one review.

    Attributes:
        grade: The reviewer grade, when the reviewer gave one.
        summary: A bounded summary of the review.
        findings: One dict per comment, each with ``body`` and, when known,
            ``path``, ``line`` and ``severity``.
        raw_feedback: Bounded reviewer text outside the JSON block.
        valid: False only when the reviewer returned nothing the host can read.
        verdict: The reviewer verdict in upper case, when the reviewer gave one.

    """

    grade: str | None
    summary: str
    findings: tuple[dict[str, object], ...]
    raw_feedback: str
    valid: bool
    verdict: str | None = None


def is_clean_go_review(audit: object | None) -> bool:
    """Return whether the review reported nothing to fix."""
    return isinstance(audit, ReviewAudit) and audit.valid and not audit.findings


def parse_review_audit(response: str | Mapping[str, object]) -> ReviewAudit:
    """Read one review response.

    The response may be an agent JSON envelope, plain text, text that ends in a
    fenced JSON block, or a bare JSON object. Text with no JSON is kept as
    feedback and the review has no comments.
    """
    source, payload = _response_payload(response)
    if source is None:
        return ReviewAudit(None, _NO_SUMMARY, (), "", valid=False)
    payload = payload or {}
    grade = payload.get("grade")
    verdict = payload.get("verdict")
    summary = payload.get("summary")
    comments = payload.get("comments")
    return ReviewAudit(
        grade=grade.strip().upper() if isinstance(grade, str) and grade.strip() else None,
        summary=_summary(summary if isinstance(summary, str) else ""),
        findings=tuple(_findings(comments if isinstance(comments, list) else [])),
        raw_feedback=_feedback(source, payload),
        valid=True,
        verdict=verdict.strip().upper() if isinstance(verdict, str) and verdict.strip() else None,
    )


def _findings(comments: list[Any]) -> list[dict[str, object]]:
    """Keep every comment that says something, whatever fields it lacks."""
    findings: list[dict[str, object]] = []
    for comment in comments:
        if isinstance(comment, Mapping):
            body = comment.get("body")
            text = body.strip() if isinstance(body, str) else ""
        else:
            text = str(comment).strip()
        if not text:
            continue
        finding: dict[str, object] = {"body": _redact_control(text)}
        if isinstance(comment, Mapping):
            path = comment.get("path")
            if isinstance(path, str) and path.strip():
                finding["path"] = path.strip()
            line = comment.get("line")
            if isinstance(line, int) and not isinstance(line, bool) and line > 0:
                finding["line"] = line
            severity = comment.get("severity")
            if isinstance(severity, str) and re.fullmatch(r"[A-Za-z]{1,20}", severity.strip()):
                # Informational only: any word is shown, none is checked.
                finding["severity"] = severity.strip().lower()
            if is_scope_retraction_finding(text):
                paths = normalize_scope_retraction_paths(comment.get("scope_retraction_paths"))
                if paths:
                    finding["scope_retraction_paths"] = paths
        findings.append(finding)
    return findings


def _redact_control(text: str) -> str:
    """Remove host-owned control markers a reviewer might copy into a comment."""
    return re.sub(
        r"(?im)^[ \t]*<!--\s*hephaestus-[a-z-]+\s*:.*$",
        _REDACTED_CONTROL_TEXT,
        text,
    ).replace(SCOPE_RETRACTION_MARKER_PREFIX, _REDACTED_CONTROL_TEXT)


def _response_payload(
    response: str | Mapping[str, object],
) -> tuple[str | None, dict[str, object] | None]:
    """Return the response text and its JSON object, when it has one.

    The text is None when the response holds nothing readable.
    """
    if isinstance(response, Mapping):
        if response.get("is_error"):
            return None, None
        result = response.get("result")
        if isinstance(result, str):
            return _response_payload(result)
        return "", dict(response)
    if not isinstance(response, str) or not response.strip():
        return None, None
    bodies = json_block_bodies(response)
    candidates = [bodies[-1]] if bodies else [response.strip()]
    for candidate in candidates:
        try:
            payload = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            return response, dict(payload)
    return response, None


def _summary(summary: str) -> str:
    """Bound and HTML-escape the reviewer-controlled summary."""
    compact = " ".join(summary.split())
    if not compact:
        return _NO_SUMMARY
    escaped = escape(compact, quote=False)
    if len(escaped) <= MAX_REVIEW_SUMMARY_CHARS:
        return escaped
    return f"{escaped[: MAX_REVIEW_SUMMARY_CHARS - 3].rstrip()}..."


def _feedback(source: str, payload: dict[str, object]) -> str:
    """Return the reviewer text outside the JSON block, bounded."""
    stripped = source.strip()
    if not stripped or (stripped.startswith("{") and stripped.endswith("}")):
        return ""
    spans = json_block_spans(source)
    if spans:
        start, end, _ = spans[-1]
        text = "\n\n".join(
            piece for piece in (source[:start].strip(), source[end:].strip()) if piece
        )
    else:
        text = stripped
    if len(text) <= MAX_RAW_FEEDBACK_CHARS:
        return text
    return f"{text[: MAX_RAW_FEEDBACK_CHARS - 15].rstrip()}... [truncated]"


def render_review_audit(audit: ReviewAudit) -> str:
    """Render the informational PR comment for a review."""
    return (
        "## Automated PR review\n\n"
        f"Reviewer verdict: {audit.verdict or 'none'}\n\n"
        f"Total grade: {audit.grade or 'ungraded'}\n\n"
        f"Review summary: {_summary(audit.summary)}\n\n"
        "Approval is the live GitHub implementation-state label. "
        "This comment is informational."
    )
