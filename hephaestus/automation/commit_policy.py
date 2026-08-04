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
_STRICT_CONVENTIONAL_PREFIX = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<bang>!)?\s*:\s*(?P<description>.*)$"
)


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


def normalize_strict_conventional_title(title: str, *, default: str = "chore") -> str:
    """Return a title guaranteed to satisfy the strict Conventional Commit gate.

    The type-only normalizer intentionally preserves authored scopes and
    descriptions.  PR titles need a stronger guarantee because they become
    squash-merge subjects: malformed scopes and empty descriptions must be
    repaired before a PR is created.

    Args:
        title: The issue-derived title to normalize.
        default: The allowlisted type used for malformed or unrecognized
            prefixes.

    Returns:
        A one-line, strict Conventional Commit title with a nonempty
        description.

    """
    normalized_default = default if default in ALLOWED_CONVENTIONAL_TYPES else "chore"
    one_line_title = " ".join(title.split())
    match = _STRICT_CONVENTIONAL_PREFIX.match(one_line_title)
    if match is None:
        return f"{normalized_default}: {one_line_title or 'update'}"

    type_token = match.group("type")
    normalized_type = type_token if type_token in ALLOWED_CONVENTIONAL_TYPES else normalized_default
    scope = match.group("scope")
    normalized_scope = f"({scope})" if scope and scope.strip() else ""
    bang = match.group("bang") or ""
    description = match.group("description").strip() or "update"
    return f"{normalized_type}{normalized_scope}{bang}: {description}"
