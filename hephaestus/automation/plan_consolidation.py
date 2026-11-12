"""Pure planner and injectable executor for operator-authorized consolidation.

Two canonical plan/review pairs can coexist on one issue when two tools each
published one pair (the Comet #705 case). No code path selects a winner by
age, tool name, label, or a historical GO. The operator names the exact keep
and supersede comment IDs and supplies the merged plan/review content. This
module binds that request to the exact source records, forces a fresh plan
review, and removes the superseded comments only after their content survives
as fenced history archives.

Validation is two-phased:

- Pre-delete, the journal is read only through :func:`planning_role_inventory`
  (foreign claims, coverage, digest drift). The journal still holds the
  duplicate owned pairs at that point.
- Post-delete, the guarded role upsert re-runs the duplicate-rejecting
  validation on a journal that holds exactly one owned claim per role.

The executor is ordered for crash-safe idempotence: archives first, then
deletes, then the canonical-content upgrade, then the label transition. Every
write is verified by an exact read-back. A re-run converges: an
already-satisfied step is skipped, and a completed replay performs no
mutation.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from hephaestus.automation.comment_identity import marker_aliases_in_body
from hephaestus.automation.markdown_markers import raw_markdown_lines
from hephaestus.automation.protocol import (
    LEGACY_PLAN_CANONICAL_MARKERS,
    LEGACY_PLAN_REVIEW_CANONICAL_MARKERS,
    PLAN_CANONICAL_MARKER,
    PLAN_CANONICAL_MARKERS,
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKERS,
)
from hephaestus.automation.review_journal import (
    ARCHIVE_SOURCE_RE,
    CONSOLIDATION_PROVENANCE_PREFIX,
    HISTORY_MARKER,
    HISTORY_RE,
    MAX_PRIOR_PLAN_FINGERPRINTS,
    PLAN_FINGERPRINTS_RE,
    CommentJournalReadError,
    ConsolidationProvenance,
    ConsolidationSource,
    IssueComment,
    comment_revision,
    contains_raw_patch,
    extract_current_plan,
    extract_current_review,
    journal_snapshot,
    parse_consolidation_provenance,
    parse_consolidation_source_spec,
    parse_plan_review_state,
    plan_fingerprint,
    render_consolidation_provenance,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.state_labels import (
    STATE_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)

logger = logging.getLogger(__name__)

MAX_ARCHIVE_FENCE_RUN: Final[int] = 3
"""Shortest code fence that CommonMark accepts for a fenced code block."""

_CONSOLIDATION_ALIAS_TAGS: Final[dict[str, str]] = {
    PLAN_CANONICAL_MARKER: "shared",
    LEGACY_PLAN_CANONICAL_MARKERS[0]: "hephaestus",
    LEGACY_PLAN_CANONICAL_MARKERS[1]: "athena",
    PLAN_REVIEW_CANONICAL_MARKER: "shared",
    LEGACY_PLAN_REVIEW_CANONICAL_MARKERS[0]: "hephaestus",
    LEGACY_PLAN_REVIEW_CANONICAL_MARKERS[1]: "athena",
}
"""Stable alias tag for each accepted plan/review marker."""

_ARCHIVE_PROSE_TEMPLATE: Final[str] = (
    "Consolidation archive. Superseded {role} source: {spec}. "
    "This comment is inert history; the fenced payload preserves the exact "
    "source body."
)

_FENCE_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^(`{3,})$")


class ConsolidationError(RuntimeError):
    """Base class for every consolidation failure."""


class ConsolidationRequestError(ConsolidationError):
    """The operator request names comments that cannot satisfy the request."""


class ForeignPlanningClaimError(ConsolidationError):
    """A plan/review claim exists that the authenticated actor did not write."""


class UnverifiablePlanningClaimError(ConsolidationError):
    """One comment claims a planning role in a way that cannot be classified."""


class ConsolidationCoverageError(ConsolidationError):
    """An owned plan/review claim is not named by the operator request."""


class ConsolidationLatchError(ConsolidationError):
    """An operator-owned execution latch label forbids the consolidation."""


class ConsolidationStateTokenError(ConsolidationError):
    """The merged review content carries a terminal plan-review state token."""


class ConsolidationRawPatchError(ConsolidationError):
    """The merged plan content embeds unified-diff material."""


class ArchiveMarkerCollisionError(ConsolidationError):
    """Two consolidation sources would share one history archive marker."""


class ConsolidationDriftError(ConsolidationError):
    """Live GitHub state no longer matches the plan that was computed."""


class ConsolidationDeletionError(ConsolidationError):
    """A superseded comment delete did not confirm; source evidence is kept."""


class ConsolidationVerificationError(ConsolidationError):
    """The consolidated state did not converge after the writes."""


@dataclass(frozen=True, slots=True)
class PlanningRoleClaim:
    """One top-level plan or review claim read from the issue journal."""

    comment_id: int
    author_login: str
    role: str
    marker: str
    alias_tag: str
    revision: int
    digest: str
    owned: bool
    created_at: str
    updated_at: str


def alias_tag_for(role: str, marker: str) -> str:
    """Return the stable alias tag for one accepted plan/review marker."""
    tag = _CONSOLIDATION_ALIAS_TAGS.get(marker)
    if tag is None:
        raise ConsolidationError(f"marker {marker!r} is not an accepted {role} alias")
    return tag


def review_fingerprint(body: str) -> str:
    """Return a stable digest over normalized extracted review content."""
    normalized = "\n".join(
        line.rstrip() for line in extract_current_review(body).strip().splitlines()
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def issue_body_digest(issue_body: str) -> str:
    """Return the digest that binds the consolidation to the issue body."""
    return hashlib.sha256(issue_body.encode("utf-8")).hexdigest()


def planning_role_inventory(
    comments: Sequence[IssueComment],
) -> tuple[PlanningRoleClaim, ...]:
    """Enumerate every plan/review claim across all accepted aliases.

    The inventory reports claims as data. It raises only when one comment
    cannot be classified at all: a body that carries both role markers, or a
    claim whose comment id is unknown. It never selects a winner, and it never
    raises because one role is claimed more than once.

    Args:
        comments: Ownership-aware issue comments in GitHub order.

    Returns:
        One claim per marker-bearing comment, in GitHub order.

    Raises:
        UnverifiablePlanningClaimError: If a claim cannot be classified.

    """
    claims: list[PlanningRoleClaim] = []
    for index, comment in enumerate(comments):
        body = comment.body
        if HISTORY_RE.match(body) is not None:
            # A retired archive is history, not a current claim. Its payload
            # is fenced, so the marker scan below cannot see it anyway.
            continue
        plan_matches = marker_aliases_in_body(body, PLAN_CANONICAL_MARKERS)
        review_matches = marker_aliases_in_body(body, PLAN_REVIEW_CANONICAL_MARKERS)
        if plan_matches and review_matches:
            raise UnverifiablePlanningClaimError(
                f"comment {index} carries both plan and review markers; manual recovery is required"
            )
        if plan_matches:
            role, marker = "plan", plan_matches[0]
        elif review_matches:
            role, marker = "review", review_matches[0]
        else:
            continue
        if comment.database_id is None:
            raise UnverifiablePlanningClaimError(
                f"comment {index} claims the {role} role without a comment id; "
                "manual recovery is required"
            )
        digest = plan_fingerprint(body) if role == "plan" else review_fingerprint(body)
        claims.append(
            PlanningRoleClaim(
                comment_id=comment.database_id,
                author_login=comment.author_login,
                role=role,
                marker=marker,
                alias_tag=alias_tag_for(role, marker),
                revision=comment_revision(body) or 1,
                digest=digest,
                owned=comment.viewer_did_author,
                created_at=comment.created_at,
                updated_at=comment.updated_at,
            )
        )
    return tuple(claims)


def _fence_length_for(body: str) -> int:
    """Return a fence length that no line of *body* can close early."""
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", body)), default=0)
    return max(MAX_ARCHIVE_FENCE_RUN, longest + 1)


def render_consolidation_archive(claim: PlanningRoleClaim, body: str) -> str:
    """Render the immutable history archive that preserves one source body.

    The archive marker carries the superseded comment's own revision, so
    archive identity denotes the retired source and not the new epoch. The
    source body is wrapped in a code fence that no line of the body can close,
    so the embedded planning markers stay invisible to marker readers.

    Args:
        claim: The inventory claim of the superseded source comment.
        body: The exact original comment body.

    Returns:
        The complete archive comment body.

    """
    fence = "`" * _fence_length_for(body)
    prose = _ARCHIVE_PROSE_TEMPLATE.format(role=claim.role, spec=_claim_spec(claim))
    return (
        f"{HISTORY_MARKER.format(revision=claim.revision, kind=claim.role)}\n"
        f"{prose}\n\n"
        f"{fence}\n{body}\n{fence}"
    )


def _claim_spec(claim: PlanningRoleClaim) -> str:
    """Return the pipe-delimited source record for one claim."""
    return f"{claim.role}|{claim.comment_id}|{claim.author_login}|{claim.alias_tag}|{claim.digest}"


def extract_consolidation_archive(body: str) -> str | None:
    """Return the exact archived source body, or None for any other body.

    The inverse of :func:`render_consolidation_archive`: the wrapper adds one
    line feed after the payload, so extraction removes exactly one trailing
    line feed to recover the source body byte for byte.
    """
    if HISTORY_RE.match(body) is None:
        return None
    lines = raw_markdown_lines(body)
    if not lines:
        return None
    open_index: int | None = None
    open_length = 0
    for index, line in enumerate(lines[1:], start=1):
        match = _FENCE_LINE_RE.match(line.rstrip("\r\n"))
        if match is not None:
            open_index = index
            open_length = len(match.group(1))
            break
    if open_index is None:
        return None
    close_index: int | None = None
    for index in range(len(lines) - 1, open_index, -1):
        match = _FENCE_LINE_RE.match(lines[index].rstrip("\r\n"))
        if match is not None and len(match.group(1)) >= open_length:
            close_index = index
            break
    if close_index is None:
        return None
    payload = "".join(lines[open_index + 1 : close_index])
    if payload.endswith("\n"):
        payload = payload[:-1]
    return payload


def archived_source_record(comment: IssueComment) -> tuple[ConsolidationSource, int] | None:
    """Return the bound source record and revision of one archive comment.

    Returns None for archive comments that a consolidation did not write, for
    example the plain compaction archives.
    """
    marker_match = HISTORY_RE.match(comment.body)
    if marker_match is None:
        return None
    spec_match = ARCHIVE_SOURCE_RE.search(comment.body)
    if spec_match is None:
        return None
    source = parse_consolidation_source_spec(spec_match.group("spec"))
    if source is None:
        return None
    return source, int(marker_match.group("revision"))


@dataclass(frozen=True, slots=True)
class ConsolidationRequest:
    """One operator-authorized consolidation request.

    The operator authors every field: the keep and supersede comment IDs, and
    the merged plan/review content. No code path derives any of them.
    """

    issue_number: int
    issue_owner: str
    issue_name: str
    keep_plan_comment_id: int
    keep_review_comment_id: int
    supersede_plan_comment_ids: tuple[int, ...] = ()
    supersede_review_comment_ids: tuple[int, ...] = ()
    plan_content: str = ""
    review_content: str = ""


@dataclass(frozen=True, slots=True)
class ConsolidationArchive:
    """One desired history archive for a superseded source comment."""

    claim: PlanningRoleClaim
    body: str

    @property
    def marker(self) -> str:
        """Return the history marker line that opens this archive."""
        return HISTORY_MARKER.format(revision=self.claim.revision, kind=self.claim.role)


@dataclass(frozen=True, slots=True)
class ConsolidationPlan:
    """The complete desired consolidation state for one issue."""

    request: ConsolidationRequest
    keep_plan_claim: PlanningRoleClaim
    keep_review_claim: PlanningRoleClaim
    superseded_claims: tuple[PlanningRoleClaim, ...]
    sources: tuple[ConsolidationSource, ...]
    epoch: int
    requirements_digest: str
    provenance_line: str
    keep_plan_body: str
    keep_review_body: str
    archives: tuple[ConsolidationArchive, ...]
    deletions: tuple[int, ...]
    label_add: tuple[str, ...]
    label_remove: tuple[str, ...]

    @property
    def requires_label_transition(self) -> bool:
        """Return whether the plan-state label must move off a historical GO."""
        return bool(self.label_add or self.label_remove)


def plan_consolidation(
    comments: Sequence[IssueComment],
    issue_body: str,
    labels: Sequence[str],
    request: ConsolidationRequest,
) -> ConsolidationPlan:
    """Compute the complete desired consolidation state for one issue.

    The planner is pure: it mutates nothing and calls nothing. It raises a
    typed :class:`ConsolidationError` subclass for every unsafe request, so a
    dry run proves the same gates the apply run enforces.

    Args:
        comments: Ownership-aware issue comments in GitHub order.
        issue_body: The current issue body text.
        labels: The current issue label names.
        request: The operator-authorized request.

    Returns:
        The complete desired state: desired archives, deletions, canonical
        bodies, and the label transition.

    Raises:
        ConsolidationRequestError: If the request is internally inconsistent.
        UnverifiablePlanningClaimError: If a claim cannot be classified.
        ForeignPlanningClaimError: If a plan/review claim is not actor-owned.
        ConsolidationLatchError: If an operator latch label is present.
        ConsolidationCoverageError: If an owned claim is not named.
        ConsolidationRawPatchError: If the merged plan embeds diff material.
        ConsolidationStateTokenError: If the merged review carries a terminal
            plan-review state token.
        ArchiveMarkerCollisionError: If two sources would share one archive
            marker.

    """
    inventory = planning_role_inventory(comments)
    _require_actor_owned(inventory)
    _require_no_latch(labels)
    keep_plan = _resolve_keep_claim(inventory, request, "plan")
    keep_review = _resolve_keep_claim(inventory, request, "review")
    superseded, present_ids = _resolve_superseded_claims(inventory, comments, request)
    covered_ids = frozenset(
        {
            keep_plan.comment_id,
            keep_review.comment_id,
            *(claim.comment_id for claim in superseded),
        }
    )
    _require_coverage(inventory, covered_ids)
    _require_writable_content(request)

    requirements_digest = issue_body_digest(issue_body)
    epoch, provenance_line, sources, fingerprints = _resolve_binding(
        request, comments, keep_plan, keep_review, superseded, requirements_digest
    )
    keep_plan_body = render_current_plan(
        request.plan_content,
        revision=epoch,
        prior_fingerprints=fingerprints,
        consolidation_provenance=provenance_line,
    )
    keep_review_body = render_current_review(request.review_content, revision=epoch)
    archives = _desired_archives(comments, superseded)
    label_add, label_remove = _label_transition(labels)
    return ConsolidationPlan(
        request=request,
        keep_plan_claim=keep_plan,
        keep_review_claim=keep_review,
        superseded_claims=superseded,
        sources=sources,
        epoch=epoch,
        requirements_digest=requirements_digest,
        provenance_line=provenance_line,
        keep_plan_body=keep_plan_body,
        keep_review_body=keep_review_body,
        archives=archives,
        deletions=tuple(
            claim.comment_id for claim in superseded if claim.comment_id in present_ids
        ),
        label_add=label_add,
        label_remove=label_remove,
    )


def _require_actor_owned(inventory: Sequence[PlanningRoleClaim]) -> None:
    """Abort when a plan/review claim is not owned by the actor."""
    foreign = [claim for claim in inventory if not claim.owned]
    if foreign:
        named = ", ".join(str(claim.comment_id) for claim in foreign)
        raise ForeignPlanningClaimError(
            "plan/review claims exist that the authenticated actor did not write "
            f"({named}); explicit authority is required before consolidation"
        )


def _require_no_latch(labels: Sequence[str]) -> None:
    """Abort while an operator-owned execution latch label is present."""
    latches = [label for label in labels if label in (STATE_BLOCKED, STATE_SKIP)]
    if latches:
        raise ConsolidationLatchError(
            f"operator latch label {latches[0]!r} is present; remove the latch before consolidation"
        )


def _resolve_keep_claim(
    inventory: Sequence[PlanningRoleClaim],
    request: ConsolidationRequest,
    role: str,
) -> PlanningRoleClaim:
    """Resolve one keep comment ID to an owned claim of the same role."""
    keep_id = request.keep_plan_comment_id if role == "plan" else request.keep_review_comment_id
    for claim in inventory:
        if claim.comment_id == keep_id:
            if claim.role != role:
                raise ConsolidationRequestError(
                    f"keep comment {keep_id} carries the {claim.role} role, not {role}"
                )
            if not claim.owned:
                raise ConsolidationRequestError(
                    f"keep comment {keep_id} is not owned by the authenticated actor"
                )
            return claim
    raise ConsolidationRequestError(
        f"keep comment {keep_id} does not carry an owned {role} claim on the issue"
    )


def _resolve_superseded_claims(
    inventory: Sequence[PlanningRoleClaim],
    comments: Sequence[IssueComment],
    request: ConsolidationRequest,
) -> tuple[tuple[PlanningRoleClaim, ...], frozenset[int]]:
    """Resolve the supersede IDs to claims in request order.

    A requested supersede ID that no longer carries a live comment must
    already have its history archive, so a partial re-run names the same
    comments as the first run and stays byte-identical.

    Returns:
        The superseded claims in request order, and the IDs of the claims
        that still carry a live comment.

    """
    by_id = {claim.comment_id: claim for claim in inventory}
    resolved: list[PlanningRoleClaim] = []
    present: set[int] = set()
    for role, requested in (
        ("plan", request.supersede_plan_comment_ids),
        ("review", request.supersede_review_comment_ids),
    ):
        for comment_id in requested:
            claim = by_id.get(comment_id)
            if claim is not None:
                if claim.role != role:
                    raise ConsolidationRequestError(
                        f"supersede comment {comment_id} carries the {claim.role} role, not {role}"
                    )
                if not claim.owned:
                    raise ConsolidationRequestError(
                        f"supersede comment {comment_id} is not owned by the authenticated actor"
                    )
                resolved.append(claim)
                present.add(comment_id)
                continue
            resolved.append(_archived_claim(comments, comment_id, role))
    return tuple(resolved), frozenset(present)


def _archived_claim(
    comments: Sequence[IssueComment],
    comment_id: int,
    role: str,
) -> PlanningRoleClaim:
    """Rebuild one superseded claim from its durable history archive."""
    for comment in comments:
        record = archived_source_record(comment)
        if record is None or record[0].comment_id != comment_id:
            continue
        source, revision = record
        if source.role != role:
            raise ConsolidationRequestError(
                f"supersede comment {comment_id} is archived as a {source.role} source, not {role}"
            )
        return PlanningRoleClaim(
            comment_id=comment_id,
            author_login=source.login,
            role=source.role,
            marker="",
            alias_tag=source.alias_tag,
            revision=revision,
            digest=source.digest,
            owned=True,
            created_at="",
            updated_at="",
        )
    raise ConsolidationRequestError(
        f"supersede comment {comment_id} is absent from the issue without a "
        "history archive; it cannot be confirmed as superseded"
    )


def _require_coverage(
    inventory: Sequence[PlanningRoleClaim],
    covered_ids: frozenset[int],
) -> None:
    """Abort when an owned claim is not named by the request."""
    unnamed = [claim for claim in inventory if claim.owned and claim.comment_id not in covered_ids]
    if unnamed:
        named = ", ".join(str(claim.comment_id) for claim in unnamed)
        raise ConsolidationCoverageError(
            f"owned plan/review claims are not named by the request ({named}); "
            "name every duplicate as keep or supersede"
        )


def _require_writable_content(request: ConsolidationRequest) -> None:
    """Abort on inconsistent IDs or unwritable merged content."""
    keep_ids = {request.keep_plan_comment_id, request.keep_review_comment_id}
    supersede_ids = {
        *request.supersede_plan_comment_ids,
        *request.supersede_review_comment_ids,
    }
    if request.keep_plan_comment_id == request.keep_review_comment_id:
        raise ConsolidationRequestError(
            "one comment cannot carry both the plan and the review role"
        )
    if keep_ids & supersede_ids:
        raise ConsolidationRequestError(
            "a comment cannot be both keep and supersede in one request"
        )
    if not request.plan_content.strip() or not request.review_content.strip():
        raise ConsolidationRequestError("merged plan and review content are required")
    if contains_raw_patch(request.plan_content):
        raise ConsolidationRawPatchError(
            "merged plan content embeds unified-diff material; remove the patch "
            "and describe the change instead"
        )
    if parse_plan_review_state(extract_current_review(request.review_content)) is not None:
        raise ConsolidationStateTokenError(
            "merged review content carries a terminal plan-review state token; "
            "the consolidated epoch must force a fresh independent review"
        )


def _bound_sources(
    keep_plan: PlanningRoleClaim,
    keep_review: PlanningRoleClaim,
    superseded: Sequence[PlanningRoleClaim],
) -> tuple[ConsolidationSource, ...]:
    """Return every bound source record in stable request order."""
    ordered = (keep_plan, *superseded, keep_review)
    return tuple(_claim_source(claim) for claim in ordered)


def _claim_source(claim: PlanningRoleClaim) -> ConsolidationSource:
    """Return the provenance source record for one claim."""
    return ConsolidationSource(
        role=claim.role,
        comment_id=claim.comment_id,
        login=claim.author_login,
        alias_tag=claim.alias_tag,
        digest=claim.digest,
    )


def _resolve_binding(
    request: ConsolidationRequest,
    comments: Sequence[IssueComment],
    keep_plan: PlanningRoleClaim,
    keep_review: PlanningRoleClaim,
    superseded: Sequence[PlanningRoleClaim],
    requirements_digest: str,
) -> tuple[int, str, tuple[ConsolidationSource, ...], tuple[str, ...]]:
    """Return the epoch, provenance line, sources, and fingerprints.

    The provenance header that a completed consolidation wrote is the
    authority for a replay of that consolidation: when it binds the same
    issue, the same superseded records, the same keep comment IDs, the same
    merged content, and the same issue requirements, its line, epoch, and the
    live fingerprint line are reused byte for byte. Otherwise a fresh binding
    is rendered one epoch past the highest source revision, so the GO from a
    previous epoch is never reapplied.
    """
    fresh_sources = _bound_sources(keep_plan, keep_review, superseded)
    current = next(
        (comment for comment in comments if comment.database_id == keep_plan.comment_id),
        None,
    )
    if current is not None:
        for line in current.body.splitlines():
            stripped = line.strip()
            if not stripped.startswith(CONSOLIDATION_PROVENANCE_PREFIX):
                continue
            stored = parse_consolidation_provenance(stripped)
            if stored is None:
                break
            if (
                stored.issue_owner == request.issue_owner
                and stored.issue_name == request.issue_name
                and stored.issue_number == request.issue_number
                and stored.requirements_digest == requirements_digest
                and _stored_binding_matches(
                    stored, comments, request, keep_plan, keep_review, superseded
                )
            ):
                fingerprints = _live_fingerprints(current.body, superseded)
                return stored.epoch, stripped, stored.sources, fingerprints
            break
    epoch = max(claim.revision for claim in (keep_plan, keep_review, *superseded)) + 1
    provenance_line = render_consolidation_provenance(
        issue_owner=request.issue_owner,
        issue_name=request.issue_name,
        issue_number=request.issue_number,
        requirements_digest=requirements_digest,
        epoch=epoch,
        sources=fresh_sources,
    )
    return (
        epoch,
        provenance_line,
        fresh_sources,
        _desired_fingerprints(request, keep_plan, superseded),
    )


def _stored_binding_matches(
    stored: ConsolidationProvenance,
    comments: Sequence[IssueComment],
    request: ConsolidationRequest,
    keep_plan: PlanningRoleClaim,
    keep_review: PlanningRoleClaim,
    superseded: Sequence[PlanningRoleClaim],
) -> bool:
    """Return whether one stored provenance header replays this request.

    The consolidation replaced both keep contents, so their pre-merge digests
    live only in the stored header. A replay must pin the keep records by
    comment ID, and must bind exactly the merged content that is live on the
    issue now; changed merged content is a new consolidation and earns a new
    epoch with a fresh review.
    """
    if len(stored.sources) != len(superseded) + 2:
        return False
    keep_plan_record, *middle, keep_review_record = stored.sources
    if (
        keep_plan_record.role != "plan"
        or keep_plan_record.comment_id != request.keep_plan_comment_id
    ):
        return False
    if (
        keep_review_record.role != "review"
        or keep_review_record.comment_id != request.keep_review_comment_id
    ):
        return False
    if tuple(middle) != tuple(_claim_source(claim) for claim in superseded):
        return False
    live_plan = next(
        (comment for comment in comments if comment.database_id == request.keep_plan_comment_id),
        None,
    )
    if live_plan is None:
        return False
    if extract_current_plan(live_plan.body) != extract_current_plan(request.plan_content):
        return False
    live_review = next(
        (comment for comment in comments if comment.database_id == request.keep_review_comment_id),
        None,
    )
    if live_review is None:
        return False
    return extract_current_review(live_review.body) == extract_current_review(
        request.review_content
    )


def _desired_fingerprints(
    request: ConsolidationRequest,
    keep_plan: PlanningRoleClaim,
    superseded: Sequence[PlanningRoleClaim],
) -> tuple[str, ...]:
    """Return the fingerprint line the consolidated plan body must carry.

    Hidden fingerprints bound the merged content to its sources: the
    operator-authored fingerprint line is unioned with every bound source
    plan's digest. A source plan digest that the fingerprint budget would drop
    fails closed, because the provenance then cannot prove what was merged.
    """
    operator_fingerprints = _parse_fingerprints(request.plan_content)
    source_fingerprints = tuple(
        claim.digest for claim in (keep_plan, *superseded) if claim.role == "plan"
    )
    merged = tuple(dict.fromkeys((*operator_fingerprints, *source_fingerprints)))
    budget = merged[-MAX_PRIOR_PLAN_FINGERPRINTS:]
    dropped = tuple(digest for digest in source_fingerprints if digest not in budget)
    if dropped:
        raise ConsolidationError(
            "source plan fingerprints exceeded the hidden fingerprint budget; "
            "trim the merged plan's fingerprint line instead"
        )
    return budget


def _live_fingerprints(
    body: str,
    superseded: Sequence[PlanningRoleClaim],
) -> tuple[str, ...]:
    """Return the fingerprint line a completed consolidation wrote.

    The live keep-plan body is the authority for a completed consolidation,
    so its own fingerprint line is reused byte for byte. A superseded source
    digest that the line no longer carries fails closed, because the body
    then no longer proves what was merged.
    """
    fingerprints: tuple[str, ...] = ()
    for line in body.splitlines():
        match = PLAN_FINGERPRINTS_RE.fullmatch(line.strip())
        if match is not None:
            fingerprints = tuple(value for value in match.group("fingerprints").split(",") if value)
            break
    missing = tuple(
        claim.digest
        for claim in superseded
        if claim.role == "plan" and claim.digest not in fingerprints
    )
    if missing:
        raise ConsolidationError(
            "the completed keep-plan body no longer carries every superseded "
            "source plan fingerprint; manual recovery is required"
        )
    return fingerprints


def _desired_archives(
    comments: Sequence[IssueComment],
    superseded: Sequence[PlanningRoleClaim],
) -> tuple[ConsolidationArchive, ...]:
    """Render the desired history archives for the superseded sources.

    Two same-kind sources that share one revision share one archive marker, so
    the planner aborts up front instead of reaching an exact-replay conflict.
    An already-archived source must reproduce its own archive body byte for
    byte; a hand-edited archive fails closed instead of publishing a record
    that does not denote its source.
    """
    archives: list[ConsolidationArchive] = []
    seen_markers: dict[str, str] = {}
    for claim in superseded:
        live = next(
            (comment for comment in comments if comment.database_id == claim.comment_id),
            None,
        )
        if live is not None:
            body = render_consolidation_archive(claim, live.body)
        else:
            archived = _archived_comment(comments, claim.comment_id)
            payload = extract_consolidation_archive(archived.body)
            if payload is None:
                raise ArchiveMarkerCollisionError(
                    f"history archive for comment {claim.comment_id} does not "
                    "reproduce its source body"
                )
            body = render_consolidation_archive(claim, payload)
            if body != archived.body:
                raise ArchiveMarkerCollisionError(
                    f"history archive for comment {claim.comment_id} does not "
                    "reproduce its own source record"
                )
        marker = HISTORY_MARKER.format(revision=claim.revision, kind=claim.role)
        collision = seen_markers.get(marker)
        if collision is not None and collision != body:
            raise ArchiveMarkerCollisionError(
                f"two superseded {claim.role} sources share revision "
                f"{claim.revision}; one archive marker cannot denote both"
            )
        seen_markers[marker] = body
        archives.append(ConsolidationArchive(claim=claim, body=body))
    for comment in comments:
        first_line = comment.body.partition("\n")[0]
        if first_line in seen_markers and seen_markers[first_line] != comment.body:
            raise ArchiveMarkerCollisionError(
                "the issue journal already stores a different history archive "
                f"for marker {first_line}"
            )
    return tuple(archives)


def _archived_comment(comments: Sequence[IssueComment], comment_id: int) -> IssueComment:
    """Return the history archive that binds one superseded source comment."""
    archived = next(
        (
            comment
            for comment in comments
            if (record := archived_source_record(comment)) is not None
            and record[0].comment_id == comment_id
        ),
        None,
    )
    if archived is None:
        raise ConsolidationDriftError(
            f"superseded comment {comment_id} carries neither a live comment "
            "nor its history archive"
        )
    return archived


def _parse_fingerprints(body: str) -> tuple[str, ...]:
    """Parse the hidden fingerprint line of one operator-authored plan."""
    match = PLAN_FINGERPRINTS_RE.search(body)
    if match is None:
        return ()
    return tuple(value for value in match.group("fingerprints").split(",") if value)


def _label_transition(labels: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the label transition that moves a historical GO off the issue."""
    if STATE_PLAN_GO in labels:
        return (STATE_PLAN_NO_GO,), (STATE_PLAN_GO,)
    return (), ()


@dataclass(frozen=True, slots=True)
class ConsolidationOutcome:
    """What one consolidation run changed, or already found satisfied."""

    already_consolidated: bool
    epoch: int
    appended_archives: int
    deleted_comment_ids: tuple[int, ...]
    labels_edited: bool


def execute_consolidation(
    plan: ConsolidationPlan,
    *,
    fetch_comments: Callable[[], Sequence[IssueComment]],
    fetch_labels: Callable[[], Sequence[str]],
    fetch_issue_body: Callable[[], str],
    append_comment: Callable[[str], None],
    delete_comment: Callable[[int], None],
    upsert_owned_comment: Callable[[str, str], int | None],
    edit_labels: Callable[[Sequence[str], Sequence[str]], None],
) -> ConsolidationOutcome:
    """Execute one consolidation plan against injectable GitHub seams.

    The executor is ordered for crash-safe idempotence and verifies every
    write by an exact read-back:

    1. Re-validate the live journal, issue body, and labels against the plan.
    2. Report an already-consolidated issue without mutating anything.
    3. Append every history archive with a read-back.
    4. Re-verify every source digest, ownership claim, and the issue body.
    5. Delete each superseded comment with a read-back.
    6. Upsert the canonical plan and review comments in place.
    7. Move a historical GO off the issue.
    8. Read back the final consolidated state.

    Args:
        plan: The plan computed by :func:`plan_consolidation`.
        fetch_comments: Read the complete ownership-aware journal.
        fetch_labels: Read the current issue label names.
        fetch_issue_body: Read the current issue body.
        append_comment: Append one immutable comment.
        delete_comment: Delete one comment by id, tolerating an already
            deleted comment.
        upsert_owned_comment: Patch the actor's newest comment for one role
            marker, or create it. The seam may return the canonical comment
            id; the executor ignores the return value.
        edit_labels: Apply one label add/remove transition.

    Returns:
        The outcome of the run.

    Raises:
        ConsolidationDriftError: If live state no longer matches the plan.
        ArchiveMarkerCollisionError: If an archive record cannot be written
            unambiguously.
        ConsolidationDeletionError: If a delete does not confirm.
        ConsolidationVerificationError: If the final state does not converge.

    """
    _validate_live_state(plan, fetch_comments(), fetch_labels(), fetch_issue_body())
    comments = list(fetch_comments())
    labels = tuple(fetch_labels())
    if _consolidation_satisfied(plan, comments, labels):
        return ConsolidationOutcome(
            already_consolidated=True,
            epoch=plan.epoch,
            appended_archives=0,
            deleted_comment_ids=(),
            labels_edited=False,
        )
    appended = _append_archives(plan, comments, append_comment, fetch_comments)
    _validate_live_state(plan, fetch_comments(), fetch_labels(), fetch_issue_body())
    deleted = _delete_superseded(plan, delete_comment, fetch_comments)
    upsert_owned_comment(PLAN_CANONICAL_MARKER, plan.keep_plan_body)
    upsert_owned_comment(PLAN_REVIEW_CANONICAL_MARKER, plan.keep_review_body)
    labels_edited = _transition_labels(plan, edit_labels, fetch_labels)
    _verify_final_state(plan, list(fetch_comments()), tuple(fetch_labels()))
    return ConsolidationOutcome(
        already_consolidated=False,
        epoch=plan.epoch,
        appended_archives=appended,
        deleted_comment_ids=deleted,
        labels_edited=labels_edited,
    )


def _first_line(body: str) -> str:
    """Return the first raw line of one comment body."""
    return body.partition("\n")[0]


def _archive_comments(comments: Sequence[IssueComment], marker: str) -> list[IssueComment]:
    """Return the comments whose first raw line is one archive marker."""
    return [comment for comment in comments if _first_line(comment.body) == marker]


def _validate_live_state(
    plan: ConsolidationPlan,
    comments: Sequence[IssueComment],
    labels: Sequence[str],
    issue_body: str,
) -> None:
    """Re-verify the plan against live state through the pure inventory.

    The pre-delete journal still holds the duplicate owned pairs, so this
    check never runs the duplicate-rejecting validator. It reads claims
    through :func:`planning_role_inventory` only.

    Raises:
        ForeignPlanningClaimError: If a plan/review claim is not actor-owned.
        ConsolidationLatchError: If an operator latch label is present.
        ConsolidationDriftError: If any bound value changed.

    """
    inventory = planning_role_inventory(comments)
    _require_actor_owned(inventory)
    _require_no_latch(labels)
    if issue_body_digest(issue_body) != plan.requirements_digest:
        raise ConsolidationDriftError(
            "the issue requirements changed before the consolidation writes; recompute the plan"
        )
    by_id = {claim.comment_id: claim for claim in inventory}
    covered = {
        plan.request.keep_plan_comment_id,
        plan.request.keep_review_comment_id,
        *(claim.comment_id for claim in plan.superseded_claims),
    }
    for claim in inventory:
        if claim.owned and claim.comment_id not in covered:
            raise ConsolidationDriftError(
                f"owned {claim.role} claim {claim.comment_id} appeared before the "
                "consolidation writes; recompute the plan"
            )
    for claim in (plan.keep_plan_claim, plan.keep_review_claim):
        live = by_id.get(claim.comment_id)
        if live is None or live.digest != claim.digest or not live.owned:
            raise ConsolidationDriftError(
                f"keep comment {claim.comment_id} changed before the "
                "consolidation writes; recompute the plan"
            )
    for claim in plan.superseded_claims:
        live = by_id.get(claim.comment_id)
        if live is None:
            continue
        if live.digest != claim.digest or not live.owned:
            raise ConsolidationDriftError(
                f"supersede comment {claim.comment_id} changed before the "
                "consolidation writes; recompute the plan"
            )


def _consolidation_satisfied(
    plan: ConsolidationPlan,
    comments: Sequence[IssueComment],
    labels: Sequence[str],
) -> bool:
    """Return whether every planned step is already durable."""
    for archive in plan.archives:
        matching = _archive_comments(comments, archive.marker)
        if len(matching) != 1 or matching[0].body != archive.body:
            return False
    for comment_id in plan.deletions:
        if any(comment.database_id == comment_id for comment in comments):
            return False
    keep_plan = next(
        (
            comment
            for comment in comments
            if comment.database_id == plan.request.keep_plan_comment_id
        ),
        None,
    )
    keep_review = next(
        (
            comment
            for comment in comments
            if comment.database_id == plan.request.keep_review_comment_id
        ),
        None,
    )
    if keep_plan is None or keep_plan.body != plan.keep_plan_body:
        return False
    if keep_review is None or keep_review.body != plan.keep_review_body:
        return False
    return not (
        plan.requires_label_transition
        and (STATE_PLAN_GO in labels or STATE_PLAN_NO_GO not in labels)
    )


def _append_archives(
    plan: ConsolidationPlan,
    comments: Sequence[IssueComment],
    append_comment: Callable[[str], None],
    fetch_comments: Callable[[], Sequence[IssueComment]],
) -> int:
    """Append every missing history archive with an exact read-back."""
    appended = 0
    for archive in plan.archives:
        matching = _archive_comments(comments, archive.marker)
        if matching:
            if len(matching) != 1 or matching[0].body != archive.body:
                raise ArchiveMarkerCollisionError(
                    f"history archive marker {archive.marker!r} already names a "
                    "different record; manual recovery is required"
                )
            continue
        append_comment(archive.body)
        appended += 1
        matching = _archive_comments(list(fetch_comments()), archive.marker)
        if len(matching) != 1 or matching[0].body != archive.body:
            raise ConsolidationVerificationError(
                f"archive for comment {archive.claim.comment_id} did not read back"
            )
        comments = list(fetch_comments())
    return appended


def _delete_superseded(
    plan: ConsolidationPlan,
    delete_comment: Callable[[int], None],
    fetch_comments: Callable[[], Sequence[IssueComment]],
) -> tuple[int, ...]:
    """Delete each superseded comment and confirm every delete by a re-read."""
    deleted: list[int] = []
    for comment_id in plan.deletions:
        comments = list(fetch_comments())
        if not any(comment.database_id == comment_id for comment in comments):
            continue
        remaining = [
            pending
            for pending in plan.deletions
            if pending not in deleted
            and any(comment.database_id == pending for comment in comments)
        ]
        try:
            delete_comment(comment_id)
        except Exception as error:
            raise ConsolidationDeletionError(
                f"delete of comment {comment_id} did not confirm; superseded "
                f"comments still to confirm: {remaining}"
            ) from error
        comments = list(fetch_comments())
        if any(comment.database_id == comment_id for comment in comments):
            raise ConsolidationDeletionError(
                f"delete of comment {comment_id} did not confirm; superseded "
                f"comments still to confirm: {remaining}"
            )
        deleted.append(comment_id)
    return tuple(deleted)


def _transition_labels(
    plan: ConsolidationPlan,
    edit_labels: Callable[[Sequence[str], Sequence[str]], None],
    fetch_labels: Callable[[], Sequence[str]],
) -> bool:
    """Move a historical GO off the issue and confirm the transition."""
    if not plan.requires_label_transition:
        return False
    labels = tuple(fetch_labels())
    if STATE_PLAN_GO not in labels and STATE_PLAN_NO_GO in labels:
        return False
    edit_labels(plan.label_add, plan.label_remove)
    labels = tuple(fetch_labels())
    if STATE_PLAN_GO in labels or STATE_PLAN_NO_GO not in labels:
        raise ConsolidationVerificationError("the plan-state label transition did not confirm")
    return True


def _verify_final_state(
    plan: ConsolidationPlan,
    comments: Sequence[IssueComment],
    labels: Sequence[str],
) -> None:
    """Require the exact consolidated state after every write."""
    inventory = planning_role_inventory(comments)
    owned_plans = [claim for claim in inventory if claim.owned and claim.role == "plan"]
    owned_reviews = [claim for claim in inventory if claim.owned and claim.role == "review"]
    if len(owned_plans) != 1 or owned_plans[0].comment_id != plan.request.keep_plan_comment_id:
        raise ConsolidationVerificationError(
            "exactly one owned plan claim must remain after consolidation"
        )
    if len(owned_reviews) > 1 or (
        owned_reviews and owned_reviews[0].comment_id != plan.request.keep_review_comment_id
    ):
        raise ConsolidationVerificationError(
            "at most one owned review claim may remain after consolidation"
        )
    keep_plan = next(
        (
            comment
            for comment in comments
            if comment.database_id == plan.request.keep_plan_comment_id
        ),
        None,
    )
    keep_review = next(
        (
            comment
            for comment in comments
            if comment.database_id == plan.request.keep_review_comment_id
        ),
        None,
    )
    if keep_plan is None or keep_plan.body != plan.keep_plan_body:
        raise ConsolidationVerificationError(
            "the consolidated plan comment did not read back exactly"
        )
    if keep_review is None or keep_review.body != plan.keep_review_body:
        raise ConsolidationVerificationError(
            "the consolidated review comment did not read back exactly"
        )
    for archive in plan.archives:
        matching = _archive_comments(comments, archive.marker)
        if len(matching) != 1 or matching[0].body != archive.body:
            raise ConsolidationVerificationError(
                f"archive for comment {archive.claim.comment_id} did not read back"
            )
    try:
        snapshot = journal_snapshot(comments)
    except CommentJournalReadError as error:
        raise ConsolidationVerificationError(
            f"the consolidated journal did not read back: {error}"
        ) from error
    if snapshot.consolidation_provenance != plan.provenance_line:
        raise ConsolidationVerificationError(
            "the consolidation provenance header did not read back"
        )
    if snapshot.revision != plan.epoch:
        raise ConsolidationVerificationError("the consolidated epoch did not read back")
    if parse_plan_review_state(snapshot.current_review) is not None:
        raise ConsolidationVerificationError(
            "the consolidated review still carries a terminal state token"
        )
    if plan.requires_label_transition and (
        STATE_PLAN_GO in labels or STATE_PLAN_NO_GO not in labels
    ):
        raise ConsolidationVerificationError("the plan-state label transition did not read back")
