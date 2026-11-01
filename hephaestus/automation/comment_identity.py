"""Shared identity checks for marker-keyed GitHub comments."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import Final

from .markdown_markers import top_level_marker_occurrences
from .protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    comment_marker_aliases,
)

logger = logging.getLogger(__name__)


class CommentAliasConflictError(RuntimeError):
    """More than one comment or marker alias claims one automation role."""


_IMMUTABLE_HISTORY_ARTIFACT_RE: Final[re.Pattern[str]] = re.compile(
    r"^<!-- hephaestus-plan-history:revision=\d+:kind=(?:plan|review) -->(?=\r?\n|\Z)"
)


def is_immutable_history_artifact(body: str) -> bool:
    """Return whether a body starts with one valid immutable history marker."""
    return _IMMUTABLE_HISTORY_ARTIFACT_RE.match(body) is not None


def marker_aliases_in_body(body: str, aliases: Sequence[str]) -> tuple[str, ...]:
    """Return exact top-level aliases in document order, including repeats."""
    return top_level_marker_occurrences(body, aliases)


def has_marker_alias(body: str, aliases: Sequence[str]) -> bool:
    """Return whether *body* has at least one exact top-level role alias."""
    return bool(marker_aliases_in_body(body, aliases))


def is_planning_marker(marker: str) -> bool:
    """Return whether *marker* is a current or legacy planning-role marker."""
    return marker in (
        *comment_marker_aliases(PLAN_CANONICAL_MARKER),
        *comment_marker_aliases(PLAN_REVIEW_CANONICAL_MARKER),
    )


def is_current_planning_marker(marker: str) -> bool:
    """Return whether *marker* is a writable shared planning-role marker."""
    return marker in (PLAN_CANONICAL_MARKER, PLAN_REVIEW_CANONICAL_MARKER)


def validate_planning_comment_identities[T](
    comments: Sequence[T],
    *,
    body_of: Callable[[T], str],
    owned_of: Callable[[T], bool],
) -> None:
    """Reject a comment body that cannot carry a planning marker.

    The host keeps no second record of who wrote a planning comment. The
    newest comment that carries the marker is the plan, and a repeat is
    patched in place. Selection and ownership therefore do not raise here.

    Two conditions stay a refusal, because each one would corrupt the write
    that follows:

    - A planning marker inside an immutable history artifact. The artifact is
      append-only, so the host cannot patch it in place.
    - One body that carries both the plan and the review marker. A later write
      cannot tell which role it is updating.

    Args:
        comments: Candidate comments, or one outgoing body.
        body_of: Read the body of one candidate.
        owned_of: Report whether this actor wrote one candidate. Kept for
            callers that still pass it; it no longer gates a decision.

    Raises:
        CommentAliasConflictError: If a body cannot carry a clean marker.

    """
    del owned_of
    plan_aliases = comment_marker_aliases(PLAN_CANONICAL_MARKER)
    review_aliases = comment_marker_aliases(PLAN_REVIEW_CANONICAL_MARKER)

    for comment in comments:
        body = body_of(comment)
        plan_matches = marker_aliases_in_body(body, plan_aliases)
        review_matches = marker_aliases_in_body(body, review_aliases)
        if not plan_matches and not review_matches:
            continue
        if is_immutable_history_artifact(body):
            raise CommentAliasConflictError(
                "planning marker embedded in immutable history artifact; "
                "manual recovery is required"
            )
        if plan_matches and review_matches:
            raise CommentAliasConflictError(
                "one comment appears to carry both plan and review markers; "
                "manual recovery is required"
            )


def validate_planning_body_for_write(marker: str, body: str) -> None:
    """Reject an outgoing body that can corrupt a shared planning identity."""
    plan_aliases = comment_marker_aliases(PLAN_CANONICAL_MARKER)
    review_aliases = comment_marker_aliases(PLAN_REVIEW_CANONICAL_MARKER)
    plan_matches = marker_aliases_in_body(body, plan_aliases)
    review_matches = marker_aliases_in_body(body, review_aliases)
    if not is_planning_marker(marker):
        if plan_matches or review_matches:
            raise CommentAliasConflictError(
                "nonplanning comment body claims a planning marker; manual recovery is required"
            )
        return

    validate_planning_comment_identities(
        (body,),
        body_of=lambda candidate: candidate,
        owned_of=lambda _candidate: True,
    )
    expected_matches = plan_matches if marker == PLAN_CANONICAL_MARKER else review_matches
    if expected_matches != (marker,):
        raise CommentAliasConflictError(
            "outgoing planning body does not contain exactly its shared marker; "
            "manual recovery is required"
        )


def select_unambiguous_comment[T](
    candidates: Sequence[T],
    *,
    marker: str,
    aliases: Sequence[str],
    body_of: Callable[[T], str],
    ownership: str = "actor-owned",
) -> T | None:
    """Return the newest candidate that carries exactly one role marker.

    The newest comment is the pointer for its role, whatever author wrote it.
    A duplicate marker in an older comment is not an identity conflict: the
    newest one is patched, and no comment is deleted.

    Args:
        candidates: Comments that carried a marker, in GitHub order.
        marker: The canonical marker for this role.
        aliases: The canonical marker and its accepted legacy aliases.
        body_of: Read the body of one candidate.
        ownership: Word used in the warning text.

    Returns:
        The newest usable candidate, or None when no candidate carries one
        clean marker.

    """
    usable = [
        candidate
        for candidate in candidates
        if len(marker_aliases_in_body(body_of(candidate), aliases)) == 1
    ]
    if not usable:
        if candidates:
            logger.warning(
                "comment_identity: no %s comment carries exactly one %r marker; "
                "the next write creates a new one and patches it in place",
                ownership,
                marker,
            )
        return None
    if len(candidates) > 1:
        logger.warning(
            "comment_identity: %d %s comments carry the %r marker; using the newest "
            "and patching it in place",
            len(candidates),
            ownership,
            marker,
        )
    return usable[-1]
