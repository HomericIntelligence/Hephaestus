"""Standalone identity checks for the recovered-requirements marker family."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from hephaestus.automation.requirements_recovery import (
    RECOVERY_PROVENANCE_PREFIX as RECOVERY_PROVENANCE_PREFIX,
    RecoveryCommentIdentityError as RecoveryCommentIdentityError,
    RecoveryCommentSelection,
    parse_recovery_provenance,
    select_recovery_comment,
)


def marker_matches_body(marker: str, body: str) -> bool:
    """Return whether *marker* agrees with the rendered *body*."""
    first_line, separator, _remainder = body.partition("\n")
    if first_line.endswith("\r"):
        first_line = first_line[:-1]
    if first_line != marker or not separator:
        return False
    return parse_recovery_provenance(body) is not None


def marker_body_error(marker: str, body: str) -> str | None:
    """Return a bounded mismatch reason, or ``None`` when they agree."""
    first_line, separator, _remainder = body.partition("\n")
    if first_line.endswith("\r"):
        first_line = first_line[:-1]
    if first_line != marker:
        return "marker does not match the first body line"
    if not separator:
        return "marker is not followed by a body"
    if parse_recovery_provenance(body) is None:
        return "marker provenance is malformed or disagrees with the body"
    return None


def validate_recovery_comment_identities(
    comments: Sequence[Any],
    *,
    body_of: Callable[[Any], str],
    owned_of: Callable[[Any], bool],
) -> RecoveryCommentSelection[Any] | None:
    """Validate and select one actor-owned recovery comment.

    Delegates to :func:`select_recovery_comment`. Raises
    :class:`RecoveryCommentIdentityError` when the journal is unsafe.
    """
    return select_recovery_comment(comments, body_of=body_of, owned_of=owned_of)


def select_marker_target(
    comments: Sequence[Any],
    *,
    body_of: Callable[[Any], str],
    owned_of: Callable[[Any], bool],
) -> Any | None:
    """Return the comment that carries the one valid recovery marker."""
    selection = validate_recovery_comment_identities(comments, body_of=body_of, owned_of=owned_of)
    if selection is None:
        return None
    return selection.comment
