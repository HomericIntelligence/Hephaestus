"""Pure protocol for autonomous issue-requirements recovery."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Final

from hephaestus.automation.prompts._shared import fence_content
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.review_journal import HISTORY_RE
from hephaestus.automation.state_labels import is_epic
from hephaestus.prompts import PromptCatalog

RECOVERY_PROVENANCE_VERSION: Final[int] = 2
RECOVERY_PROVENANCE_PREFIX: Final[str] = "<!-- hephaestus-recovered-requirements:"
OBSOLETE_EXPLANATION_MARKER: Final[str] = "<!-- hephaestus-obsolete-explanation:v=1 -->"

_DIGEST_RE = r"[0-9a-f]{64}"
_PROVENANCE_RE = re.compile(
    rf"^<!-- hephaestus-recovered-requirements:v=(?P<version>\d+):"
    rf"source=(?P<source>{_DIGEST_RE}):requirements=(?P<requirements>{_DIGEST_RE}):"
    rf"evidence=(?P<evidence>{_DIGEST_RE})"
    rf"(?::successor_revision=(?P<successor_revision>\d+):"
    rf"successor_plan=(?P<successor_plan>{_DIGEST_RE}))? -->$"
)
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
    successor_revision: int | None = None
    successor_plan_digest: str | None = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def has_contaminated_issue_body(body: str) -> bool:
    """Return whether the first non-whitespace line is a canonical derived artifact."""
    first_line = body.lstrip().partition("\n")[0].strip()
    return bool(
        first_line in {PLAN_CANONICAL_MARKER, PLAN_REVIEW_CANONICAL_MARKER}
        or HISTORY_RE.fullmatch(first_line)
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
    marker = (
        f"{RECOVERY_PROVENANCE_PREFIX}v={RECOVERY_PROVENANCE_VERSION}:"
        f"source={bound_source_digest}:requirements={_sha256(normalized)}:"
        f"evidence={evidence_binding}"
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
    if version not in {1, RECOVERY_PROVENANCE_VERSION}:
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
