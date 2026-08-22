"""Durable recovery receipt for implementation-go audit publication."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from hephaestus.automation.review_audit import (
    MAX_RAW_FEEDBACK_CHARS,
    MAX_REVIEW_SUMMARY_CHARS,
    ReviewAudit,
)

IMPLEMENTATION_GO_AUDIT_PENDING_PREFIX = "<!-- hephaestus-implementation-go-audit-pending:"
_PENDING_MARKER_RE = re.compile(
    r"<!-- hephaestus-implementation-go-audit-pending:pr=(?P<pr>\d+):"
    r"head=(?P<head>[0-9a-f]{40}(?:[0-9a-f]{24})?) -->"
)
_PUBLIC_AUDIT_RE = re.compile(
    r"<!-- hephaestus-implementation-go-audit:pr=(?P<pr>\d+):"
    r"head=(?P<head>[0-9a-f]{40}(?:[0-9a-f]{24})?) -->\n\n"
    r"## Automated PR review\n\nTotal grade: (?P<grade>[A-F])\n\n"
    r"Review summary: (?P<summary>[^\r\n]+)\n\n"
    r"Eligibility is represented only by the live GitHub implementation-state label; "
    r"this audit comment is informational\.\n\nReviewed head: `(?P=head)`\."
)


@dataclass(frozen=True)
class PendingImplementationGoAudit:
    """One validated, exact-head publication recovery receipt."""

    pr_number: int
    head_sha: str
    audit: ReviewAudit


def render_pending_implementation_go_audit(
    pr_number: int, head_sha: str, audit: ReviewAudit
) -> tuple[str, str]:
    """Render an actor-owned machine journal before the GO label write."""
    if (
        pr_number <= 0
        or _PENDING_MARKER_RE.fullmatch(
            f"<!-- hephaestus-implementation-go-audit-pending:pr={pr_number}:head={head_sha} -->"
        )
        is None
    ):
        raise ValueError("pending implementation-go audit identity is invalid")
    if not audit.valid or audit.grade is None or audit.findings:
        raise ValueError("pending implementation-go audit must be a valid clean audit")
    marker = f"<!-- hephaestus-implementation-go-audit-pending:pr={pr_number}:head={head_sha} -->"
    payload = json.dumps(
        {
            "format": 1,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "grade": audit.grade,
            "summary": audit.summary,
            "raw_feedback": audit.raw_feedback,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return marker, f"{marker}\n<!-- {payload} -->"


def parse_pending_implementation_go_audit(body: str) -> PendingImplementationGoAudit | None:
    """Parse one exact machine journal, rejecting malformed owned records."""
    marker, separator, payload_line = body.partition("\n")
    match = _PENDING_MARKER_RE.fullmatch(marker)
    if match is None:
        return None
    if not separator or not payload_line.startswith("<!-- ") or not payload_line.endswith(" -->"):
        raise ValueError("pending implementation-go audit journal is malformed")
    try:
        payload = json.loads(payload_line.removeprefix("<!-- ").removesuffix(" -->"))
    except json.JSONDecodeError as error:
        raise ValueError("pending implementation-go audit journal is malformed") from error
    if not isinstance(payload, dict) or payload.get("format") != 1:
        raise ValueError("pending implementation-go audit journal format is invalid")
    pr_number = int(match.group("pr"))
    head_sha = match.group("head")
    grade = payload.get("grade")
    summary = payload.get("summary")
    raw_feedback = payload.get("raw_feedback")
    if (
        payload.get("pr_number") != pr_number
        or payload.get("head_sha") != head_sha
        or grade not in tuple("ABCDEF")
        or not isinstance(summary, str)
        or not summary
        or len(summary) > MAX_REVIEW_SUMMARY_CHARS
        or not isinstance(raw_feedback, str)
        or len(raw_feedback) > MAX_RAW_FEEDBACK_CHARS
    ):
        raise ValueError("pending implementation-go audit journal payload is invalid")
    return PendingImplementationGoAudit(
        pr_number=pr_number,
        head_sha=head_sha,
        audit=ReviewAudit(
            grade=grade,
            summary=summary,
            findings=(),
            raw_feedback=raw_feedback,
            valid=True,
        ),
    )


def parse_published_implementation_go_audit(body: str) -> PendingImplementationGoAudit | None:
    """Recover the bounded audit from its deterministic public rendering."""
    match = _PUBLIC_AUDIT_RE.fullmatch(body)
    if match is None:
        return None
    return PendingImplementationGoAudit(
        pr_number=int(match.group("pr")),
        head_sha=match.group("head"),
        audit=ReviewAudit(
            grade=match.group("grade"),
            summary=match.group("summary"),
            findings=(),
            raw_feedback="",
            valid=True,
        ),
    )
