"""Pure model and rendering helpers for canonical plan-review comments.

The current plan and review supply the active journal. Exclusive ``state:*``
labels control the pipeline state. Retired archives supply no current authority.

A malformed issue comment is quarantined per item. The host warns about it and
keeps the usable comments, so one bad comment does not stop a work item. A
comment that can change pipeline state stays all-or-nothing.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, MutableSequence, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from hephaestus.automation.comment_identity import (
    CommentAliasConflictError,
    validate_planning_comment_identities,
)
from hephaestus.automation.markdown_markers import (
    raw_markdown_lines as _raw_markdown_lines,
    top_level_marker_line_indexes as _top_level_marker_line_indexes,
    top_level_marker_occurrences as top_level_marker_occurrences,
)
from hephaestus.automation.protocol import (
    HOMERIC_INTELLIGENCE_COMMENT_PREFIX,
    PLAN_CANONICAL_MARKER,
    PLAN_CANONICAL_MARKERS,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKERS,
    PLAN_REVIEW_PREFIX,
)
from hephaestus.automation.review_warnings import ReviewWarning, record_review_warning

HISTORY_MARKER: Final[str] = "<!-- hephaestus-plan-history:revision={revision}:kind={kind} -->"
HISTORY_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-history:"
HISTORY_RE: Final[re.Pattern[str]] = re.compile(
    r"^<!-- hephaestus-plan-history:revision=(?P<revision>\d+):"
    r"kind=(?P<kind>plan|review) -->(?=\r?\n|\Z)"
)
REVISION_RE: Final[re.Pattern[str]] = re.compile(r"<!-- revision: (?P<revision>\d+) -->")
PLAN_FINGERPRINTS_RE: Final[re.Pattern[str]] = re.compile(
    r"<!-- prior-plan-fingerprints: (?P<fingerprints>[0-9a-f,]*) -->"
)
FORCED_PLANNING_EPOCH_MARKER: Final[str] = "<!-- hephaestus-forced-planning-epoch -->"
RECOVERY_SOURCE_EPOCH_RE: Final[re.Pattern[str]] = re.compile(
    r"<!-- hephaestus-recovery-source-epoch: (?P<source>[0-9a-f]{64}) -->"
)
RAW_PATCH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^\s*```diff\s*$|^\s*diff --git\s+|^\s*@@\s+-\d+(?:,\d+)?\s+\+\d+"
)
MAX_PRIOR_PLAN_FINGERPRINTS: Final[int] = 16
PLAN_REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {"state:plan-go", "state:plan-no-go", "state:plan-blocked"}
)

#: Planning/review prompts receive the current plan and/or latest review in
#: dedicated fields. Keep the restart-only current-revision context bounded
#: so legacy superseded artifacts cannot compete with active feedback.
MAX_CURRENT_REVISION_CONTEXT_CHARS: Final[int] = 12_000

_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: A comment that names any automation marker family can change the current
#: plan, the current review, or a recovery claim. The host cannot quarantine
#: such a comment, because a dropped artifact looks the same as no artifact.
_STATE_BEARING_COMMENT_MARKERS: Final[tuple[str, ...]] = (
    HOMERIC_INTELLIGENCE_COMMENT_PREFIX,
    HISTORY_MARKER_PREFIX,
    "<!-- hephaestus-recovered-requirements:",
    "<!-- hephaestus-obsolete-explanation:",
)
_QUARANTINE_RECOVERY = (
    "Delete or repair the malformed issue comment, then re-run the round so the "
    "host reads the complete journal again."
)


class CommentJournalReadError(RuntimeError):
    """A complete, ownership-verifiable GitHub comment read failed."""


@dataclass(frozen=True)
class IssueComment:
    """Issue comment metadata required for ownership and feedback decisions."""

    body: str
    author_login: str = ""
    author_association: str = ""
    created_at: str = ""
    updated_at: str = ""
    viewer_did_author: bool = False
    database_id: int | None = None
    url: str = ""

    @property
    def is_trusted_human(self) -> bool:
        """Return whether this is maintainer feedback rather than bot output."""
        login = self.author_login.lower()
        return (
            bool(self.body.strip())
            and self.author_association.upper() in _TRUSTED_ASSOCIATIONS
            and not login.endswith("[bot]")
        )


class PlanDiscoveryStatus(StrEnum):
    """Outcome of discovering an actor-owned canonical plan."""

    FOUND = "found"
    ABSENT = "absent"
    READ_ERROR = "read_error"
    IDENTITY_CONFLICT = "identity_conflict"


@dataclass(frozen=True, slots=True)
class PlanDiscoveryResult:
    """Plan-discovery outcome and its optional payload or diagnostic."""

    status: PlanDiscoveryStatus
    plan_text: str | None = None
    error: str | None = None

    @classmethod
    def found(cls, plan_text: str) -> PlanDiscoveryResult:
        """Create a successful result containing the canonical plan text."""
        return cls(PlanDiscoveryStatus.FOUND, plan_text=plan_text)

    @classmethod
    def absent(cls) -> PlanDiscoveryResult:
        """Create a successful result proving no actor-owned plan exists."""
        return cls(PlanDiscoveryStatus.ABSENT)

    @classmethod
    def read_error(cls, error: object) -> PlanDiscoveryResult:
        """Create a result describing an incomplete or invalid journal read."""
        return cls(PlanDiscoveryStatus.READ_ERROR, error=str(error))

    @classmethod
    def identity_conflict(cls, error: object) -> PlanDiscoveryResult:
        """Create a result describing an unsafe planning-marker identity."""
        return cls(PlanDiscoveryStatus.IDENTITY_CONFLICT, error=str(error))


@dataclass(frozen=True)
class JournalSnapshot:
    """Current canonical comments and their bounded plan metadata."""

    revision: int
    current_plan: str
    current_plan_body: str
    current_review: str
    current_review_body: str
    current_review_revision: int | None
    prior_plan_fingerprints: tuple[str, ...] = ()
    forced_planning_epoch: bool = False
    recovery_source_digest: str | None = None


def is_plan_comment(body: str) -> bool:
    """Recognize a plan comment only by an exact top-level marker line."""
    return bool(top_level_marker_occurrences(body, PLAN_CANONICAL_MARKERS))


def is_plan_review_comment(body: str) -> bool:
    """Recognize a review comment only by an exact top-level marker line."""
    return bool(top_level_marker_occurrences(body, PLAN_REVIEW_CANONICAL_MARKERS))


def has_exact_leading_marker(body: str, marker: str) -> bool:
    """Return whether *marker* occupies the complete first raw line of *body*."""
    return bool(marker) and (
        body == marker or body.startswith(f"{marker}\n") or body.startswith(f"{marker}\r\n")
    )


def _top_level_marker(body: str, markers: Sequence[str]) -> str | None:
    """Return the first semantic marker from one protocol family."""
    matches = top_level_marker_occurrences(body, markers)
    return matches[0] if matches else None


def _without_top_level_marker_line(text: str, marker: str | None) -> str:
    """Remove one recognized marker line while preserving all other raw text."""
    if marker is None:
        return text
    matches = _top_level_marker_line_indexes(text, (marker,))
    if not matches:
        return text
    line_index, _matched_marker = matches[0]
    lines = _raw_markdown_lines(text)
    return "".join((*lines[:line_index], *lines[line_index + 1 :]))


def _normalized_issue_comment(
    raw: object,
    *,
    actor: str,
) -> tuple[IssueComment | None, str]:
    """Validate one raw REST comment payload and derive its ownership.

    Args:
        raw: One raw REST comment payload.
        actor: Login of the authenticated GitHub session.

    Returns:
        The normalized comment, or None and a short failure reason.

    """
    if not isinstance(raw, Mapping):
        return None, "was not an object"

    body = raw.get("body")
    author = raw.get("user") or raw.get("author")
    login = author.get("login") if isinstance(author, Mapping) else None
    if not isinstance(body, str):
        return None, "body was not a string"
    if not isinstance(login, str) or not login.strip():
        return None, "author login was unavailable"

    database_id: int | None = None
    raw_database_id = raw.get("databaseId")
    if raw_database_id is None:
        raw_database_id = raw.get("id")
    if raw_database_id is not None:
        if (
            not isinstance(raw_database_id, int)
            or isinstance(raw_database_id, bool)
            or raw_database_id <= 0
        ):
            return None, "database id was not correct"
        database_id = raw_database_id

    return (
        IssueComment(
            body=body,
            author_login=login,
            author_association=str(
                raw.get("author_association") or raw.get("authorAssociation") or ""
            ),
            created_at=str(raw.get("created_at") or raw.get("createdAt") or ""),
            updated_at=str(raw.get("updated_at") or raw.get("updatedAt") or ""),
            viewer_did_author=login.casefold() == actor.casefold(),
            database_id=database_id,
            url=str(raw.get("html_url") or raw.get("url") or ""),
        ),
        "",
    )


def _comment_can_change_pipeline_state(raw: object) -> bool:
    """Return whether a malformed comment can still be a pipeline artifact."""
    if not isinstance(raw, Mapping):
        return False
    body = raw.get("body")
    if not isinstance(body, str):
        return False
    return any(marker in body for marker in _STATE_BEARING_COMMENT_MARKERS)


def normalize_issue_comments(
    comments: Sequence[object],
    *,
    viewer_login: str,
    warnings: MutableSequence[ReviewWarning] | None = None,
) -> list[IssueComment]:
    """Validate REST comments and derive ownership from authenticated identity.

    A plan lookup may report ``ABSENT`` only after every comment in the
    complete journal has been read.  In particular, ownership is derived
    from the authenticated viewer login and the comment's author metadata;
    caller-provided ownership flags are intentionally ignored.

    One malformed comment is quarantined. The host warns about it and returns
    the usable remainder.

    These checks stay all-or-nothing. Each one protects the durable state:

    - An unavailable viewer login stays all-or-nothing because the host cannot
      derive ownership for any comment without it.
    - A malformed comment that names an automation marker family stays
      all-or-nothing because a dropped plan, review, history, or recovery
      comment is indistinguishable from a comment that was never written.

    Args:
        comments: Raw REST comment payloads in chronological order.
        viewer_login: Login returned by the authenticated GitHub session.
        warnings: Collector that the caller reads, or None to log.

    Returns:
        Structured comments with derived ownership metadata.

    Raises:
        CommentJournalReadError: If the viewer identity is missing, or if a
            comment that can change pipeline state is malformed.

    """
    actor = viewer_login.strip()
    if not actor:
        raise CommentJournalReadError(
            "cannot verify GitHub comment ownership: viewer login unavailable"
        )

    normalized: list[IssueComment] = []
    for index, raw in enumerate(comments):
        comment, reason = _normalized_issue_comment(raw, actor=actor)
        if comment is not None:
            normalized.append(comment)
            continue
        if _comment_can_change_pipeline_state(raw):
            raise CommentJournalReadError(f"comment {index} {reason}")
        record_review_warning(
            warnings,
            "journal_comment_quarantined",
            message=f"Issue comment {index} {reason}. The host skipped it.",
            recovery=_QUARANTINE_RECOVERY,
        )
    return normalized


def discover_plan_from_comments(
    comments: Sequence[IssueComment],
) -> PlanDiscoveryResult:
    """Select the latest actor-owned plan from a validated complete journal.

    The identity checks stay all-or-nothing. More than one comment or marker
    alias that claims the same automation role means the host cannot tell which
    artifact is current, so it must not select one.
    """
    try:
        validate_planning_comment_identities(
            comments,
            body_of=lambda comment: comment.body,
            owned_of=lambda comment: comment.viewer_did_author,
        )
    except CommentAliasConflictError as exc:
        return PlanDiscoveryResult.identity_conflict(exc)
    for comment in reversed(comments):
        if not comment.viewer_did_author:
            continue
        body = comment.body
        if is_plan_review_comment(body):
            continue
        if is_plan_comment(body):
            return PlanDiscoveryResult.found(comment.body)
    return PlanDiscoveryResult.absent()


def is_journal_comment(body: str) -> bool:
    """Return whether the body is an automation-owned plan journal artifact."""
    return (
        is_plan_comment(body) or is_plan_review_comment(body) or HISTORY_RE.match(body) is not None
    )


def _without_leading_line(text: str, prefix: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith(prefix):
        return stripped
    first, separator, rest = stripped.partition("\n")
    if first.strip() != prefix:
        return stripped
    return rest if separator else ""


def _without_revision_line(text: str) -> str:
    stripped = text.lstrip()
    first, separator, rest = stripped.partition("\n")
    if REVISION_RE.fullmatch(first.strip()):
        return rest if separator else ""
    return stripped


def _without_fingerprint_line(text: str) -> str:
    stripped = text.lstrip()
    first, separator, rest = stripped.partition("\n")
    if PLAN_FINGERPRINTS_RE.fullmatch(first.strip()):
        return rest if separator else ""
    return stripped


def _current_plan_parts(body: str) -> tuple[str, tuple[str, ...], bool, str | None]:
    """Parse host metadata only from the canonical plan-comment header."""
    stripped = body.lstrip()
    canonical_marker = _top_level_marker(body, PLAN_CANONICAL_MARKERS)
    if canonical_marker is None and not stripped.startswith(PLAN_COMMENT_MARKER):
        return stripped.strip(), (), False, None

    text = (
        _without_top_level_marker_line(body, canonical_marker)
        if canonical_marker is not None
        else stripped
    )
    text = _without_leading_line(text, PLAN_COMMENT_MARKER)
    text = _without_revision_line(text)
    first = text.lstrip().partition("\n")[0].strip()
    fingerprints_match = PLAN_FINGERPRINTS_RE.fullmatch(first)
    fingerprints = (
        tuple(value for value in fingerprints_match.group("fingerprints").split(",") if value)
        if fingerprints_match is not None
        else ()
    )
    if fingerprints_match is not None:
        text = _without_fingerprint_line(text)

    forced = False
    recovery_source: str | None = None
    first = text.lstrip().partition("\n")[0].strip()
    if first == FORCED_PLANNING_EPOCH_MARKER:
        forced = True
        text = _without_leading_line(text, FORCED_PLANNING_EPOCH_MARKER)
    first = text.lstrip().partition("\n")[0].strip()
    recovery_match = RECOVERY_SOURCE_EPOCH_RE.fullmatch(first)
    if recovery_match is not None:
        recovery_source = recovery_match.group("source")
        text = _without_leading_line(text, first)
    return text.strip(), fingerprints, forced, recovery_source


def extract_current_plan(body: str) -> str:
    """Return only the plan payload from a current or legacy plan comment."""
    return _current_plan_parts(body)[0]


def extract_current_review(body: str) -> str:
    """Return only reviewer output from a current or legacy review comment."""
    marker = _top_level_marker(body, PLAN_REVIEW_CANONICAL_MARKERS)
    text = _without_top_level_marker_line(body, marker) if marker is not None else body
    text = _without_leading_line(text, PLAN_REVIEW_PREFIX)
    return _without_revision_line(text).strip()


def render_current_plan(
    plan: str,
    *,
    revision: int = 1,
    prior_fingerprints: Sequence[str] = (),
    forced_planning_epoch: bool = False,
    recovery_source_digest: str | None = None,
) -> str:
    """Render the editable current plan with an opaque canonical marker."""
    payload = extract_current_plan(plan)
    fingerprints = tuple(dict.fromkeys(prior_fingerprints))[-MAX_PRIOR_PLAN_FINGERPRINTS:]
    metadata = ""
    if fingerprints:
        metadata = f"\n<!-- prior-plan-fingerprints: {','.join(fingerprints)} -->"
    if forced_planning_epoch:
        metadata += f"\n{FORCED_PLANNING_EPOCH_MARKER}"
    if recovery_source_digest is not None:
        if re.fullmatch(r"[0-9a-f]{64}", recovery_source_digest) is None:
            raise ValueError("recovery source digest must be a lowercase SHA-256 digest")
        metadata += f"\n<!-- hephaestus-recovery-source-epoch: {recovery_source_digest} -->"
    return (
        f"{PLAN_CANONICAL_MARKER}\n{PLAN_COMMENT_MARKER}\n"
        f"<!-- revision: {revision} -->{metadata}\n\n{payload}"
    )


def render_current_review(review: str, *, revision: int) -> str:
    """Render the editable current review with an opaque canonical marker."""
    payload = extract_current_review(review)
    return (
        f"{PLAN_REVIEW_CANONICAL_MARKER}\n{PLAN_REVIEW_PREFIX}\n"
        f"<!-- revision: {revision} -->\n\n{payload}"
    )


def render_pending_review(*, revision: int) -> str:
    """Render the canonical review pointer before revision review completes."""
    return render_current_review(
        f"Review pending for implementation plan revision {revision}.",
        revision=revision,
    )


def blocked_audit_recovery_body(
    comments: Sequence[IssueComment],
) -> str | None:
    """Return an actionable repair body when BLOCKED lacks a current explanation.

    The plan-state label remains the sole plan-review state authority. This
    helper only repairs the
    audit half of an interrupted label-first BLOCKED transaction and never
    infers eligibility or removes the latch.
    """
    snapshot = journal_snapshot(comments)
    if (
        snapshot.current_review_revision == snapshot.revision
        and parse_plan_review_state(snapshot.current_review) == "state:plan-blocked"
    ):
        return None
    return render_current_review(
        "Automation confirmed `state:plan-blocked`, but an interrupted audit write "
        "left the original detailed explanation unavailable. An external operator "
        "must inspect the issue or automation logs, document and resolve the missing "
        "decision or dependency, then replace the blocked label with exactly one next "
        "plan-state label.\n\nstate:plan-blocked",
        revision=snapshot.revision,
    )


def is_pending_review(review: str, *, revision: int) -> bool:
    """Return whether canonical review content is the exact pending sentinel."""
    return extract_current_review(review) == (
        f"Review pending for implementation plan revision {revision}."
    )


def parse_plan_review_state(review: str) -> str | None:
    """Parse the sole final state token from a newly produced plan review.

    This parser intentionally does not recognize the historical ``Verdict:``
    vocabulary. Reviewer output selects a GitHub label only when exactly one
    supported state token is present as the final non-empty line. BLOCKED also
    requires explanatory prose so the canonical comment tells maintainers what
    input or dependency is missing.
    """
    lines = [line.strip() for line in review.splitlines() if line.strip()]
    matches = [line for line in lines if line in PLAN_REVIEW_STATES]
    if len(matches) != 1 or not lines or lines[-1] != matches[0]:
        return None
    if matches[0] == "state:plan-blocked":
        explanation = [
            line
            for line in lines[:-1]
            if not line.startswith(("#", "<!--")) and line not in PLAN_REVIEW_STATES
        ]
        if not explanation:
            return None
    return matches[0]


def comment_revision(body: str) -> int | None:
    """Read a canonical artifact's explicit revision, if present."""
    match = REVISION_RE.search(body)
    return int(match.group("revision")) if match else None


def normalized_plan(plan: str) -> str:
    """Normalize a plan for deterministic no-progress comparisons."""
    return "\n".join(line.rstrip() for line in extract_current_plan(plan).strip().splitlines())


def contains_raw_patch(plan: str) -> bool:
    """Return whether a proposed public plan embeds unified-diff material."""
    return RAW_PATCH_RE.search(extract_current_plan(plan)) is not None


def plan_fingerprint(plan: str) -> str:
    """Return a stable short fingerprint for a normalized plan."""
    return hashlib.sha256(normalized_plan(plan).encode("utf-8")).hexdigest()


def journal_snapshot(comments: Sequence[IssueComment]) -> JournalSnapshot:
    """Read current canonical comments with explicit ownership metadata.

    The ownership type check and the identity checks stay all-or-nothing. A
    comment without verified ownership, or a comment that claims the same
    automation role twice, leaves the host unable to name the current artifact.
    """
    if any(not isinstance(comment, IssueComment) for comment in comments):
        raise TypeError("journal comments require explicit ownership metadata")
    validate_planning_comment_identities(
        comments,
        body_of=lambda comment: comment.body,
        owned_of=lambda comment: comment.viewer_did_author,
    )
    current_plan_body = ""
    current_review_body = ""
    for comment in comments:
        if not comment.viewer_did_author:
            continue
        if is_plan_comment(comment.body):
            current_plan_body = comment.body
        elif is_plan_review_comment(comment.body):
            current_review_body = comment.body
    revision = comment_revision(current_plan_body) if current_plan_body else None
    review_revision = comment_revision(current_review_body) if current_review_body else None
    current_plan, prior_fingerprints, forced_epoch, recovery_source = _current_plan_parts(
        current_plan_body
    )
    return JournalSnapshot(
        revision=revision or 1,
        current_plan=current_plan if current_plan_body else "",
        current_plan_body=current_plan_body,
        current_review=(extract_current_review(current_review_body) if current_review_body else ""),
        current_review_body=current_review_body,
        current_review_revision=review_revision,
        prior_plan_fingerprints=prior_fingerprints,
        forced_planning_epoch=forced_epoch,
        recovery_source_digest=recovery_source,
    )


def _bounded_excerpt(text: str, max_chars: int) -> str:
    """Return a deterministic head/tail excerpt that fits *max_chars*."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n\n_[artifact excerpt truncated]_\n\n"
    if max_chars <= len(marker):
        return marker[:max_chars]
    remaining = max_chars - len(marker)
    head = (remaining + 1) // 2
    tail = remaining - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"


def current_plan_context(
    comments: Sequence[IssueComment],
    *,
    max_chars: int = MAX_CURRENT_REVISION_CONTEXT_CHARS,
) -> str:
    """Return a bounded canonical-plan excerpt without superseded revisions.

    Bounded fingerprint metadata retains oscillation detection without exposing
    superseded plans. This projection is supplemental context for a resumed
    planner, which receives the latest canonical critique separately.
    """
    if max_chars <= 0:
        return ""
    snapshot = journal_snapshot(comments)
    if not snapshot.current_plan:
        return ""
    plan = render_current_plan(
        snapshot.current_plan,
        revision=snapshot.revision,
        forced_planning_epoch=snapshot.forced_planning_epoch,
        recovery_source_digest=snapshot.recovery_source_digest,
    )
    return _bounded_excerpt(plan, max_chars)


def current_revision_context(
    comments: Sequence[IssueComment],
    *,
    max_chars: int = MAX_CURRENT_REVISION_CONTEXT_CHARS,
) -> str:
    """Return bounded current plan context while preserving its paired review.

    Superseded plan/review artifacts are deliberately excluded because they
    can contain stale critique. Stored archive comments remain unchanged.
    When space is constrained, preserve the current review in full whenever
    possible and spend the remaining budget on a deterministic plan excerpt.
    """
    if max_chars <= 0:
        return ""
    snapshot = journal_snapshot(comments)
    plan = (
        render_current_plan(
            snapshot.current_plan,
            revision=snapshot.revision,
            forced_planning_epoch=snapshot.forced_planning_epoch,
            recovery_source_digest=snapshot.recovery_source_digest,
        )
        if snapshot.current_plan
        else ""
    )
    review = (
        render_current_review(snapshot.current_review, revision=snapshot.revision)
        if snapshot.current_review
        and snapshot.current_review_revision is not None
        and snapshot.current_review_revision >= snapshot.revision
        else ""
    )
    if not plan:
        return _bounded_excerpt(review, max_chars)
    if not review:
        return _bounded_excerpt(plan, max_chars)

    plan_heading = "## Current rejected plan\n\n"
    review_heading = "## Current review\n\n"
    separator = "\n\n---\n\n"
    fixed_size = len(plan_heading) + len(review_heading) + len(separator) + len(review)
    if fixed_size >= max_chars:
        return _bounded_excerpt(
            f"{review_heading}{review}",
            max_chars,
        )
    return (
        f"{plan_heading}{_bounded_excerpt(plan, max_chars - fixed_size)}"
        f"{separator}{review_heading}{review}"
    )
