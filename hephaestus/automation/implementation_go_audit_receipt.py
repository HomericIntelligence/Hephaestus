"""Durable recovery receipt for implementation-go audit publication."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from html import escape

from hephaestus.automation.github_api.diff import (
    MAX_REVIEW_FINDING_AGGREGATE_CHARS,
    MAX_REVIEW_FINDING_BODY_CHARS,
    MAX_REVIEW_FINDING_EVIDENCE_CHARS,
    MAX_REVIEW_FINDING_PATH_CHARS,
    MAX_REVIEW_FINDINGS,
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
_FINDING_REASONS = frozenset(
    {
        "reviewed_diff_unavailable",
        "path_not_in_diff",
        "line_not_in_diff",
        "unsupported_side",
    }
)


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


def _normalize_anchor(value: object, *, final: bool) -> dict[str, object] | None:
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
    for record in records:
        if (
            not isinstance(record, dict)
            or not required.issubset(record)
            or set(record) - (required | {"evidence"})
        ):
            raise ValueError("review finding record shape is invalid")
        finding_id = record.get("finding_id")
        source_head = record.get("source_head")
        severity = record.get("severity")
        body = record.get("body")
        evidence = record.get("evidence")
        status = record.get("status")
        surface = record.get("surface")
        reason = record.get("reason")
        if (
            not isinstance(finding_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", finding_id) is None
            or finding_id in ids
            or not _full_sha(source_head)
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
            or status not in {"published", "corrected", "not_publishable"}
            or surface not in {"inline", "audit", "not_publishable"}
            or (reason is not None and reason not in _FINDING_REASONS)
            or (surface == "audit" and severity not in {"minor", "nitpick"})
            or (status == "not_publishable") != (surface == "not_publishable")
        ):
            raise ValueError("review finding record value is invalid")
        original_anchor = _normalize_anchor(record.get("original_anchor"), final=False)
        final_anchor = _normalize_anchor(record.get("final_anchor"), final=True)
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
        normalized.append(value)
        ids.add(finding_id)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > MAX_REVIEW_FINDING_AGGREGATE_CHARS:
        raise ValueError("review finding records exceed their aggregate size limit")
    return tuple(normalized)


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
