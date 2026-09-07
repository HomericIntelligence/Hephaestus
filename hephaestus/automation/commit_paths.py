"""Classify and bind exact Git paths for automated commits."""

from __future__ import annotations

import logging
import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SECRET_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".secret",
        "credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
SECRET_FILE_EXTENSIONS: frozenset[str] = frozenset({".key", ".pem", ".pfx", ".p12"})
_PORCELAIN_STATUS_PAIRS: frozenset[str] = frozenset(
    {"D ", "DD", "AU", "UD", "UA", "DU", "AA", "UU", "??"}
    | {f" {worktree_status}" for worktree_status in "AMDTRC"}
    | {f"{index_status}{worktree_status}" for index_status in "MTARC" for worktree_status in " MTD"}
)
_UNMERGED_STATUS_PAIRS: frozenset[str] = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


@dataclass(frozen=True)
class CommitPaths:
    """Paths classified for ordinary and index-update staging."""

    add_paths: tuple[str, ...]
    update_paths: tuple[str, ...]


def is_bounded_commit_paths(
    paths: CommitPaths,
    *,
    max_paths: int,
    max_bytes: int,
) -> bool:
    """Return whether an inspected path manifest is safe and bounded."""
    combined = (*paths.add_paths, *paths.update_paths)
    if not combined or not all(isinstance(path, str) for path in combined):
        return False
    if (
        len(combined) > max_paths
        or len(set(paths.add_paths)) != len(paths.add_paths)
        or len(set(paths.update_paths)) != len(paths.update_paths)
    ):
        return False
    encoded_bytes = 0
    for path in combined:
        relative = Path(path)
        if (
            not path
            or "\0" in path
            or relative.is_absolute()
            or relative.as_posix() != path
            or any(component in {"", ".", ".."} for component in relative.parts)
        ):
            return False
        encoded_bytes += len(os.fsencode(path)) + 1
        if encoded_bytes > max_bytes:
            return False
    return True


def parse_porcelain_status(output: str) -> tuple[tuple[str, str], ...]:
    """Return status and target-path entries from NUL-delimited porcelain v1."""
    if not output:
        return ()
    if not output.endswith("\0"):
        raise RuntimeError("Malformed git status --porcelain=v1 -z output")
    records = output[:-1].split("\0")
    if any(not record for record in records):
        raise RuntimeError("Malformed git status --porcelain=v1 -z output")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2] != " " or not record[3:]:
            raise RuntimeError("Malformed git status --porcelain=v1 -z output")
        status = record[:2]
        if status not in _PORCELAIN_STATUS_PAIRS:
            raise RuntimeError("Malformed git status --porcelain=v1 -z output")
        entries.append((status, record[3:]))
        index += 1
        if "R" in status or "C" in status:
            if index >= len(records):
                raise RuntimeError("Malformed renamed path in git porcelain output")
            index += 1
    return tuple(entries)


def is_secret_path(path: str) -> bool:
    """Return whether a repository-relative path matches secret-file policy."""
    return any(
        component in SECRET_FILE_NAMES
        or any(component.endswith(extension) for extension in SECRET_FILE_EXTENSIONS)
        for component in Path(path).parts
    )


def select_commit_paths(
    entries: Collection[tuple[str, str]],
    allowed_paths: Collection[str] | None,
) -> CommitPaths:
    """Apply exact allowlist and secret policy to parsed status entries."""
    allowed = set(allowed_paths) if allowed_paths is not None else None
    add_paths: list[str] = []
    update_paths: list[str] = []
    for status, path in entries:
        if status in _UNMERGED_STATUS_PAIRS:
            raise RuntimeError("Cannot commit an unresolved merge")
        if allowed is not None and path not in allowed:
            logger.debug("Skipping non-allowlisted file: %r", path)
            continue
        if is_secret_path(path):
            logger.warning("Skipping potential secret file: %r", path)
            continue
        if "D" in status:
            update_paths.append(path)
        else:
            add_paths.append(path)
    return CommitPaths(tuple(add_paths), tuple(update_paths))


def reject_filtered_path_shape_changes(
    entries: Collection[tuple[str, str]],
    selected: CommitPaths,
) -> None:
    """Reject a selected path that can implicitly stage a filtered path."""
    add_paths = set(selected.add_paths)
    selected_paths = add_paths | set(selected.update_paths)
    filtered_paths = {path for _status, path in entries if path not in selected_paths}
    filtered_components = {tuple(path.split("/")) for path in filtered_paths}
    filtered_ancestors = {
        components[:length]
        for components in filtered_components
        for length in range(1, len(components))
    }
    for selected_path in selected_paths:
        components = tuple(selected_path.split("/"))
        selected_add_replaces_descendant = (
            selected_path in add_paths and components in filtered_ancestors
        )
        selected_descendant_replaces_filtered = any(
            components[:length] in filtered_components for length in range(1, len(components))
        )
        if selected_add_replaces_descendant or selected_descendant_replaces_filtered:
            raise RuntimeError("A selected file-tree change overlaps a filtered path")
