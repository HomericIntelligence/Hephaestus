"""Conventional Commit policy helpers for automation-generated subjects."""

from __future__ import annotations

import re

ALLOWED_CONVENTIONAL_TYPES = frozenset(
    {
        "feat",
        "fix",
        "docs",
        "refactor",
        "test",
        "chore",
        "ci",
        "build",
        "perf",
        "style",
        "revert",
    }
)

_CONVENTIONAL_PREFIX = re.compile(r"^(?P<type>[a-z]+)(?P<scope>\([^)]*\))?(?P<bang>!)?:\s")


def normalize_conventional_type(subject: str, *, default: str = "chore") -> str:
    """Return an authored subject whose leading type is allowlisted.

    Args:
        subject: The one-line subject to normalize.
        default: The allowlisted type used when the subject is not authored
            with an allowlisted Conventional Commit type.

    Returns:
        The original subject when its type is allowlisted, otherwise a subject
        with the leading type replaced or added.

    """
    match = _CONVENTIONAL_PREFIX.match(subject)
    if match is None:
        return f"{default}: {subject.strip()}" if subject.strip() else f"{default}: update"
    if match.group("type") in ALLOWED_CONVENTIONAL_TYPES:
        return subject
    scope = match.group("scope") or ""
    bang = match.group("bang") or ""
    return f"{default}{scope}{bang}: {subject[match.end() :]}"
