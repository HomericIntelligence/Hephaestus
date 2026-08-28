"""Shared identity checks for marker-keyed GitHub comments."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .review_journal import top_level_marker_occurrences


class CommentAliasConflictError(RuntimeError):
    """More than one comment or marker alias claims one automation role."""


def marker_aliases_in_body(body: str, aliases: Sequence[str]) -> tuple[str, ...]:
    """Return exact top-level aliases in document order, including repeats."""
    return top_level_marker_occurrences(body, aliases)


def has_marker_alias(body: str, aliases: Sequence[str]) -> bool:
    """Return whether *body* has at least one exact top-level role alias."""
    return bool(marker_aliases_in_body(body, aliases))


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
