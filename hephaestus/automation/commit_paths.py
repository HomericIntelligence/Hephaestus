"""Classify and bind exact Git paths for automated commits."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
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


@dataclass(frozen=True)
class CommitPaths:
    """Paths classified for ordinary and index-update staging."""

    add_paths: tuple[str, ...]
    update_paths: tuple[str, ...]


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
    filename = Path(path).name
    return filename in SECRET_FILE_NAMES or any(
        filename.endswith(extension) for extension in SECRET_FILE_EXTENSIONS
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


def head_tracked_commit_paths(
    paths: CommitPaths,
    worktree_path: Path,
    *,
    revision: str,
    git_timeout: int | None,
    runner: Callable[..., object],
    env: dict[str, str] | None = None,
) -> CommitPaths:
    """Remove update paths that do not exist in the specified base tree."""
    if not paths.update_paths:
        return paths
    argv = [
        "git",
        "--literal-pathspecs",
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        revision,
        "--",
        *paths.update_paths,
    ]
    if env is None:
        result = runner(argv, cwd=worktree_path, timeout=git_timeout)
    else:
        result = runner(argv, cwd=worktree_path, timeout=git_timeout, env=env)
    stdout = getattr(result, "stdout", "")
    tracked_at_head = {
        path for path in (stdout if isinstance(stdout, str) else "").split("\0") if path
    }
    return CommitPaths(
        add_paths=paths.add_paths,
        update_paths=tuple(path for path in paths.update_paths if path in tracked_at_head),
    )
