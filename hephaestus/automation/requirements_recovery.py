"""Pure protocol for autonomous issue-requirements recovery."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Final

from markdown_it import MarkdownIt

from hephaestus.automation.prompts._shared import fence_content
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.review_journal import HISTORY_RE
from hephaestus.automation.state_labels import is_epic
from hephaestus.prompts import PromptCatalog

RECOVERY_PROVENANCE_VERSION: Final[int] = 3
RECOVERY_PROVENANCE_PREFIX: Final[str] = "<!-- hephaestus-recovered-requirements:"
OBSOLETE_EXPLANATION_MARKER: Final[str] = "<!-- hephaestus-obsolete-explanation:v=1 -->"
ATHENA_FINALIZED_PLAN_PREFIX: Final[str] = "<!-- athena:finalize-plan "

_DIGEST_RE = r"[0-9a-f]{64}"
# GitHub issue comments expose a positive REST ``id`` and an ``IC_`` GraphQL
# node ID. Artifact role names such as ``plan-comment`` are not identities and
# cannot bind Athena's sealed source comments.
_FINALIZED_COMMENT_ID_RE = r"(?:[1-9][0-9]*|IC_[A-Za-z0-9_-]+)"
_FINALIZED_ARTIFACT_IDENTITY_RE = rf"{_FINALIZED_COMMENT_ID_RE}:{_DIGEST_RE}"
_PROVENANCE_RE = re.compile(
    rf"^<!-- hephaestus-recovered-requirements:v=(?P<version>\d+):"
    rf"source=(?P<source>{_DIGEST_RE}):requirements=(?P<requirements>{_DIGEST_RE}):"
    rf"evidence=(?P<evidence>{_DIGEST_RE})"
    rf"(?::title=(?P<title>{_DIGEST_RE}):revision=(?P<revision>[0-9a-f]{{40}}))?"
    rf"(?::successor_revision=(?P<successor_revision>\d+):"
    rf"successor_plan=(?P<successor_plan>{_DIGEST_RE}))? -->$"
)
_FINALIZED_PLAN_RE = re.compile(
    rf"^{re.escape(ATHENA_FINALIZED_PLAN_PREFIX)}"
    rf"R=(?P<requirements>{_DIGEST_RE}) "
    rf"P=(?P<plan>{_FINALIZED_ARTIFACT_IDENTITY_RE}) "
    rf"V=(?P<review>{_FINALIZED_ARTIFACT_IDENTITY_RE}) "
    rf"F=(?P<final>{_DIGEST_RE}) -->$",
)
# CommonMark still treats an HTML comment with up to three leading spaces as
# top-level content. Keep the raw line as the candidate so any indentation
# fails exact seal verification instead of hiding a malformed authority claim.
_FINALIZED_PLAN_CANDIDATE_RE = re.compile(
    rf"^ {{0,3}}{re.escape(ATHENA_FINALIZED_PLAN_PREFIX.rstrip())}"
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


@dataclass(frozen=True, slots=True)
class RecoveryProvenance:
    """Digests encoded in a recovered issue body's hidden marker."""

    version: int
    source_digest: str
    requirements_digest: str
    evidence_digest: str
    title_digest: str | None = None
    repository_revision: str | None = None
    successor_revision: int | None = None
    successor_plan_digest: str | None = None


@dataclass(frozen=True, slots=True)
class FinalizedPlanIdentity:
    """Sealed identities from one self-verifying Athena finalized body."""

    requirements_identity: str
    plan_identity: str
    review_identity: str
    final_body_digest: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _finalized_plan_candidate_lines(body: str) -> list[tuple[int, str]]:
    """Return offsets and lines for top-level Athena finalization claims."""
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


def _has_finalized_plan_candidate(body: str) -> bool:
    """Return whether a top-level line claims Athena finalization."""
    return bool(_finalized_plan_candidate_lines(body))


def verified_finalized_plan(body: str) -> FinalizedPlanIdentity | None:
    """Return a finalized-plan identity only when its exact body verifies ``F``.

    Athena defines ``F`` as SHA-256 over the complete UTF-8 issue body after
    replacing the marker's concrete ``F`` value with the literal ``<F>``.
    Requiring one exact top-level marker means malformed, duplicated, inline,
    or materially edited bodies cannot suppress a fresh planning epoch.
    """
    candidates = _finalized_plan_candidate_lines(body)
    if len(candidates) != 1:
        return None
    line_offset, marker_line = candidates[0]
    match = _FINALIZED_PLAN_RE.fullmatch(marker_line)
    if match is None:
        return None
    plan_identity = match.group("plan")
    review_identity = match.group("review")
    if plan_identity.partition(":")[0] == review_identity.partition(":")[0]:
        return None
    final_start = line_offset + match.start("final")
    final_end = line_offset + match.end("final")
    canonical_body = body[:final_start] + "<F>" + body[final_end:]
    if _sha256(canonical_body) != match.group("final"):
        return None
    return FinalizedPlanIdentity(
        requirements_identity=match.group("requirements"),
        plan_identity=plan_identity,
        review_identity=review_identity,
        final_body_digest=match.group("final"),
    )


def has_contaminated_issue_body(body: str) -> bool:
    """Return whether the first non-whitespace line is a canonical derived artifact."""
    first_line = body.lstrip().partition("\n")[0].strip()
    return bool(
        first_line in {PLAN_CANONICAL_MARKER, PLAN_REVIEW_CANONICAL_MARKER}
        or HISTORY_RE.fullmatch(first_line)
        or (_has_finalized_plan_candidate(body) and verified_finalized_plan(body) is None)
    )


def is_semantic_disposition_candidate(title: str, body: str) -> bool:
    """Select narrow tracker/obsolete candidates for independent model review."""
    return bool(
        is_epic((), title) or _OBSOLETE_TITLE_RE.search(title) or _OBSOLETE_BODY_RE.search(body)
    )


def evidence_digest(
    repository: str,
    issue_number: int,
    repository_revision: str,
    issue_title: str,
    source_body: str,
) -> str:
    """Digest the immutable identity supplied to the recovery agents."""
    encoded = json.dumps(
        {
            "issue": issue_number,
            "repository": repository,
            "repository_revision": repository_revision,
            "source_body_digest": _sha256(source_body),
            "title": issue_title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _sha256(encoded)


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


def render_recovered_requirements(
    source_body: str,
    requirements: str,
    evidence_binding: str,
    *,
    source_digest: str | None = None,
    successor_revision: int | None = None,
    successor_plan_digest: str | None = None,
    issue_title: str | None = None,
    repository_revision: str | None = None,
) -> str:
    """Render requirements with a versioned, digest-bound hidden marker."""
    normalized = requirements.strip()
    if not normalized:
        raise ValueError("requirements must not be empty")
    if re.fullmatch(_DIGEST_RE, evidence_binding) is None:
        raise ValueError("evidence_binding must be a lowercase SHA-256 digest")
    bound_source_digest = source_digest or _sha256(source_body)
    if re.fullmatch(_DIGEST_RE, bound_source_digest) is None:
        raise ValueError("source_digest must be a lowercase SHA-256 digest")
    if (successor_revision is None) != (successor_plan_digest is None):
        raise ValueError("recovery successor revision and plan digest must be supplied together")
    if successor_revision is not None and successor_revision < 1:
        raise ValueError("recovery successor revision must be positive")
    if (
        successor_plan_digest is not None
        and re.fullmatch(_DIGEST_RE, successor_plan_digest) is None
    ):
        raise ValueError("recovery successor plan digest must be a lowercase SHA-256 digest")
    if (issue_title is None) != (repository_revision is None):
        raise ValueError("issue title and repository revision must be supplied together")
    contextual = issue_title is not None and repository_revision is not None
    if (
        issue_title is not None
        and repository_revision is not None
        and re.fullmatch(r"[0-9a-f]{40}", repository_revision) is None
    ):
        raise ValueError("repository revision must be a lowercase full SHA-1")
    version = RECOVERY_PROVENANCE_VERSION if contextual else 2
    context_marker = (
        f":title={_sha256(issue_title)}:revision={repository_revision}"
        if issue_title is not None and repository_revision is not None
        else ""
    )
    marker = (
        f"{RECOVERY_PROVENANCE_PREFIX}v={version}:"
        f"source={bound_source_digest}:requirements={_sha256(normalized)}:"
        f"evidence={evidence_binding}"
        + context_marker
        + (
            f":successor_revision={successor_revision}:successor_plan={successor_plan_digest}"
            if successor_revision is not None
            else ""
        )
        + " -->"
    )
    return f"{marker}\n\n{normalized}"


def parse_recovery_provenance(body: str) -> RecoveryProvenance | None:
    """Return valid provenance only when the marker and rendered body agree."""
    stripped = body.lstrip()
    first_line, separator, remainder = stripped.partition("\n")
    match = _PROVENANCE_RE.fullmatch(first_line.strip())
    if match is None or not separator:
        return None
    version = int(match.group("version"))
    requirements = remainder.lstrip("\n")
    successor_revision = match.group("successor_revision")
    successor_plan = match.group("successor_plan")
    title_digest = match.group("title")
    repository_revision = match.group("revision")
    if version not in {1, 2, RECOVERY_PROVENANCE_VERSION}:
        return None
    if version >= 3 and (title_digest is None or repository_revision is None):
        return None
    if version < 3 and (title_digest is not None or repository_revision is not None):
        return None
    if (successor_revision is None) != (successor_plan is None):
        return None
    if _sha256(requirements) != match.group("requirements"):
        return None
    return RecoveryProvenance(
        version=version,
        source_digest=match.group("source"),
        requirements_digest=match.group("requirements"),
        evidence_digest=match.group("evidence"),
        title_digest=title_digest,
        repository_revision=repository_revision,
        successor_revision=int(successor_revision) if successor_revision is not None else None,
        successor_plan_digest=successor_plan,
    )


def recovered_requirements_for_source(body: str, source_digest: str) -> str | None:
    """Return a verified recovered-comment payload bound to *source_digest*."""
    provenance = parse_recovery_provenance(body)
    if provenance is None or provenance.source_digest != source_digest:
        return None
    _marker, _separator, requirements = body.lstrip().partition("\n")
    return requirements.lstrip("\n") or None


def recovered_requirements_for_context(
    body: str,
    *,
    repository: str,
    issue_number: int,
    issue_title: str,
    source_body: str,
    repository_revision: str,
) -> str | None:
    """Return requirements only when every recovery evidence identity is current."""
    provenance = parse_recovery_provenance(body)
    if provenance is None or provenance.version < RECOVERY_PROVENANCE_VERSION:
        return None
    if (
        provenance.source_digest != _sha256(source_body)
        or provenance.title_digest != _sha256(issue_title)
        or provenance.repository_revision != repository_revision
        or provenance.evidence_digest
        != evidence_digest(
            repository,
            issue_number,
            repository_revision,
            issue_title,
            source_body,
        )
    ):
        return None
    _marker, _separator, requirements = body.lstrip().partition("\n")
    return requirements.lstrip("\n") or None


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
    evidence_binding: str,
) -> str:
    """Build the evidence-fenced requirements reconstruction prompt."""
    fenced = fence_content()
    return PromptCatalog.current().render(
        "planning/requirements_recovery.j2",
        untrusted_notice=fenced.untrusted_notice,
        issue_number=issue_number,
        repository=repository,
        repository_revision=repository_revision,
        evidence_binding=evidence_binding,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
    )


def build_recovery_review_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    source_body_digest: str,
    evidence_binding: str,
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
        source_body_digest=source_body_digest,
        evidence_binding=evidence_binding,
        proposal_block=fenced.fence("RECOVERY_PROPOSAL", proposal_json),
    )
