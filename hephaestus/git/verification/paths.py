"""Safe repository-relative paths for host verification.

A host check can be scoped to a changed path. Paths arrive from GitHub data,
so the library normalizes and bounds them before any command uses them.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["safe_changed_paths", "safe_relative_path"]

_RELATIVE_PATH_MAX_BYTES = 4096


def safe_relative_path(value: object) -> str | None:
    """Return an unchanged repository-relative path, or None when unsafe."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    path = Path(value)
    if path.is_absolute() or path.as_posix() != value:
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return None
    if not encoded or len(encoded) > _RELATIVE_PATH_MAX_BYTES:
        return None
    return value


def safe_changed_paths(value: object, *, count_max: int, bytes_max: int) -> tuple[str, ...] | None:
    """Return bounded unique relative paths, or None for invalid input."""
    if count_max <= 0 or bytes_max <= 0:
        raise ValueError("path bounds must be positive")
    if not isinstance(value, (list, tuple)) or len(value) > count_max:
        return None
    paths: list[str] = []
    total = 0
    for item in value:
        path = safe_relative_path(item)
        if path is None:
            return None
        total += len(path.encode("utf-8")) + 1
        if total > bytes_max:
            return None
        paths.append(path)
    if len(set(paths)) != len(paths):
        return None
    return tuple(paths)
