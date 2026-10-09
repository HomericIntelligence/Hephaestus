"""Validated scope-retraction metadata shared by PR review and publication."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, TypeGuard

SCOPE_RETRACTION_MARKER_PREFIX = "<!-- hephaestus-scope-retraction-paths:"


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


def scope_retraction_paths_from_finding(
    finding: Mapping[str, object],
) -> tuple[str, ...] | None:
    """Read anchored metadata before the host publishes a review thread.

    Return an empty tuple for an ordinary finding and None for invalid metadata.
    Body text does not classify a finding.
    """
    if "scope_retraction_paths" not in finding:
        return ()
    paths = normalize_scope_retraction_paths(finding["scope_retraction_paths"])
    path = finding.get("path")
    if paths is None or not is_safe_scope_retraction_path(path) or path not in paths:
        return None
    return paths


def scope_retraction_paths_from_body(body: object) -> tuple[str, ...] | None:
    """Read a complete manifest from one durable host marker.

    Return an empty tuple when the marker is absent. Return None when a marker
    is incomplete, repeated, malformed, or unsafe. Body words are not metadata.
    """
    if not isinstance(body, str) or SCOPE_RETRACTION_MARKER_PREFIX not in body:
        return ()
    if body.count(SCOPE_RETRACTION_MARKER_PREFIX) != 1:
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


def scope_retraction_paths_for_threads(
    threads: list[dict[str, Any]],
) -> tuple[str, ...] | None:
    """Return the complete safe retraction manifest requested by *threads*.

    A scope-control finding must carry one complete manifest which includes
    its anchored file.  The host uses this result both when building the
    implementation prompt and when configuring the commit/push verification;
    an incomplete or unsafe request is therefore never inferred from prose.
    """
    paths: set[str] = set()
    for thread in threads:
        scope_paths = scope_retraction_paths_from_body(thread.get("body"))
        if scope_paths == ():
            continue
        path = thread.get("path")
        if (
            scope_paths is None
            or not is_safe_scope_retraction_path(path)
            or path not in scope_paths
        ):
            return None
        paths.update(scope_paths)
    return tuple(sorted(paths))
