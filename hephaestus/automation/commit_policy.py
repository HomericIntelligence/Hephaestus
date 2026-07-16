"""Conventional Commit policy helpers for automation-generated subjects."""

from __future__ import annotations

import re

# This mirrors ``ALLOWED_TYPES`` in ``scripts/check_conventional_commit.py``.
# The scripts module remains the validation boundary, while automation uses this
# helper to ensure generated subjects are valid before creating a PR.
ALLOWED_CONVENTIONAL_TYPES = frozenset(
    {"feat", "fix", "docs", "refactor", "test", "chore", "ci", "build", "perf", "style", "revert"}
)

_CONVENTIONAL_PREFIX = re.compile(r"^(?P<type>[a-z]+)(?P<scope>\([^)]*\))?(?P<bang>!)?:\s")


def normalize_conventional_type(subject: str, *, default: str = "chore") -> str:
    """Return a subject whose leading type is accepted by the CI policy.

    Args:
        subject: The one-line commit or PR subject to normalize.
        default: The allowlisted fallback type for unknown or absent prefixes.

    Returns:
        A subject with an allowlisted leading Conventional Commit type.

    """
    match = _CONVENTIONAL_PREFIX.match(subject)
    if match is None:
        return f"{default}: {subject.strip()}" if subject.strip() else f"{default}: update"
    scope = match.group("scope") or ""
    bang = match.group("bang") or ""
    description = subject[match.end() :].strip()
    scope_is_valid = not scope or bool(scope[1:-1].strip())
    if match.group("type") in ALLOWED_CONVENTIONAL_TYPES and scope_is_valid and description:
        return subject
    valid_scope = scope if scope_is_valid else ""
    return f"{default}{valid_scope}{bang}: {description or 'update'}"
