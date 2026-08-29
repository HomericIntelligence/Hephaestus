"""Shared identity checks for marker-keyed GitHub comments."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Final

from .markdown_markers import top_level_marker_occurrences
from .protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    comment_marker_aliases,
)


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
    """Reject unsafe plan and review identities before a planning operation.

    The shared planning protocol requires one verified actor-owned comment for
    each role. A foreign, unverifiable, repeated, or cross-role marker is an
    identity conflict. Other marker families remain outside this check.
    """
    plan_marker = PLAN_CANONICAL_MARKER
    review_marker = PLAN_REVIEW_CANONICAL_MARKER
    plan_aliases = comment_marker_aliases(plan_marker)
    review_aliases = comment_marker_aliases(review_marker)
    plan_candidates: list[T] = []
    review_candidates: list[T] = []

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
        if not owned_of(comment):
            raise CommentAliasConflictError(
                "foreign or unverifiable planning marker identity; manual recovery is required"
            )
        if plan_matches and review_matches:
            raise CommentAliasConflictError(
                "one actor-owned comment claims both plan and review markers; "
                "manual recovery is required"
            )
        if len(plan_matches) > 1:
            raise CommentAliasConflictError(
                f"ambiguous actor-owned comment aliases for {plan_marker!r}; "
                "manual recovery is required"
            )
        if len(review_matches) > 1:
            raise CommentAliasConflictError(
                f"ambiguous actor-owned comment aliases for {review_marker!r}; "
                "manual recovery is required"
            )
        if plan_matches:
            plan_candidates.append(comment)
        if review_matches:
            review_candidates.append(comment)

    if len(plan_candidates) > 1:
        raise CommentAliasConflictError(
            f"ambiguous actor-owned comment aliases for {plan_marker!r}; "
            "manual recovery is required"
        )
    if len(review_candidates) > 1:
        raise CommentAliasConflictError(
            f"ambiguous actor-owned comment aliases for {review_marker!r}; "
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
    """Return one candidate or stop before a marker-identity mutation.

    A role has one durable identity. More than one actor-owned comment, or
    more than one alias in a selected comment, requires manual recovery. This
    rule prevents a migration from selecting a newest comment and deleting a
    potentially independent artifact.
    """
    if len(candidates) > 1:
        raise CommentAliasConflictError(
            f"ambiguous {ownership} comment aliases for {marker!r}; manual recovery is required"
        )
    if not candidates:
        return None

    candidate = candidates[0]
    if len(marker_aliases_in_body(body_of(candidate), aliases)) != 1:
        raise CommentAliasConflictError(
            f"ambiguous {ownership} comment aliases for {marker!r}; manual recovery is required"
        )
    return candidate
