"""Validated scope-retraction metadata shared by PR review and publication."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import TypeGuard

SCOPE_RETRACTION_MARKER_PREFIX = "<!-- hephaestus-scope-retraction-paths:"
_SCOPE_ACTION_RE = re.compile(r"\b(?:drop|remove|split)\b", re.IGNORECASE)
_SCOPE_BOUNDARY_RE = re.compile(r"\b(?:unrelated|out[\s-]*of[\s-]*scope)\b", re.IGNORECASE)


def is_scope_retraction_finding(body: object) -> TypeGuard[str]:
    """Return whether a finding explicitly requests removal for scope reasons."""
    return bool(
        isinstance(body, str) and _SCOPE_ACTION_RE.search(body) and _SCOPE_BOUNDARY_RE.search(body)
    )


def is_safe_scope_retraction_path(path: object) -> TypeGuard[str]:
    """Return whether a path is safe as both a Git pathspec and prompt datum."""
    if (
        not isinstance(path, str)
        or not path
        or path == "."
        or path.startswith(("/", "./", ":"))
        or "\\" in path
        or "`" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        return False
    pure_path = PurePosixPath(path)
    return (
        not pure_path.is_absolute() and "." not in pure_path.parts and ".." not in pure_path.parts
    )


def normalize_scope_retraction_paths(value: object) -> tuple[str, ...] | None:
    """Validate a non-empty complete manifest and return a stable tuple."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if not all(is_safe_scope_retraction_path(path) for path in value):
        return None
    return tuple(sorted(set(value)))


def scope_retraction_marker(paths: tuple[str, ...]) -> str:
    """Serialize validated paths into the durable review-comment marker."""
    normalized = normalize_scope_retraction_paths(paths)
    if normalized is None:
        raise ValueError("scope retraction paths must be a safe non-empty manifest")
    return f"{SCOPE_RETRACTION_MARKER_PREFIX} {json.dumps(normalized)} -->"


def scope_retraction_paths_from_body(body: object) -> tuple[str, ...] | None:
    """Read a complete manifest from an explicit scope-retraction finding.

    ``()`` means the finding is ordinary remediation. ``None`` means the
    finding requested a scope retraction but lacks a valid complete manifest,
    which must stop publication rather than guess which files to preserve.
    """
    if not is_scope_retraction_finding(body):
        return ()
    if not isinstance(body, str):
        return None
    marker_payloads = [
        line.strip()[len(SCOPE_RETRACTION_MARKER_PREFIX) : -3].strip()
        for line in body.splitlines()
        if line.strip().startswith(SCOPE_RETRACTION_MARKER_PREFIX) and line.strip().endswith("-->")
    ]
    if len(marker_payloads) != 1:
        return None
    try:
        return normalize_scope_retraction_paths(json.loads(marker_payloads[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
