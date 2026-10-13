"""Identity checks for actor-owned recovered-requirements comments."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .comment_identity import (
    CommentAliasConflictError,
    has_marker_alias,
    select_unambiguous_comment,
    validate_planning_comment_identities,
)
from .requirements_recovery import RECOVERY_PROVENANCE_PREFIX, parse_recovery_provenance
from .review_journal import has_exact_leading_marker


def is_recovery_provenance_marker(marker: str) -> bool:
    """Return whether *marker* names the versioned recovery marker family."""
    return marker == RECOVERY_PROVENANCE_PREFIX


def has_valid_recovery_provenance(body: str) -> bool:
    """Return whether *body* starts with one valid recovery provenance marker."""
    return (
        body.startswith(RECOVERY_PROVENANCE_PREFIX) and parse_recovery_provenance(body) is not None
    )


def marker_body_error(body: str, marker: str) -> str | None:
    """Return a write error when *body* is not valid for *marker*."""
    if is_recovery_provenance_marker(marker):
        if not has_valid_recovery_provenance(body):
            return "canonical comment body must start with a valid recovered marker"
        return None
    if not has_exact_leading_marker(body, marker):
        return f"canonical comment body must start with marker {marker!r}"
    return None


def marker_matches_body(body: str, marker: str, aliases: Sequence[str]) -> bool:
    """Return whether *body* has one valid identity in the marker family."""
    if is_recovery_provenance_marker(marker):
        return has_valid_recovery_provenance(body)
    return has_marker_alias(body, aliases)


def validate_recovery_comment_identities[T](
    comments: Sequence[T],
    *,
    body_of: Callable[[T], str],
    owned_of: Callable[[T], bool],
) -> None:
    """Reject malformed, foreign, duplicate, or ambiguous recovery comments."""
    candidates = [
        comment for comment in comments if body_of(comment).startswith(RECOVERY_PROVENANCE_PREFIX)
    ]
    for comment in candidates:
        body = body_of(comment)
        if not owned_of(comment):
            raise CommentAliasConflictError(
                "foreign or unverifiable recovered-requirements marker; manual recovery is required"
            )
        if not has_valid_recovery_provenance(body):
            raise CommentAliasConflictError(
                "malformed recovered-requirements marker; manual recovery is required"
            )
    if len(candidates) > 1:
        raise CommentAliasConflictError(
            "ambiguous recovered-requirements comments; manual recovery is required"
        )


def validate_marker_identities[T](
    comments: Sequence[T],
    *,
    marker: str,
    planning_marker: bool,
    body_of: Callable[[T], str],
    owned_of: Callable[[T], bool],
) -> None:
    """Validate the selected marker family before a comment mutation."""
    if is_recovery_provenance_marker(marker):
        validate_recovery_comment_identities(
            comments,
            body_of=body_of,
            owned_of=owned_of,
        )
    elif planning_marker:
        validate_planning_comment_identities(
            comments,
            body_of=body_of,
            owned_of=owned_of,
        )


def select_marker_target[T](
    candidates: Sequence[T],
    *,
    marker: str,
    aliases: Sequence[str],
    body_of: Callable[[T], str],
    owned_of: Callable[[T], bool] | None = None,
    ownership: str = "actor-owned",
) -> T | None:
    """Select one validated marker-family target without choosing duplicates."""
    if is_recovery_provenance_marker(marker):
        # This family carries a versioned provenance body, so alias matching does
        # not apply. Enforce the same duplicate and ownership rules here instead
        # of depending on a caller to have validated first.
        if owned_of is not None and not all(owned_of(candidate) for candidate in candidates):
            raise CommentAliasConflictError(
                f"foreign or unverifiable {ownership} recovered-requirements marker; "
                "manual recovery is required"
            )
        if len(candidates) > 1:
            raise CommentAliasConflictError(
                f"ambiguous {ownership} recovered-requirements comments; "
                "manual recovery is required"
            )
        return candidates[0] if candidates else None
    return select_unambiguous_comment(
        candidates,
        marker=marker,
        aliases=aliases,
        body_of=body_of,
        ownership=ownership,
    )
