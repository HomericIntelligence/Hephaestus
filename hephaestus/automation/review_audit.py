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
from typing import Literal, cast

from hephaestus.automation.pipeline.scope_retraction import (
    SCOPE_RETRACTION_MARKER_PREFIX,
    is_scope_retraction_finding,
    normalize_scope_retraction_paths,
)
from hephaestus.automation.review_finding_history import (
    normalize_review_finding_collection,
    review_finding_collection_payload,
)
from hephaestus.automation.review_warnings import (
    MalformedReviewFindingReason,
    ReviewWarning,
    bounded_review_text,
    make_review_warning,
)
from hephaestus.automation.scope_expansion_domain import (
    ScopeExpansion,
    normalize_scope_expansions,
)

_JSON_BLOCK_RE = re.compile(
    r"^[ \t]*```json[ \t]*\r?\n(.*?)\r?\n^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
_VALID_GRADES = frozenset("ABCDEF")
_VALID_VERDICTS = frozenset({"GO", "NOGO", "BLOCKED"})
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
MAX_REVIEW_WARNINGS = 64
MAX_REVIEW_REPORTED_FINDINGS = 32
MAX_REVIEW_REPORTED_FINDING_CHARS = 2_000
_INVALID_SUMMARY = "No structured reviewer summary was provided."
_REDACTED_CONTROL_TEXT = "[redacted control text]"
_FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_FINDING_FIELDS = "Send each finding with a path, a line, side RIGHT, a severity, and a body."
# One message and one recovery instruction per malformed-finding reason.
_FINDING_WARNING_TEXT: dict[str, tuple[str, str]] = {
    "finding_invalid": (
        "The host did not accept one review finding.",
        f"Send the finding again. {_FINDING_FIELDS}",
    ),
    "finding_not_an_object": (
        "The review finding is not a JSON object.",
        f"Send each finding as a JSON object. {_FINDING_FIELDS}",
    ),
    "finding_path_invalid": (
        "The review finding path is missing or is not a non-empty string.",
        "Send a path as a non-empty string that stays inside the path size limit.",
    ),
    "finding_line_invalid": (
        "The review finding line is missing or is not a positive integer.",
        "Send a line number as an integer that is greater than zero.",
    ),
    "finding_side_unsupported": (
        "The review finding side is not RIGHT.",
        "Send side as RIGHT. The host publishes no other side inline.",
    ),
    "finding_severity_invalid": (
        "The review finding severity is not supported.",
        "Send severity as critical, major, minor, or nitpick.",
    ),
    "finding_body_invalid": (
        "The review finding body is missing or is empty.",
        "Send a non-empty body as a string.",
    ),
    "finding_evidence_invalid": (
        "The review finding evidence is missing or is not a non-empty string.",
        "Send evidence as a non-empty string, or omit the evidence field.",
    ),
    "finding_id_invalid": (
        "The review finding ID is not a lowercase SHA-256 digest.",
        "Send finding_id as a 64 character lowercase hexadecimal digest, or omit the field.",
    ),
    "finding_reserved_control_text": (
        "The review finding body contains reserved control text.",
        "Remove the reserved control text from the body, then send the finding again.",
    ),
    "finding_scope_retraction_paths_invalid": (
        "The scope retraction finding does not carry valid paths.",
        "Send a scope retraction finding with a non-empty list of scope_retraction_paths strings.",
    ),
    "finding_scope_retraction_conflict": (
        "The review finding sends scope_retraction_paths without a scope retraction marker.",
        "Add the scope retraction marker to the body, or remove the scope_retraction_paths field.",
    ),
    "finding_content_truncated": (
        "The host truncated one review finding field.",
        "Send a shorter finding field so the host does not truncate the finding.",
    ),
    "malformed_finding_budget_exhausted": (
        "The review sends more malformed findings than the reporting limit.",
        "Send fewer findings so that the host can report each malformed finding.",
    ),
}
_DEFAULT_WARNING_TEXT = _FINDING_WARNING_TEXT["finding_invalid"]
_FindingNormalization = tuple[dict[str, object] | None, MalformedReviewFindingReason | None]

ReviewVerdict = Literal["GO", "NOGO", "BLOCKED"]


@dataclass(frozen=True)
class ReviewAudit:
    """Sanitized structural output from one implementation review.

    Attributes:
        grade: Reviewer grade from A to F, or None for an invalid audit.
        summary: Bounded reviewer summary.
        findings: Findings that the host can publish.
        raw_feedback: Bounded reviewer prose outside the JSON object.
        valid: Whether the host accepted the review structure.
        verdict: Explicit reviewer verdict, or None for an invalid audit.
        scope_expansions: Scope expansions that the reviewer reported.
        warnings: Bounded warnings about malformed findings.
        reported_findings: Bounded text for each malformed finding.

    """

    grade: str | None
    summary: str
    findings: tuple[dict[str, object], ...]
    raw_feedback: str
    valid: bool
    verdict: ReviewVerdict | None = None
    scope_expansions: tuple[ScopeExpansion, ...] = ()
    warnings: tuple[ReviewWarning, ...] = ()
    reported_findings: tuple[dict[str, object], ...] = ()


def is_clean_go_review(audit: object | None) -> bool:
    """Return whether one parsed audit can authorize implementation GO.

    A reported finding is not a warning. It is a defect that the reviewer found
    and that the host could not publish as an inline anchor. The implementation
    sign-off gate is the one blocking gate in this design, so it must not
    authorize a GO while such a defect is unresolved. Any reported finding
    therefore prevents a clean sign-off here.

    This boundary does not skip the finding. The publication path still renders
    the finding in the visible review body with a warning marker, the reviewer
    can re-anchor it, and the work item routes through the normal non-GO review
    path. The gate never raises and never fails open.
    """
    return (
        isinstance(audit, ReviewAudit)
        and audit.valid
        and audit.verdict == "GO"
        and not audit.findings
        and not audit.reported_findings
        and not audit.scope_expansions
    )


def parse_review_audit(response: str | Mapping[str, object]) -> ReviewAudit:
    """Parse one structural review response.

    The response may be Claude's JSON envelope, Codex stdout, a fenced JSON
    block following reviewer prose, or a raw JSON object. Text outside the
    object is retained only as bounded supplemental feedback; it never affects
    validity or a label transition.

    The grade, the summary, the comment list, and the explicit verdict stay
    strict. Validation of one finding is informational. The host warns about a
    malformed finding, keeps the other findings, and carries the malformed
    content as bounded text in ``reported_findings``.
    """
    source, payload = _response_payload(response)
    raw_feedback = _bounded_feedback(source, payload)
    if payload is None:
        return _invalid_audit(raw_feedback)

    grade = payload.get("grade")
    raw_verdict = payload.get("verdict")
    summary = payload.get("summary")
    comments = payload.get("comments")
    scope_expansions = payload.get("scope_expansions")
    if not isinstance(grade, str) or grade.strip().upper() not in _VALID_GRADES:
        return _invalid_audit(raw_feedback)
    if not isinstance(summary, str) or not isinstance(comments, list):
        return _invalid_audit(raw_feedback)
    normalized_scope_expansions = normalize_scope_expansions(scope_expansions)
    if normalized_scope_expansions is None:
        return _invalid_audit(raw_feedback)

    # The implementation-GO boundary requires an explicit reviewer verdict.
    # Missing, malformed, or unsupported values invalidate the whole audit so
    # the gate can fail closed instead of inferring from the grade or summary.
    if not isinstance(raw_verdict, str):
        return _invalid_audit(raw_feedback)
    verdict = cast(ReviewVerdict, raw_verdict.strip().upper())
    if verdict not in _VALID_VERDICTS:
        return _invalid_audit(raw_feedback)

    findings: list[dict[str, object]] = []
    warnings: list[ReviewWarning] = []
    reported_findings: list[dict[str, object]] = []
    # Finding validation is informational. The host keeps every valid finding,
    # warns about each malformed finding, and carries the malformed content as
    # bounded text. One malformed finding never invalidates this audit.
    for comment in comments:
        normalized, reason = _normalize_finding(comment)
        if normalized is not None:
            findings.append(normalized)
            continue
        if len(reported_findings) >= MAX_REVIEW_REPORTED_FINDINGS:
            _record_budget_warning(warnings)
            continue
        code = reason or "finding_invalid"
        message, recovery = _FINDING_WARNING_TEXT.get(code, _DEFAULT_WARNING_TEXT)
        warnings.append(make_review_warning(code, message=message, recovery=recovery))
        reported_findings.append(_reported_finding(code, comment))

    return ReviewAudit(
        grade=grade.strip().upper(),
        verdict=verdict,
        summary=_sanitize_summary(summary),
        findings=tuple(findings),
        scope_expansions=normalized_scope_expansions,
        raw_feedback=raw_feedback,
        valid=True,
        warnings=tuple(warnings),
        reported_findings=tuple(reported_findings),
    )


def _record_budget_warning(warnings: list[ReviewWarning]) -> None:
    """Record the reporting budget warning once and keep the list bounded."""
    if len(warnings) >= MAX_REVIEW_WARNINGS:
        return
    if any(warning.code == "malformed_finding_budget_exhausted" for warning in warnings):
        return
    message, recovery = _FINDING_WARNING_TEXT["malformed_finding_budget_exhausted"]
    warnings.append(
        make_review_warning(
            "malformed_finding_budget_exhausted", message=message, recovery=recovery
        )
    )


def _reported_finding(code: str, comment: object) -> dict[str, object]:
    """Carry one malformed finding as bounded text for a later public report."""
    reported: dict[str, object] = {"code": code, "text": _reported_finding_text(comment)}
    if isinstance(comment, Mapping):
        path = comment.get("path")
        line = comment.get("line")
        if isinstance(path, str):
            reported["path"] = bounded_review_text(path, MAX_REVIEW_REPORTED_FINDING_CHARS)
        if isinstance(line, int) and not isinstance(line, bool):
            reported["line"] = line
    return reported


def _reported_finding_text(comment: object) -> str:
    """Return bounded and redacted text for one malformed finding."""
    try:
        text = json.dumps(
            _redact_finding_value(comment), ensure_ascii=False, sort_keys=True, default=str
        )
    except (TypeError, ValueError):
        text = str(_redact_finding_value(comment))
    return bounded_review_text(text, MAX_REVIEW_REPORTED_FINDING_CHARS)


def _redact_finding_value(value: object) -> object:
    """Return one finding value with its untrusted control text removed.

    The host redacts each untrusted string before it encodes the finding, so a
    reported finding keeps its JSON structure for a later public report.
    """
    if isinstance(value, str):
        return _redact_reserved_control(value)
    if isinstance(value, (list, tuple)):
        return [_redact_finding_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _redact_finding_value(item) for key, item in value.items()}
    return value


def _redact_reserved_control(text: str) -> str:
    """Remove pipeline-owned control text before the host stores or reports it.

    A malformed finding is reported as text only. The host never publishes it
    inline, and the host never carries a control token into a public comment or
    a later review prompt.
    """
    redacted = _RESERVED_AUTHORITY_CLAIM_RE.sub("", text)
    redacted = _SEVERITY_MARKER_RE.sub(_REDACTED_CONTROL_TEXT, redacted)
    return redacted.replace(SCOPE_RETRACTION_MARKER_PREFIX, _REDACTED_CONTROL_TEXT)


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


def _finding_field_reason(comment: Mapping[str, object]) -> MalformedReviewFindingReason | None:
    """Return why one finding field is not acceptable, or None for a valid field set.

    Args:
        comment: One raw reviewer finding.

    Returns:
        One typed reason, or None.

    """
    path = comment.get("path")
    if not isinstance(path, str) or not path.strip():
        return "finding_path_invalid"
    line = comment.get("line")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return "finding_line_invalid"
    if comment.get("side") != "RIGHT":
        return "finding_side_unsupported"
    severity = comment.get("severity")
    if not isinstance(severity, str) or severity.lower() not in _VALID_SEVERITIES:
        return "finding_severity_invalid"
    body = comment.get("body")
    if not isinstance(body, str) or not body.strip():
        return "finding_body_invalid"
    if has_reserved_finding_control(body):
        return "finding_reserved_control_text"
    finding_id = comment.get("finding_id")
    if finding_id is not None and (
        not isinstance(finding_id, str) or re.fullmatch(r"[0-9a-f]{64}", finding_id) is None
    ):
        return "finding_id_invalid"
    evidence = comment.get("evidence")
    if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
        return "finding_evidence_invalid"
    return None


def _normalize_finding(comment: object) -> _FindingNormalization:
    """Validate and normalize one finding, and report why a finding fails.

    Args:
        comment: One raw reviewer finding.

    Returns:
        The normalized finding and no reason, or no finding and one typed
        reason that states why the host did not accept the finding.

    """
    if not isinstance(comment, Mapping):
        return None, "finding_not_an_object"
    reason = _finding_field_reason(comment)
    if reason is not None:
        return None, reason
    body = cast(str, comment["body"])
    scope_retraction_paths = comment.get("scope_retraction_paths")
    if is_scope_retraction_finding(body):
        paths = normalize_scope_retraction_paths(scope_retraction_paths)
        if paths is None:
            return None, "finding_scope_retraction_paths_invalid"
        # A scope retraction is a publication-safety boundary, not advisory
        # review prose. Host-normalize it to blocking before POST filters
        # minor/nitpick comments out of the remediation path.
        normalized_severity = "major"
    elif scope_retraction_paths is not None:
        return None, "finding_scope_retraction_conflict"
    else:
        paths = ()
        normalized_severity = cast(str, comment["severity"]).lower()
    finding: dict[str, object] = {
        "path": cast(str, comment["path"]).strip(),
        "line": cast(int, comment["line"]),
        "side": "RIGHT",
        "severity": normalized_severity,
        "body": body.strip(),
    }
    finding_id = comment.get("finding_id")
    if finding_id is not None:
        finding["finding_id"] = cast(str, finding_id)
    evidence = comment.get("evidence")
    if evidence is not None:
        finding["evidence"] = cast(str, evidence).strip()
    if paths:
        finding["scope_retraction_paths"] = paths
    return finding, None


def has_reserved_finding_control(body: str) -> bool:
    """Return whether a finding body contains a pipeline-owned control."""
    return bool(
        _SEVERITY_MARKER_RE.search(body)
        or SCOPE_RETRACTION_MARKER_PREFIX in body
        or _RESERVED_AUTHORITY_CLAIM_RE.search(body)
    )


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
    match = _JSON_BLOCK_RE.search(source) if payload is not None else None
    if match:
        feedback = "\n\n".join(
            piece
            for piece in (source[: match.start()].strip(), source[match.end() :].strip())
            if piece
        )
    else:
        feedback = source.strip()
    # Reviewer prose is informational only. Never carry a decision-shaped
    # token forward into a durable audit comment or a later review prompt:
    # GitHub's implementation-state label is the sole automated eligibility
    # authority; merge_wait separately checks the reviewed head and CI state.
    feedback = _RESERVED_AUTHORITY_CLAIM_RE.sub("", feedback).strip()
    if len(feedback) <= MAX_RAW_FEEDBACK_CHARS:
        return feedback
    return f"{feedback[: MAX_RAW_FEEDBACK_CHARS - 15].rstrip()}... [truncated]"


def _invalid_audit(raw_feedback: str) -> ReviewAudit:
    """Build a fail-closed audit result."""
    return ReviewAudit(
        grade=None,
        verdict=None,
        summary=_INVALID_SUMMARY,
        findings=(),
        scope_expansions=(),
        raw_feedback=raw_feedback,
        valid=False,
    )


def render_review_audit(audit: ReviewAudit) -> str:
    """Render the bounded informational PR comment for a review audit.

    The reviewer verdict is audit evidence. Callers must also obtain a fresh,
    confirmed GitHub label transition.
    """
    summary = _sanitize_summary(audit.summary)
    return (
        "## Automated PR review\n\n"
        f"Reviewer verdict: {audit.verdict or 'invalid'}\n\n"
        f"Total grade: {audit.grade or 'ungraded'}\n\n"
        f"Review summary: {summary}\n\n"
        "Eligibility is represented only by the live GitHub implementation-state label; "
        "this audit comment is informational."
    )


def render_implementation_go_audit(
    audit: ReviewAudit,
    *,
    pr_number: int,
    head_sha: str,
    finding_records: object = (),
    compacted_outcomes: object = None,
) -> tuple[str, str]:
    """Render one public, idempotent audit comment for an approved PR head."""
    if not is_clean_go_review(audit):
        raise ValueError("implementation-go audit must have a clean GO verdict")
    if pr_number <= 0:
        raise ValueError("pr_number must be positive")
    if _FULL_COMMIT_SHA_RE.fullmatch(head_sha) is None:
        raise ValueError("head_sha must be a full commit SHA")
    marker = f"<!-- hephaestus-implementation-go-audit:pr={pr_number}:head={head_sha} -->"
    body = f"{marker}\n\n{render_review_audit(audit)}\n\nReviewed head: `{head_sha}`."
    records, compacted = normalize_review_finding_collection(
        finding_records,
        compacted_outcomes=compacted_outcomes,
    )
    if records or compacted["identities"]:
        import base64

        counts = compacted["counts"]
        lines = ["## Retained review findings"]
        if compacted["identities"]:
            lines.append(
                "Earlier outcomes: "
                f"published {counts['published']}, corrected {counts['corrected']}, "
                f"not publishable {counts['not_publishable']}"
            )
        for record in records:
            lines.append(
                "- "
                f"`{record['status']}` `{record['severity']}` from "
                f"`{record['source_head']}`: {escape(str(record['body']), quote=False)}"
            )
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                review_finding_collection_payload(records, compacted),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        body = (
            f"{body}\n\n" + "\n".join(lines) + "\n\n"
            f"<!-- hephaestus-review-finding-records:{encoded} -->"
        )
    if len(body) > 65_536:
        raise ValueError("implementation-go audit exceeds the GitHub comment size limit")
    return marker, body
