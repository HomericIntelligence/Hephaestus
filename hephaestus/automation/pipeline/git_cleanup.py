"""Shared low-level cleanup operations for both pipeline worker lanes."""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.git_utils import (
    delete_local_branch_if_unchanged,
    delete_reserved_branch_if_unchanged,
    run,
)
from hephaestus.automation.source_worktree import SourceWorkspaceError, SourceWorkspaceManager
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.utils.file_lock import file_lock
from hephaestus.utils.helpers import get_repo_root
from hephaestus.utils.worktree_identity import is_expected_managed_worktree_path

from .git_jobs import GitJob
from .job_results import JobResult

_CODEX_SESSION_IDENTITY = re.compile(r"[0-9a-f]{64}")
_CODEX_SESSION_CLEANUP_MAX_ENTRIES = 100_000
_CODEX_SESSION_CLEANUP_MAX_BYTES = 2 * 1024 * 1024 * 1024
_CODEX_QUARANTINE_MAX_BYTES = 16 * 1024
_CODEX_DELETE_STAGING = ".terminal-delete-work"
_CODEX_DELETE_NODE = re.compile(r"node-([0-9a-f]+)-([0-9a-f]+)")
_CODEX_DELETE_IDENTITY = re.compile(r"identity-([0-9a-f]{64})-([0-9a-f]+)-([0-9a-f]+)")
_CODEX_QUARANTINE_KEYS = frozenset(
    {
        "issue",
        "private_profile_path",
        "repository",
        "run_nonce",
        "session_identity_digest",
        "status",
        "worktree_path",
    }
)


class _CodexSessionCleanupError(RuntimeError):
    """Report a fail-closed Codex session-root cleanup result."""


class _CodexSessionCleanupProgressError(_CodexSessionCleanupError):
    """Report bounded cleanup that made forward progress."""


@dataclass(frozen=True)
class _CodexQuarantineBinding:
    """Bind quarantine evidence to one terminal cleanup request."""

    session_root: Path
    worktree_path: Path
    repository: str
    issue_number: int


def _codex_session_root(worktree_path: Path) -> Path:
    """Return the deterministic sibling session root for one worktree."""
    lexical_worktree = _normalized_lexical_path(worktree_path)
    return lexical_worktree.parent / f".{lexical_worktree.name}-codex-sessions"


def _normalized_lexical_path(path: Path) -> Path:
    """Return an absolute normalized path without following a link."""
    return Path(os.path.abspath(path))


def _opened_directory_matches(status: os.stat_result, descriptor: int) -> bool:
    """Return whether an opened directory still has its inspected identity."""
    opened = os.fstat(descriptor)
    return (
        stat.S_ISDIR(status.st_mode)
        and stat.S_ISDIR(opened.st_mode)
        and status.st_uid == os.geteuid()
        and opened.st_uid == os.geteuid()
        and (status.st_dev, status.st_ino) == (opened.st_dev, opened.st_ino)
    )


def _same_owned_entry(status: os.stat_result, opened: os.stat_result) -> bool:
    """Return whether two observations identify one effective-user entry."""
    return (
        status.st_uid == os.geteuid()
        and opened.st_uid == os.geteuid()
        and stat.S_IFMT(status.st_mode) == stat.S_IFMT(opened.st_mode)
        and (status.st_dev, status.st_ino) == (opened.st_dev, opened.st_ino)
    )


def _repository_matches_quarantine(receipt_repository: object, expected_repository: str) -> bool:
    """Return whether a quarantine repository matches the cleanup job."""
    if not isinstance(receipt_repository, str) or not receipt_repository:
        return False
    if "/" in expected_repository:
        return receipt_repository == expected_repository
    return receipt_repository.endswith(f"/{expected_repository}")


def _read_codex_quarantine(descriptor: int) -> tuple[os.stat_result, bytes]:
    """Read one bounded owner-only quarantine receipt from an open file."""
    status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o400
        or status.st_nlink != 1
        or status.st_size > _CODEX_QUARANTINE_MAX_BYTES
    ):
        raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
    chunks: list[bytes] = []
    remaining = _CODEX_QUARANTINE_MAX_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(4096, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > _CODEX_QUARANTINE_MAX_BYTES:
        raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
    return status, payload


def _validate_codex_quarantine(
    descriptor: int,
    *,
    identity: str,
    session_root: Path,
    worktree_path: Path,
    repository: str,
    issue_number: int,
) -> os.stat_result:
    """Validate one terminal-state quarantine against its cleanup request."""
    status, raw = _read_codex_quarantine(descriptor)
    try:
        payload = json.loads(raw, parse_constant=lambda _value: None)
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid") from None
    if not isinstance(payload, dict) or set(payload) != _CODEX_QUARANTINE_KEYS or raw != canonical:
        raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
    issue = payload.get("issue")
    run_nonce = payload.get("run_nonce")
    session_digest = payload.get("session_identity_digest")
    if (
        isinstance(issue, bool)
        or not isinstance(issue, int)
        or issue < 1
        or issue != issue_number
        or not isinstance(run_nonce, str)
        or _CODEX_SESSION_IDENTITY.fullmatch(run_nonce) is None
        or not isinstance(session_digest, str)
        or _CODEX_SESSION_IDENTITY.fullmatch(session_digest) is None
        or payload.get("status") != "terminal-state-invalid"
    ):
        raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
    worktree = _normalized_lexical_path(worktree_path)
    expected_profile = session_root / identity / ".runs" / run_nonce / "profile"
    private_profile = payload.get("private_profile_path")
    try:
        profile_is_canonical = (
            isinstance(private_profile, str)
            and private_profile == str(expected_profile)
            and Path(private_profile).is_absolute()
            and Path(private_profile).resolve(strict=False) == expected_profile
        )
    except OSError:
        profile_is_canonical = False
    if (
        payload.get("worktree_path") != str(worktree)
        or not _repository_matches_quarantine(payload.get("repository"), repository)
        or not profile_is_canonical
    ):
        raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
    return status


def _validate_codex_quarantine_entry(
    store_descriptor: int,
    *,
    identity: str,
    session_root: Path,
    worktree_path: Path,
    repository: str,
    issue_number: int,
) -> None:
    """Open and validate one no-follow quarantine receipt by name."""
    receipt_descriptor = -1
    try:
        lexical = os.stat(".quarantine.json", dir_fd=store_descriptor, follow_symlinks=False)
        receipt_descriptor = os.open(
            ".quarantine.json",
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=store_descriptor,
        )
        opened = _validate_codex_quarantine(
            receipt_descriptor,
            identity=identity,
            session_root=session_root,
            worktree_path=worktree_path,
            repository=repository,
            issue_number=issue_number,
        )
        if not _same_owned_entry(lexical, opened):
            raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
    except _CodexSessionCleanupError:
        raise
    except OSError:
        raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid") from None
    finally:
        if receipt_descriptor >= 0:
            os.close(receipt_descriptor)


def _validate_codex_session_store(
    descriptor: int,
    *,
    identity: str,
    session_root: Path,
    worktree_path: Path,
    repository: str,
    issue_number: int,
) -> bool:
    """Reject an active receipt or an invalid identity-store entry."""
    allowed = {".runs", ".quarantine.json", "sessions"}
    has_quarantine = False
    with os.scandir(descriptor) as entries:
        for count, entry in enumerate(entries, start=1):
            if count > _CODEX_SESSION_CLEANUP_MAX_ENTRIES:
                raise _CodexSessionCleanupError("Codex session root cleanup limit exceeded")
            name = entry.name
            if name == ".active.json" or name not in allowed:
                raise _CodexSessionCleanupError(
                    "Codex session root contains an active or invalid control receipt"
                )
            if name == ".quarantine.json":
                _validate_codex_quarantine_entry(
                    descriptor,
                    identity=identity,
                    session_root=session_root,
                    worktree_path=worktree_path,
                    repository=repository,
                    issue_number=issue_number,
                )
                has_quarantine = True
                continue
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
                raise _CodexSessionCleanupError("Codex session root identity is invalid")
    return has_quarantine


def _validate_codex_session_root_children(
    descriptor: int,
    *,
    session_root: Path,
    worktree_path: Path,
    repository: str,
    issue_number: int,
) -> bool:
    """Validate identity stores and reject host-control receipt residue."""
    has_quarantine = False
    with os.scandir(descriptor) as entries:
        for count, entry in enumerate(entries, start=1):
            if count > _CODEX_SESSION_CLEANUP_MAX_ENTRIES:
                raise _CodexSessionCleanupError("Codex session root cleanup limit exceeded")
            name = entry.name
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if name == ".transient-auth":
                if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                continue
            if _CODEX_SESSION_IDENTITY.fullmatch(name) is None or not stat.S_ISDIR(status.st_mode):
                raise _CodexSessionCleanupError(
                    "Codex session root contains an active or invalid control receipt"
                )
            child = -1
            try:
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                if not _opened_directory_matches(status, child):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                has_quarantine = (
                    _validate_codex_session_store(
                        child,
                        identity=name,
                        session_root=session_root,
                        worktree_path=worktree_path,
                        repository=repository,
                        issue_number=issue_number,
                    )
                    or has_quarantine
                )
            except OSError:
                raise _CodexSessionCleanupError("Codex session root identity is invalid") from None
            finally:
                if child >= 0:
                    os.close(child)
    return has_quarantine


def _open_owned_codex_directory(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> tuple[int, os.stat_result]:
    """Open one no-follow directory and bind it to its inspected inode."""
    if not stat.S_ISDIR(expected.st_mode) or expected.st_uid != os.geteuid():
        raise _CodexSessionCleanupError("Codex session root identity is invalid")
    os.chmod(
        name,
        0o700,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    normalized = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not _same_owned_entry(expected, normalized) or not stat.S_ISDIR(normalized.st_mode):
        raise _CodexSessionCleanupError("Codex session root identity is invalid")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    opened = os.fstat(descriptor)
    if not _same_owned_entry(normalized, opened) or not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise _CodexSessionCleanupError("Codex session root identity is invalid")
    return descriptor, opened


def _codex_staged_identity(name: str) -> str | None:
    """Return the identity encoded in one staged directory name."""
    match = _CODEX_DELETE_IDENTITY.fullmatch(name)
    return match.group(1) if match is not None else None


def _codex_delete_identity_name(identity: str, status: os.stat_result) -> str:
    """Return a staging name that binds one identity-store directory."""
    return f"identity-{identity}-{status.st_dev:x}-{status.st_ino:x}"


def _codex_delete_identity_matches(name: str, status: os.stat_result) -> bool:
    """Return whether a staged identity name binds its directory inode."""
    match = _CODEX_DELETE_IDENTITY.fullmatch(name)
    return bool(
        match is not None
        and stat.S_ISDIR(status.st_mode)
        and status.st_uid == os.geteuid()
        and (int(match.group(2), 16), int(match.group(3), 16)) == (status.st_dev, status.st_ino)
    )


def _codex_delete_node_name(status: os.stat_result) -> str:
    """Return a collision-free staging name for one held directory inode."""
    return f"node-{status.st_dev:x}-{status.st_ino:x}"


def _codex_delete_node_matches(name: str, status: os.stat_result) -> bool:
    """Return whether a staged name binds its owned directory inode."""
    match = _CODEX_DELETE_NODE.fullmatch(name)
    return bool(
        match is not None
        and stat.S_ISDIR(status.st_mode)
        and status.st_uid == os.geteuid()
        and (int(match.group(1), 16), int(match.group(2), 16)) == (status.st_dev, status.st_ino)
    )


def _remove_codex_session_tree_contents(  # noqa: C901 - traversal is iterative and fail-closed
    descriptor: int,
    *,
    budget: list[int],
    quarantine_binding: _CodexQuarantineBinding | None = None,
    identity: str | None = None,
    root_level: bool = False,
) -> None:
    """Delete one tree iteratively through held, no-follow descriptors."""
    if not root_level or quarantine_binding is None or identity is not None:
        raise _CodexSessionCleanupError("Codex session root identity is invalid")
    staging_descriptor = -1
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(_CODEX_DELETE_STAGING, mode=0o700, dir_fd=descriptor)
        staging_status = os.stat(
            _CODEX_DELETE_STAGING,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        staging_descriptor, staging_opened = _open_owned_codex_directory(
            descriptor,
            _CODEX_DELETE_STAGING,
            staging_status,
        )
        if stat.S_IMODE(staging_opened.st_mode) != 0o700:
            raise _CodexSessionCleanupError("Codex session root identity is invalid")

        pending: deque[tuple[str, os.stat_result, str | None]] = deque()
        quarantine_holders: list[tuple[str, os.stat_result, str]] = []
        staged_node_count = 0
        with os.scandir(staging_descriptor) as staged_entries:
            for entry in staged_entries:
                staged_name = entry.name
                staged_identity = _codex_staged_identity(staged_name)
                if staged_identity is None and _CODEX_DELETE_NODE.fullmatch(staged_name) is None:
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                staged_status = os.stat(
                    staged_name,
                    dir_fd=staging_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(staged_status.st_mode) or staged_status.st_uid != os.geteuid():
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                if staged_identity is not None and not _codex_delete_identity_matches(
                    staged_name,
                    staged_status,
                ):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                if staged_identity is None and not _codex_delete_node_matches(
                    staged_name,
                    staged_status,
                ):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                if staged_identity is None:
                    if staged_node_count >= _CODEX_SESSION_CLEANUP_MAX_ENTRIES:
                        continue
                    staged_node_count += 1
                pending.append((staged_name, staged_status, staged_identity))

        with os.scandir(descriptor) as root_entries:
            for entry in root_entries:
                name = entry.name
                if name == _CODEX_DELETE_STAGING:
                    continue
                if name == ".active.json":
                    raise _CodexSessionCleanupError(
                        "Codex session root contains an active or invalid control receipt"
                    )
                status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child, opened = _open_owned_codex_directory(descriptor, name, status)
                try:
                    os.fchmod(child, 0o700)
                    staged_name = (
                        _codex_delete_identity_name(name, opened)
                        if _CODEX_SESSION_IDENTITY.fullmatch(name) is not None
                        else _codex_delete_node_name(opened)
                    )
                    os.rename(
                        name,
                        staged_name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=staging_descriptor,
                    )
                    moved = os.stat(
                        staged_name,
                        dir_fd=staging_descriptor,
                        follow_symlinks=False,
                    )
                    if not _same_owned_entry(moved, opened):
                        raise _CodexSessionCleanupError("Codex session root identity is invalid")
                    pending.append((staged_name, moved, _codex_staged_identity(staged_name)))
                    budget[0] += 1
                    if budget[0] > _CODEX_SESSION_CLEANUP_MAX_ENTRIES:
                        raise _CodexSessionCleanupProgressError(
                            "Codex session root cleanup limit exceeded"
                        )
                finally:
                    os.close(child)

        while pending:
            staged_name, expected, staged_identity = pending.popleft()
            current_descriptor, current_opened = _open_owned_codex_directory(
                staging_descriptor,
                staged_name,
                expected,
            )
            try:
                os.fchmod(current_descriptor, 0o700)
                retain_quarantine = False
                with os.scandir(current_descriptor) as entries:
                    for entry in entries:
                        name = entry.name
                        if staged_identity is not None and name == ".active.json":
                            raise _CodexSessionCleanupError(
                                "Codex session root contains an active or invalid control receipt"
                            )
                        status = os.stat(name, dir_fd=current_descriptor, follow_symlinks=False)
                        if status.st_uid != os.geteuid():
                            raise _CodexSessionCleanupError(
                                "Codex session root identity is invalid"
                            )
                        if stat.S_ISDIR(status.st_mode):
                            budget[0] += 1
                            if budget[0] > _CODEX_SESSION_CLEANUP_MAX_ENTRIES:
                                raise _CodexSessionCleanupProgressError(
                                    "Codex session root cleanup limit exceeded"
                                )
                            child, opened = _open_owned_codex_directory(
                                current_descriptor,
                                name,
                                status,
                            )
                            try:
                                os.fchmod(child, 0o700)
                                child_name = _codex_delete_node_name(opened)
                                os.rename(
                                    name,
                                    child_name,
                                    src_dir_fd=current_descriptor,
                                    dst_dir_fd=staging_descriptor,
                                )
                                moved = os.stat(
                                    child_name,
                                    dir_fd=staging_descriptor,
                                    follow_symlinks=False,
                                )
                                if not _same_owned_entry(moved, opened):
                                    raise _CodexSessionCleanupError(
                                        "Codex session root identity is invalid"
                                    )
                                if len(pending) < _CODEX_SESSION_CLEANUP_MAX_ENTRIES:
                                    pending.append((child_name, moved, None))
                            finally:
                                os.close(child)
                            continue
                        if staged_identity is not None and name == ".quarantine.json":
                            file_descriptor = os.open(
                                name,
                                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                                dir_fd=current_descriptor,
                            )
                            try:
                                opened = os.fstat(file_descriptor)
                                if not _same_owned_entry(status, opened):
                                    raise _CodexSessionCleanupError(
                                        "Codex session root identity is invalid"
                                    )
                                _validate_codex_quarantine(
                                    file_descriptor,
                                    identity=staged_identity,
                                    session_root=quarantine_binding.session_root,
                                    worktree_path=quarantine_binding.worktree_path,
                                    repository=quarantine_binding.repository,
                                    issue_number=quarantine_binding.issue_number,
                                )
                                retain_quarantine = True
                            finally:
                                os.close(file_descriptor)
                            continue
                        budget[0] += 1
                        if budget[0] > _CODEX_SESSION_CLEANUP_MAX_ENTRIES:
                            raise _CodexSessionCleanupProgressError(
                                "Codex session root cleanup limit exceeded"
                            )
                        current = os.stat(
                            name,
                            dir_fd=current_descriptor,
                            follow_symlinks=False,
                        )
                        if not _same_owned_entry(current, status):
                            raise _CodexSessionCleanupError(
                                "Codex session root identity is invalid"
                            )
                        os.unlink(name, dir_fd=current_descriptor)
                current = os.stat(
                    staged_name,
                    dir_fd=staging_descriptor,
                    follow_symlinks=False,
                )
                if not _same_owned_entry(current, current_opened):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                if retain_quarantine:
                    assert staged_identity is not None  # noqa: S101 - validated above
                    quarantine_holders.append((staged_name, current, staged_identity))
                else:
                    os.rmdir(staged_name, dir_fd=staging_descriptor)
            finally:
                os.close(current_descriptor)

        retained_names = {name for name, _expected, _identity in quarantine_holders}
        with os.scandir(staging_descriptor) as remaining_entries:
            if any(entry.name not in retained_names for entry in remaining_entries):
                raise _CodexSessionCleanupProgressError("Codex session root cleanup limit exceeded")

        for staged_name, expected, staged_identity in quarantine_holders:
            holder_descriptor, holder_opened = _open_owned_codex_directory(
                staging_descriptor,
                staged_name,
                expected,
            )
            try:
                names = [entry.name for entry in os.scandir(holder_descriptor)]
                if names != [".quarantine.json"]:
                    raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
                _validate_codex_quarantine_entry(
                    holder_descriptor,
                    identity=staged_identity,
                    session_root=quarantine_binding.session_root,
                    worktree_path=quarantine_binding.worktree_path,
                    repository=quarantine_binding.repository,
                    issue_number=quarantine_binding.issue_number,
                )
                receipt_status = os.stat(
                    ".quarantine.json",
                    dir_fd=holder_descriptor,
                    follow_symlinks=False,
                )
                receipt_descriptor = os.open(
                    ".quarantine.json",
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=holder_descriptor,
                )
                try:
                    receipt_opened = os.fstat(receipt_descriptor)
                    if not _same_owned_entry(receipt_status, receipt_opened):
                        raise _CodexSessionCleanupError(
                            "Codex session quarantine receipt is invalid"
                        )
                    os.unlink(".quarantine.json", dir_fd=holder_descriptor)
                finally:
                    os.close(receipt_descriptor)
                current = os.stat(
                    staged_name,
                    dir_fd=staging_descriptor,
                    follow_symlinks=False,
                )
                if not _same_owned_entry(current, holder_opened):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                os.rmdir(staged_name, dir_fd=staging_descriptor)
            finally:
                os.close(holder_descriptor)

        staging_current = os.stat(
            _CODEX_DELETE_STAGING,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if not _same_owned_entry(staging_current, staging_opened):
            raise _CodexSessionCleanupError("Codex session root identity is invalid")
        os.rmdir(_CODEX_DELETE_STAGING, dir_fd=descriptor)
    except _CodexSessionCleanupError:
        raise
    except OSError:
        raise _CodexSessionCleanupError("Codex session root cleanup failed") from None
    finally:
        if staging_descriptor >= 0:
            os.close(staging_descriptor)


def _validate_codex_cleanup_recovery_root(  # noqa: C901 - validates staged identities
    descriptor: int,
    *,
    session_root: Path,
    worktree_path: Path,
    repository: str,
    issue_number: int,
) -> bool:
    """Validate one interrupted iterative-cleanup staging root."""
    staging_descriptor = -1
    quarantine_count = 0
    try:
        staging_status = os.stat(
            _CODEX_DELETE_STAGING,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        staging_descriptor, staging_opened = _open_owned_codex_directory(
            descriptor,
            _CODEX_DELETE_STAGING,
            staging_status,
        )
        if stat.S_IMODE(staging_opened.st_mode) != 0o700:
            raise _CodexSessionCleanupError("Codex session root identity is invalid")
        with os.scandir(staging_descriptor) as entries:
            for entry in entries:
                name = entry.name
                if (
                    _CODEX_DELETE_IDENTITY.fullmatch(name) is None
                    and _CODEX_DELETE_NODE.fullmatch(name) is None
                ):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                status = os.stat(name, dir_fd=staging_descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                staged_identity = _codex_staged_identity(name)
                if staged_identity is not None and not _codex_delete_identity_matches(name, status):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                if staged_identity is None and not _codex_delete_node_matches(name, status):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                if staged_identity is not None:
                    identity_descriptor, _opened = _open_owned_codex_directory(
                        staging_descriptor,
                        name,
                        status,
                    )
                    try:
                        quarantine_count += int(
                            _validate_codex_session_store(
                                identity_descriptor,
                                identity=staged_identity,
                                session_root=session_root,
                                worktree_path=worktree_path,
                                repository=repository,
                                issue_number=issue_number,
                            )
                        )
                    finally:
                        os.close(identity_descriptor)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                name = entry.name
                if name == _CODEX_DELETE_STAGING:
                    continue
                status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (
                    name == ".active.json"
                    or (
                        name != ".transient-auth"
                        and _CODEX_SESSION_IDENTITY.fullmatch(name) is None
                    )
                    or not stat.S_ISDIR(status.st_mode)
                    or status.st_uid != os.geteuid()
                ):
                    raise _CodexSessionCleanupError("Codex session root identity is invalid")
                if _CODEX_SESSION_IDENTITY.fullmatch(name) is not None:
                    identity_descriptor, _opened = _open_owned_codex_directory(
                        descriptor,
                        name,
                        status,
                    )
                    try:
                        quarantine_count += int(
                            _validate_codex_session_store(
                                identity_descriptor,
                                identity=name,
                                session_root=session_root,
                                worktree_path=worktree_path,
                                repository=repository,
                                issue_number=issue_number,
                            )
                        )
                    finally:
                        os.close(identity_descriptor)
        if quarantine_count > 1:
            raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
        return quarantine_count == 1
    except _CodexSessionCleanupError:
        raise
    except OSError:
        raise _CodexSessionCleanupError("Codex session root identity is invalid") from None
    finally:
        if staging_descriptor >= 0:
            os.close(staging_descriptor)


def _remove_codex_session_root(  # noqa: C901 - atomic cleanup validates each state transition
    worktree_path: Path,
    *,
    repository: str,
    issue_number: int,
    validate_only: bool = False,
    require_quarantine: bool = False,
    detach_only: bool = False,
) -> bool:
    """Remove one validated sibling session root, or accept its absence."""
    root = _codex_session_root(worktree_path)
    tombstone_name = f"{root.name}.terminal-cleanup"
    parent_descriptor = -1
    root_descriptor = -1
    try:
        parent_descriptor = os.open(
            root.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            lexical = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            try:
                lexical = os.stat(
                    tombstone_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            selected_name = tombstone_name
        else:
            selected_name = root.name
            try:
                os.stat(tombstone_name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _CodexSessionCleanupError("Codex session root identity is invalid")
        root_descriptor = os.open(
            selected_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        selected_path = root.with_name(selected_name)
        if (
            not _opened_directory_matches(lexical, root_descriptor)
            or stat.S_IMODE(lexical.st_mode) != 0o700
            or selected_path.resolve(strict=True) != selected_path.absolute()
        ):
            raise _CodexSessionCleanupError("Codex session root identity is invalid")
        cleanup_recovery = False
        if selected_name == tombstone_name:
            try:
                os.stat(
                    _CODEX_DELETE_STAGING,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                cleanup_recovery = True
        if cleanup_recovery:
            has_quarantine = _validate_codex_cleanup_recovery_root(
                root_descriptor,
                session_root=root,
                worktree_path=worktree_path,
                repository=repository,
                issue_number=issue_number,
            )
        else:
            has_quarantine = _validate_codex_session_root_children(
                root_descriptor,
                session_root=root,
                worktree_path=worktree_path,
                repository=repository,
                issue_number=issue_number,
            )
        if require_quarantine and not has_quarantine:
            raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
        if validate_only:
            return has_quarantine
        if selected_name == root.name:
            os.rename(
                root.name,
                tombstone_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            moved = os.stat(tombstone_name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (moved.st_dev, moved.st_ino) != (lexical.st_dev, lexical.st_ino):
                raise _CodexSessionCleanupError("Codex session root identity is invalid")
            selected_name = tombstone_name
            has_quarantine = _validate_codex_session_root_children(
                root_descriptor,
                session_root=root,
                worktree_path=worktree_path,
                repository=repository,
                issue_number=issue_number,
            )
            if require_quarantine and not has_quarantine:
                raise _CodexSessionCleanupError("Codex session quarantine receipt is invalid")
        if detach_only:
            return has_quarantine
        _remove_codex_session_tree_contents(
            root_descriptor,
            budget=[0, 0],
            quarantine_binding=_CodexQuarantineBinding(
                session_root=root,
                worktree_path=worktree_path,
                repository=repository,
                issue_number=issue_number,
            ),
            root_level=True,
        )
        try:
            current = os.stat(selected_name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            raise _CodexSessionCleanupError("Codex session root identity is invalid") from None
        if not _same_owned_entry(current, os.fstat(root_descriptor)):
            raise _CodexSessionCleanupError("Codex session root identity is invalid")
        try:
            os.rmdir(selected_name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except OSError:
            raise _CodexSessionCleanupError("Codex session root cleanup failed") from None
        try:
            os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return has_quarantine
        raise _CodexSessionCleanupError(
            "Codex session root contains an active or invalid control receipt"
        )
    except _CodexSessionCleanupError:
        raise
    except OSError:
        raise _CodexSessionCleanupError("Codex session root identity is invalid") from None
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _codex_session_cleanup_failure(
    worktree_path: Path,
    *,
    repository: str,
    issue_number: int,
    require_quarantine: bool = False,
    detach_only: bool = False,
) -> JobResult | None:
    """Return a typed failure if terminal session-root cleanup is unsafe."""
    try:
        _remove_codex_session_root(
            worktree_path,
            repository=repository,
            issue_number=issue_number,
            require_quarantine=require_quarantine,
            detach_only=detach_only,
        )
    except _CodexSessionCleanupProgressError as exc:
        return JobResult(
            ok=False,
            value={"cleanup_progress": True},
            error=str(exc),
        )
    except _CodexSessionCleanupError as exc:
        return JobResult(ok=False, error=str(exc))
    return None


def _codex_session_quarantine_proof(
    worktree_path: Path,
    *,
    repository: str,
    issue_number: int,
) -> tuple[bool, JobResult | None]:
    """Inspect terminal quarantine authority without changing session state."""
    try:
        has_quarantine = _remove_codex_session_root(
            worktree_path,
            repository=repository,
            issue_number=issue_number,
            validate_only=True,
        )
    except _CodexSessionCleanupError as exc:
        return False, JobResult(ok=False, error=str(exc))
    return has_quarantine, None


def _is_full_commit_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _is_expected_worktree_path(path: Path, *, repo_root: Path, issue_number: int) -> bool:
    """Bind destructive cleanup to one managed issue worktree identity."""
    lexical = _normalized_lexical_path(path)
    try:
        if lexical.is_symlink() or lexical.resolve(strict=False) != lexical:
            return False
    except OSError:
        return False
    return is_expected_managed_worktree_path(
        lexical,
        repo_root=repo_root,
        issue_number=issue_number,
    )


def _worktree_record(
    worktree_path: Path,
    *,
    repo_root: Path,
    timeout: int,
    worktree_manager_type: Any,
) -> dict[str, str] | None:
    """Return the registered record for an exact worktree path."""
    target = _normalized_lexical_path(worktree_path)
    manager = worktree_manager_type(repo_root=repo_root)
    return next(
        (
            record
            for record in manager.list_worktrees(raise_on_error=True, timeout=timeout)
            if record.get("path") and _normalized_lexical_path(Path(record["path"])) == target
        ),
        None,
    )


def _record_matches_canonical_worktree(
    record: dict[str, str],
    worktree_path: Path,
) -> bool:
    """Return whether a Git record names the same canonical lexical path."""
    registered_value = record.get("path")
    if not registered_value:
        return False
    lexical = _normalized_lexical_path(worktree_path)
    registered = _normalized_lexical_path(Path(registered_value))
    try:
        return (
            registered == lexical
            and lexical.resolve(strict=False) == lexical
            and registered.resolve(strict=False) == registered
        )
    except OSError:
        return False


def _ownership_changed(
    record: dict[str, str] | None,
    *,
    expected_branch: object,
    expected_head: object,
    expected_detached: bool,
) -> bool:
    """Return whether the registered worktree differs from its cleanup proof."""
    branch_changed = expected_branch is not None and (
        not isinstance(expected_branch, str)
        or not expected_branch
        or record is None
        or record.get("branch") != f"refs/heads/{expected_branch}"
    )
    head_changed = expected_head is not None and (
        not _is_full_commit_sha(expected_head)
        or record is None
        or record.get("commit") != expected_head
    )
    detached_changed = expected_detached and (record is None or bool(record.get("branch")))
    return branch_changed or head_changed or detached_changed


def _cleanup_local_branch(
    receipt: object,
    *,
    expected_branch: object,
    repo_root: Path,
    timeout: int,
) -> JobResult | None:
    """Delete one receipt-bound local branch, or report no request."""
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        return JobResult(ok=False, error="local branch cleanup receipt is invalid")
    cleanup_branch = receipt.get("branch")
    expected_sha = str(receipt.get("base_sha") or "")
    if (
        not isinstance(cleanup_branch, str)
        or cleanup_branch != expected_branch
        or not _is_full_commit_sha(expected_sha)
    ):
        return JobResult(ok=False, error="local branch cleanup receipt is invalid")
    deleted = delete_local_branch_if_unchanged(
        cleanup_branch,
        expected_sha,
        repo_root,
        timeout=timeout,
    )
    return JobResult(ok=True, value={"local_branch_deleted": deleted})


def _run_source_lane_cleanup(
    job: GitJob,
    *,
    worktree_path: Path,
    repo_root: Path,
    issue_number: int,
    expected_head: object,
    expected_detached: bool,
    worktree_manager_type: Any,
    remote_env: dict[str, str] | None,
    remote_config: tuple[str, ...],
    revalidate_remote: Callable[[], tuple[dict[str, str], tuple[str, ...]]] | None,
) -> JobResult:
    """Clean one receipt-owned source lane through its state owner."""
    try:
        source_lane = SourceLane(job.kwargs["source_lane"])
    except (KeyError, TypeError, ValueError):
        return JobResult(ok=False, error="source worktree cleanup lane is invalid")
    if issue_number < 1 or not _is_full_commit_sha(expected_head) or not expected_detached:
        return JobResult(ok=False, error="source worktree cleanup proof is invalid")
    manager = SourceWorkspaceManager(repo_root, repository=job.repo)
    if worktree_path.resolve() != manager.path_for(issue_number, source_lane).resolve():
        return JobResult(ok=False, error="source worktree cleanup identity is invalid")

    def physical_cleanup() -> None:
        generic_kwargs = dict(job.kwargs)
        generic_kwargs.pop("source_lane", None)
        result = run_cleanup_job(
            GitJob(
                repo=job.repo,
                op=job.op,
                timeout_s=job.timeout_s,
                kwargs=generic_kwargs,
                descr=job.descr,
                expected_repository=job.expected_repository,
            ),
            worktree_manager_type=worktree_manager_type,
            remote_env=remote_env,
            remote_config=remote_config,
            revalidate_remote=revalidate_remote,
        )
        if not result.ok:
            raise SourceWorkspaceError(result.error or "worktree cleanup failed")

    try:
        manager.cleanup(
            issue_number,
            source_lane,
            expected_revision=str(expected_head),
            expected_detached=expected_detached,
            physical_cleanup=physical_cleanup,
        )
    except SourceWorkspaceError as exc:
        return JobResult(ok=False, error=str(exc))
    return JobResult(ok=True)


def _release_branch_reservation(
    job: GitJob,
    *,
    remote_env: dict[str, str] | None,
    remote_config: tuple[str, ...],
    revalidate_remote: Callable[[], tuple[dict[str, str], tuple[str, ...]]] | None,
) -> JobResult:
    """Release one reserved branch after validating its immutable base."""
    branch_name = str(job.kwargs.get("branch") or "")
    base_sha = str(job.kwargs.get("base_sha") or "")
    repo_root_value = job.kwargs.get("repo_root")
    repo_root = Path(str(repo_root_value)) if repo_root_value else None
    if (
        not branch_name
        or not _is_full_commit_sha(base_sha)
        or repo_root is None
        or not repo_root.is_dir()
    ):
        return JobResult(
            ok=False,
            error="release_branch_reservation requires branch, base_sha, and repo_root",
        )
    released = delete_reserved_branch_if_unchanged(
        branch_name,
        base_sha,
        repo_root,
        timeout=job.timeout_s,
        env=remote_env,
        remote_config=remote_config,
        revalidate_remote=revalidate_remote,
    )
    return JobResult(ok=True, value=released)


def run_cleanup_job(  # noqa: C901 - cleanup validates independent durable receipts
    job: GitJob,
    *,
    worktree_manager_type: Any = WorktreeManager,
    remote_env: dict[str, str] | None = None,
    remote_config: tuple[str, ...] = (),
    revalidate_remote: Callable[[], tuple[dict[str, str], tuple[str, ...]]] | None = None,
) -> JobResult:
    """Run one validated worktree or reservation cleanup operation."""
    if job.op == "release_branch_reservation":
        return _release_branch_reservation(
            job,
            remote_env=remote_env,
            remote_config=remote_config,
            revalidate_remote=revalidate_remote,
        )

    if job.op != "remove_worktree":
        raise TypeError(f"unsupported cleanup Git operation: {job.op}")
    if job.kwargs.get("worktree_path"):
        worktree_path = Path(str(job.kwargs["worktree_path"]))
        repo_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
        issue_number = job.kwargs.get("issue_number")
        expected_branch = job.kwargs.get("expected_branch")
        expected_head = job.kwargs.get("expected_head")
        expected_detached = job.kwargs.get("expected_detached", False)
        if (
            isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
            or not _is_expected_worktree_path(
                worktree_path,
                repo_root=repo_root,
                issue_number=issue_number,
            )
            or (expected_branch is None and expected_head is None)
            or not isinstance(expected_detached, bool)
        ):
            return JobResult(ok=False, error="worktree cleanup identity is invalid")
        if job.kwargs.get("source_lane") is not None:
            return _run_source_lane_cleanup(
                job,
                worktree_path=worktree_path,
                repo_root=repo_root,
                issue_number=issue_number,
                expected_head=expected_head,
                expected_detached=expected_detached,
                worktree_manager_type=worktree_manager_type,
                remote_env=remote_env,
                remote_config=remote_config,
                revalidate_remote=revalidate_remote,
            )
        with file_lock(worktree_manager_type.git_metadata_lock_path(repo_root)):
            record = _worktree_record(
                worktree_path,
                repo_root=repo_root,
                timeout=job.timeout_s,
                worktree_manager_type=worktree_manager_type,
            )
            if record is None and not worktree_path.exists():
                session_failure = _codex_session_cleanup_failure(
                    worktree_path,
                    repository=job.transport_repository,
                    issue_number=issue_number,
                )
                if session_failure is not None:
                    return session_failure
                local_result = _cleanup_local_branch(
                    job.kwargs.get("local_branch_cleanup"),
                    expected_branch=expected_branch,
                    repo_root=repo_root,
                    timeout=job.timeout_s,
                )
                return local_result or JobResult(ok=True)
            if record is not None and not _record_matches_canonical_worktree(
                record,
                worktree_path,
            ):
                return JobResult(ok=False, error="worktree cleanup identity is invalid")
            if _ownership_changed(
                record,
                expected_branch=expected_branch,
                expected_head=expected_head,
                expected_detached=expected_detached,
            ):
                return JobResult(ok=False, error="worktree cleanup ownership changed")
            quarantine_authorized, quarantine_failure = _codex_session_quarantine_proof(
                worktree_path,
                repository=job.transport_repository,
                issue_number=issue_number,
            )
            if quarantine_failure is not None:
                return quarantine_failure
            status = run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=worktree_path,
                timeout=job.timeout_s,
            )
            if status.stdout.strip() and not quarantine_authorized:
                return JobResult(
                    ok=False,
                    error="worktree cleanup refused a dirty checkout",
                )
            session_failure = _codex_session_cleanup_failure(
                worktree_path,
                repository=job.transport_repository,
                issue_number=issue_number,
                require_quarantine=quarantine_authorized,
                detach_only=quarantine_authorized,
            )
            if session_failure is not None:
                return session_failure
            command = ["git", "worktree", "remove"]
            if quarantine_authorized:
                command.append("--force")
            command.append(str(worktree_path))
            run(command, cwd=repo_root, timeout=job.timeout_s)
            if quarantine_authorized:
                session_failure = _codex_session_cleanup_failure(
                    worktree_path,
                    repository=job.transport_repository,
                    issue_number=issue_number,
                    require_quarantine=True,
                )
                if session_failure is not None:
                    return session_failure
            run(
                ["git", "worktree", "prune"],
                cwd=repo_root,
                check=False,
                timeout=job.timeout_s,
            )
            local_result = _cleanup_local_branch(
                job.kwargs.get("local_branch_cleanup"),
                expected_branch=expected_branch,
                repo_root=repo_root,
                timeout=job.timeout_s,
            )
            if local_result is not None:
                return local_result
        return JobResult(ok=True)
    return JobResult(ok=False, error="cleanup requires an exact worktree path")
