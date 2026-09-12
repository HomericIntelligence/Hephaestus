"""Durable recovery receipt for implementation-go audit publication."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from html import escape

from hephaestus.automation.github_api.diff import (
    normalize_review_finding_records as normalize_review_finding_records,
)
from hephaestus.automation.review_audit import (
    MAX_RAW_FEEDBACK_CHARS,
    MAX_REVIEW_SUMMARY_CHARS,
    ReviewAudit,
    is_clean_go_review,
)

IMPLEMENTATION_GO_AUDIT_PENDING_PREFIX = "<!-- hephaestus-implementation-go-audit-pending:"
REVIEW_FINDING_JOURNAL_PREFIX = "<!-- hephaestus-review-findings:"
_PENDING_MARKER_RE = re.compile(
    r"<!-- hephaestus-implementation-go-audit-pending:pr=(?P<pr>\d+):"
    r"head=(?P<head>[0-9a-f]{40}(?:[0-9a-f]{24})?) -->"
)
_PUBLIC_AUDIT_RE = re.compile(
    r"<!-- hephaestus-implementation-go-audit:pr=(?P<pr>\d+):"
    r"head=(?P<head>[0-9a-f]{40}(?:[0-9a-f]{24})?) -->\n\n"
    r"## Automated PR review\n\nReviewer verdict: (?P<verdict>GO)\n\n"
    r"Total grade: (?P<grade>[A-F])\n\n"
    r"Review summary: (?P<summary>[^\r\n]+)\n\n"
    r"Eligibility is represented only by the live GitHub implementation-state label; "
    r"this audit comment is informational\.\n\nReviewed head: `(?P=head)`\."
)
_FINDING_JOURNAL_RE = re.compile(
    r"<!-- hephaestus-review-findings:pr=(?P<pr>\d+):"
    r"head=(?P<head>[0-9a-f]{40}(?:[0-9a-f]{24})?) -->"
)
_PUBLIC_FINDING_PAYLOAD_PREFIX = "<!-- hephaestus-review-finding-records:"


@dataclass(frozen=True)
class PendingImplementationGoAudit:
    """One validated, exact-head publication recovery receipt."""

    pr_number: int
    head_sha: str
    audit: ReviewAudit
    finding_records: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class PendingReviewFindingJournal:
    """One actor-owned exact-head collection of review-finding records."""

    pr_number: int
    head_sha: str
    finding_records: tuple[dict[str, object], ...]


def _full_sha(value: object) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is not None
    )


def render_review_finding_journal(
    pr_number: int, head_sha: str, records: object
) -> tuple[str, str]:
    """Render one versioned exact-head finding journal."""
    if pr_number <= 0 or not _full_sha(head_sha):
        raise ValueError("review finding journal identity is invalid")
    normalized = normalize_review_finding_records(records)
    marker = f"<!-- hephaestus-review-findings:pr={pr_number}:head={head_sha} -->"
    payload = json.dumps(
        {"format": 1, "pr_number": pr_number, "head_sha": head_sha, "findings": normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return marker, f"{marker}\n<!-- {payload} -->"


def parse_review_finding_journal(body: str) -> PendingReviewFindingJournal | None:
    """Parse one exact finding journal and reject malformed owned data."""
    marker, separator, payload_line = body.partition("\n")
    match = _FINDING_JOURNAL_RE.fullmatch(marker)
    if match is None:
        return None
    if not separator or not payload_line.startswith("<!-- ") or not payload_line.endswith(" -->"):
        raise ValueError("review finding journal is malformed")
    try:
        payload = json.loads(payload_line.removeprefix("<!-- ").removesuffix(" -->"))
    except json.JSONDecodeError as error:
        raise ValueError("review finding journal is malformed") from error
    pr_number = int(match.group("pr"))
    head_sha = match.group("head")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"format", "pr_number", "head_sha", "findings"}
        or payload.get("format") != 1
        or payload.get("pr_number") != pr_number
        or payload.get("head_sha") != head_sha
    ):
        raise ValueError("review finding journal payload is invalid")
    records = normalize_review_finding_records(payload.get("findings"))
    return PendingReviewFindingJournal(pr_number, head_sha, records)


class LegacyPendingImplementationGoAuditError(ValueError):
    """A pre-verdict receipt that is inert and requires a fresh review."""

    def __init__(self, pr_number: int, head_sha: str) -> None:
        """Record the exact identity of the legacy receipt."""
        super().__init__("pending implementation-go audit journal requires a fresh review")
        self.pr_number = pr_number
        self.head_sha = head_sha


def render_pending_implementation_go_audit(
    pr_number: int,
    head_sha: str,
    audit: ReviewAudit,
    *,
    finding_records: object = (),
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
    if not is_clean_go_review(audit) or audit.grade is None:
        raise ValueError("pending implementation-go audit must have a clean GO verdict")
    marker = f"<!-- hephaestus-implementation-go-audit-pending:pr={pr_number}:head={head_sha} -->"
    normalized_records = normalize_review_finding_records(finding_records)
    payload = json.dumps(
        {
            "format": 3,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "grade": audit.grade,
            "verdict": audit.verdict,
            "summary": audit.summary,
            "raw_feedback": audit.raw_feedback,
            "finding_records": normalized_records,
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
    pr_number = int(match.group("pr"))
    head_sha = match.group("head")
    if isinstance(payload, dict) and payload.get("format") == 1:
        raise LegacyPendingImplementationGoAuditError(pr_number, head_sha)
    if not isinstance(payload, dict) or payload.get("format") not in {2, 3}:
        raise ValueError("pending implementation-go audit journal format is invalid")
    grade = payload.get("grade")
    verdict = payload.get("verdict")
    summary = payload.get("summary")
    raw_feedback = payload.get("raw_feedback")
    if (
        payload.get("pr_number") != pr_number
        or payload.get("head_sha") != head_sha
        or grade not in tuple("ABCDEF")
        or verdict != "GO"
        or not isinstance(summary, str)
        or not summary
        or len(summary) > MAX_REVIEW_SUMMARY_CHARS
        or not isinstance(raw_feedback, str)
        or len(raw_feedback) > MAX_RAW_FEEDBACK_CHARS
    ):
        raise ValueError("pending implementation-go audit journal payload is invalid")
    finding_records = (
        normalize_review_finding_records(payload.get("finding_records"))
        if payload.get("format") == 3
        else ()
    )
    return PendingImplementationGoAudit(
        pr_number=pr_number,
        head_sha=head_sha,
        audit=ReviewAudit(
            grade=grade,
            summary=summary,
            findings=(),
            raw_feedback=raw_feedback,
            valid=True,
            verdict="GO",
        ),
        finding_records=finding_records,
    )


def parse_published_implementation_go_audit(body: str) -> PendingImplementationGoAudit | None:
    """Recover the bounded audit from its deterministic public rendering."""
    base_body = body
    finding_records: tuple[dict[str, object], ...] = ()
    section = "\n\n## Retained review findings\n"
    if section in body:
        base_body, _separator, suffix = body.partition(section)
        payload_line = suffix.rsplit("\n\n", 1)[-1]
        if not (
            payload_line.startswith(_PUBLIC_FINDING_PAYLOAD_PREFIX)
            and payload_line.endswith(" -->")
        ):
            return None
        encoded = payload_line.removeprefix(_PUBLIC_FINDING_PAYLOAD_PREFIX).removesuffix(" -->")
        try:
            decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
            finding_records = normalize_review_finding_records(json.loads(decoded))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return None
    match = _PUBLIC_AUDIT_RE.fullmatch(base_body)
    if match is None:
        return None
    receipt = PendingImplementationGoAudit(
        pr_number=int(match.group("pr")),
        head_sha=match.group("head"),
        audit=ReviewAudit(
            grade=match.group("grade"),
            summary=match.group("summary"),
            findings=(),
            raw_feedback="",
            valid=True,
            verdict="GO",
        ),
        finding_records=finding_records,
    )
    if finding_records:
        lines = ["## Retained review findings"]
        for record in finding_records:
            lines.append(
                "- "
                f"`{record['status']}` `{record['severity']}` from "
                f"`{record['source_head']}`: {escape(str(record['body']), quote=False)}"
            )
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                finding_records,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        expected = (
            f"{base_body}\n\n" + "\n".join(lines) + "\n\n"
            f"{_PUBLIC_FINDING_PAYLOAD_PREFIX}{encoded} -->"
        )
        if expected != body:
            return None
    return receipt
