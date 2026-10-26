"""Pure protocol for autonomous issue-requirements recovery."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Final

from markdown_it import MarkdownIt

from hephaestus.automation.prompts._shared import fence_content
from hephaestus.automation.protocol import (
    FINALIZED_PLAN_PREFIX,
    FINALIZED_PLAN_PREFIXES,
    LEGACY_FINALIZED_PLAN_PREFIXES,
    PLAN_CANONICAL_MARKERS,
    PLAN_REVIEW_CANONICAL_MARKERS,
)
from hephaestus.automation.review_journal import HISTORY_RE
from hephaestus.automation.state_labels import is_epic
from hephaestus.prompts import PromptCatalog

RECOVERED_REQUIREMENTS_MARKER: Final[str] = "<!-- hephaestus-recovered-requirements -->"
OBSOLETE_EXPLANATION_MARKER: Final[str] = "<!-- hephaestus-obsolete-explanation:v=1 -->"
HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX: Final[str] = FINALIZED_PLAN_PREFIX
"""Current shared finalized-plan marker prefix."""

ATHENA_FINALIZED_PLAN_PREFIX: Final[str] = LEGACY_FINALIZED_PLAN_PREFIXES[0]
"""Deprecated read-only prefix retained for existing Athena-finalized bodies."""

# CommonMark treats an HTML comment with up to three leading spaces as
# top-level content, so the candidate match allows that indentation.
_FINALIZED_PLAN_CANDIDATE_RE = re.compile(
    rf"^ {{0,3}}(?:{'|'.join(re.escape(prefix.rstrip()) for prefix in FINALIZED_PLAN_PREFIXES)})"
)
_COMMONMARK_LINE_END_RE = re.compile(r"\r\n|\r|\n")
_OBSOLETE_TITLE_RE = re.compile(r"^\s*(?:\[[^]]*obsolete[^]]*\]|obsolete\s*:)", re.IGNORECASE)
_OBSOLETE_BODY_RE = re.compile(
    r"\b(?:already (?:resolved|implemented|fixed)|no longer (?:needed|applicable)|"
    r"superseded by|duplicate of)\b",
    re.IGNORECASE,
)


class RecoveryDisposition(StrEnum):
    """Semantic disposition proposed for an issue entering planning."""

    REQUIREMENTS = "REQUIREMENTS"
    TRACKER = "TRACKER"
    OBSOLETE = "OBSOLETE"


class RecoveryVerdict(StrEnum):
    """Independent recovery-review verdict."""

    GO = "GO"
    NOGO = "NOGO"


@dataclass(frozen=True, slots=True)
class RecoveredRequirements:
    """One evidence-bound planner proposal."""

    disposition: RecoveryDisposition
    requirements: str
    reason: str
    evidence: str


@dataclass(frozen=True, slots=True)
class RecoveryReview:
    """Independent review of a recovery proposal."""

    verdict: RecoveryVerdict
    disposition: RecoveryDisposition
    reason: str


def _finalized_plan_candidate_lines(body: str) -> list[tuple[int, str]]:
    """Return offsets and lines for top-level shared finalization claims."""
    # CommonMark recognizes CR, LF, and CRLF as line endings. ``splitlines``
    # recognizes additional Unicode separators and would desynchronize these
    # raw indexes from markdown-it-py's token ``map`` line numbers.
    raw_lines: list[str] = []
    line_offsets: list[int] = []
    line_start = 0
    for line_end in _COMMONMARK_LINE_END_RE.finditer(body):
        line_offsets.append(line_start)
        raw_lines.append(body[line_start : line_end.end()])
        line_start = line_end.end()
    if line_start < len(body):
        line_offsets.append(line_start)
        raw_lines.append(body[line_start:])

    candidates: list[tuple[int, str]] = []
    # Token levels distinguish document-level HTML comments from examples in
    # fences, indented code, block quotes, and list containers. Delegating that
    # block structure to a CommonMark parser also honors implicit container
    # closure instead of maintaining a partial fence state machine here.
    for token in MarkdownIt("commonmark").parse(body):
        if token.type != "html_block" or token.level != 0 or token.map is None:
            continue
        start_line, _end_line = token.map
        raw_line = raw_lines[start_line]
        line = raw_line.rstrip("\r\n")
        # Authority must open its own top-level HTML block. Scanning later
        # lines would accept marker-shaped text nested inside an outer HTML
        # comment, script, or container block.
        if _FINALIZED_PLAN_CANDIDATE_RE.match(line):
            candidates.append((line_offsets[start_line], line))
    return candidates


def has_finalized_plan_candidate(body: str) -> bool:
    """Return whether a top-level line claims shared finalization."""
    return bool(_finalized_plan_candidate_lines(body))


def has_contaminated_issue_body(body: str) -> bool:
    """Return whether the first non-whitespace line is a canonical derived artifact."""
    first_line = body.lstrip().partition("\n")[0].strip()
    return bool(
        first_line in {*PLAN_CANONICAL_MARKERS, *PLAN_REVIEW_CANONICAL_MARKERS}
        or HISTORY_RE.fullmatch(first_line)
    )


def is_semantic_disposition_candidate(title: str, body: str) -> bool:
    """Select narrow tracker/obsolete candidates for independent model review."""
    return bool(
        is_epic((), title) or _OBSOLETE_TITLE_RE.search(title) or _OBSOLETE_BODY_RE.search(body)
    )


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("recovery output must be one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("recovery output must be one JSON object")
    return value


def _required_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    return normalized


def parse_recovered_requirements(raw: str) -> RecoveredRequirements:
    """Parse a strict planner result, failing closed on schema drift."""
    value = _json_object(raw)
    expected = {"disposition", "requirements", "reason", "evidence"}
    if set(value) != expected:
        raise ValueError("recovery proposal fields do not match the required schema")
    try:
        disposition = RecoveryDisposition(value["disposition"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid recovery disposition") from exc
    requirements = _required_text(
        value["requirements"],
        "requirements",
        allow_empty=disposition is not RecoveryDisposition.REQUIREMENTS,
    )
    if disposition is not RecoveryDisposition.REQUIREMENTS and requirements:
        raise ValueError("tracker and obsolete proposals must not contain requirements")
    return RecoveredRequirements(
        disposition=disposition,
        requirements=requirements,
        reason=_required_text(value["reason"], "reason"),
        evidence=_required_text(value["evidence"], "evidence"),
    )


def parse_recovery_review(raw: str) -> RecoveryReview:
    """Parse a strict independent-review result."""
    value = _json_object(raw)
    expected = {"verdict", "disposition", "reason"}
    if set(value) != expected:
        raise ValueError("recovery review fields do not match the required schema")
    try:
        verdict = RecoveryVerdict(value["verdict"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid recovery review verdict") from exc
    try:
        disposition = RecoveryDisposition(value["disposition"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid recovery review disposition") from exc
    return RecoveryReview(
        verdict=verdict,
        disposition=disposition,
        reason=_required_text(value["reason"], "reason"),
    )


def recovered_requirements_json(proposal: RecoveredRequirements) -> str:
    """Serialize a proposal deterministically for the independent reviewer."""
    payload = asdict(proposal)
    payload["disposition"] = proposal.disposition.value
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def render_recovered_requirements(requirements: str) -> str:
    """Render recovered requirements under the plain recovery marker."""
    normalized = requirements.strip()
    if not normalized:
        raise ValueError("requirements must not be empty")
    return f"{RECOVERED_REQUIREMENTS_MARKER}\n\n{normalized}"


def recovered_requirements(body: str) -> str | None:
    """Return the requirements text of a recovery comment, or None."""
    first_line, separator, remainder = body.partition("\n")
    if first_line.rstrip("\r") != RECOVERED_REQUIREMENTS_MARKER or not separator:
        return None
    return remainder.lstrip("\n") or None


def select_recovery_comment[T](
    comments: Sequence[T],
    *,
    body_of: Callable[[T], str],
    owned_of: Callable[[T], bool],
) -> T | None:
    """Return the newest actor-owned recovery comment, or None.

    Args:
        comments: Issue comments in chronological order.
        body_of: Return the body of one comment.
        owned_of: Return True when the actor wrote the comment.

    Returns:
        The newest comment whose body starts with the recovery marker.

    """
    for comment in reversed(comments):
        if owned_of(comment) and recovered_requirements(body_of(comment)) is not None:
            return comment
    return None


def render_obsolete_explanation(reason: str) -> str:
    """Render the one actor-owned explanation for a confirmed obsolete issue."""
    return (
        f"{OBSOLETE_EXPLANATION_MARKER}\n\n"
        f"Automation skipped this issue as obsolete: {reason.strip()}"
    )


def build_recovery_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repository: str,
    repository_revision: str,
) -> str:
    """Build the evidence-fenced requirements reconstruction prompt."""
    fenced = fence_content()
    return PromptCatalog.current().render(
        "planning/requirements_recovery.j2",
        untrusted_notice=fenced.untrusted_notice,
        issue_number=issue_number,
        repository=repository,
        repository_revision=repository_revision,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
    )


def build_recovery_review_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    proposal_json: str,
    repository: str,
    repository_revision: str,
) -> str:
    """Build the independent semantic review prompt."""
    fenced = fence_content()
    return PromptCatalog.current().render(
        "planning/requirements_recovery_review.j2",
        untrusted_notice=fenced.untrusted_notice,
        issue_number=issue_number,
        repository=repository,
        repository_revision=repository_revision,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        proposal_block=fenced.fence("RECOVERY_PROPOSAL", proposal_json),
    )
