"""Read rebase conflict files through bounded, stable descriptors."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_CONFLICT_HUNK_MAX = 4000


_CONFLICT_FILE_MAX_BYTES = _CONFLICT_HUNK_MAX * 4


class _RebaseConflictContextError(RuntimeError):
    """Raised when conflict context is incomplete or unsafe to retain."""


def _validated_conflict_path(cwd: Path, path: str) -> Path:
    """Validate every path component before a conflict file read."""
    target = cwd / path
    try:
        target.relative_to(cwd)
        current = target
        while True:
            if current.is_symlink():
                raise _RebaseConflictContextError("conflict source path is unsafe")
            if current == cwd:
                break
            parent = current.parent
            if parent == current:
                raise _RebaseConflictContextError("conflict source path is unsafe")
            current = parent
    except (OSError, ValueError) as exc:
        raise _RebaseConflictContextError("conflict source path is unsafe") from exc
    return target


def _open_bounded_conflict_file(parent_fd: int, name: str) -> int:
    """Open one conflict file through its bound parent descriptor."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if not isinstance(nofollow, int) or not nofollow or not isinstance(nonblock, int):
        raise _RebaseConflictContextError("secure conflict source reads are unavailable")
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | nofollow | nonblock,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _RebaseConflictContextError("conflict source path is unsafe") from exc


_ConflictFileIdentity = tuple[int, int, int, int, int]


_ConflictPathBinding = tuple[int, str, int, _ConflictFileIdentity]


def _conflict_file_identity(info: os.stat_result) -> _ConflictFileIdentity:
    """Return metadata that proves one file stayed unchanged."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_conflict_file_chunks(descriptor: int) -> bytes:
    """Read at most the configured number of bytes from an open file."""
    chunks: list[bytes] = []
    size = 0
    while size < _CONFLICT_FILE_MAX_BYTES:
        chunk = os.read(descriptor, _CONFLICT_FILE_MAX_BYTES - size)
        if not chunk:
            break
        size += len(chunk)
        if size > _CONFLICT_FILE_MAX_BYTES:
            raise _RebaseConflictContextError(
                f"conflict source exceeds {_CONFLICT_HUNK_MAX} characters"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _read_bounded_conflict_descriptor(descriptor: int) -> bytes:
    """Read and verify one open conflict file without exceeding its bound."""
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise _RebaseConflictContextError("conflict source path is unsafe")
    if before.st_size > _CONFLICT_FILE_MAX_BYTES:
        raise _RebaseConflictContextError(
            f"conflict source exceeds {_CONFLICT_HUNK_MAX} characters"
        )
    raw = _read_conflict_file_chunks(descriptor)
    after = os.fstat(descriptor)
    if after.st_size > _CONFLICT_FILE_MAX_BYTES:
        raise _RebaseConflictContextError(
            f"conflict source exceeds {_CONFLICT_HUNK_MAX} characters"
        )
    if _conflict_file_identity(before) != _conflict_file_identity(after):
        raise _RebaseConflictContextError("conflict source changed during read")
    return raw


def _open_conflict_descriptor_chain(
    cwd: Path,
    parts: tuple[str, ...],
    validated_root: os.stat_result,
) -> tuple[
    list[int],
    int,
    int,
    list[_ConflictPathBinding],
    _ConflictFileIdentity,
    int,
]:
    """Open a conflict path and retain each directory-name binding."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or not nofollow:
        raise _RebaseConflictContextError("secure conflict source reads are unavailable")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | nofollow | os.O_DIRECTORY
    descriptors: list[int] = []
    try:
        try:
            root_fd = os.open(cwd, directory_flags)
        except FileNotFoundError as exc:
            raise _RebaseConflictContextError("conflict source changed during read") from exc
        descriptors.append(root_fd)
        root_identity = _conflict_file_identity(os.fstat(root_fd))
        if _conflict_file_identity(validated_root) != root_identity:
            raise _RebaseConflictContextError("conflict source changed during read")
        bindings: list[_ConflictPathBinding] = []
        parent_fd = root_fd
        for component in parts[:-1]:
            try:
                named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise _RebaseConflictContextError("conflict source changed during read") from exc
            if not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode):
                raise _RebaseConflictContextError("conflict source path is unsafe")
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise _RebaseConflictContextError("conflict source changed during read") from exc
            descriptors.append(child_fd)
            opened = os.fstat(child_fd)
            expected = _conflict_file_identity(opened)
            if _conflict_file_identity(named) != expected:
                raise _RebaseConflictContextError("conflict source changed during read")
            bindings.append((parent_fd, component, child_fd, expected))
            parent_fd = child_fd

        descriptor = _open_bounded_conflict_file(parent_fd, parts[-1])
        descriptors.append(descriptor)
        return (
            descriptors,
            descriptor,
            parent_fd,
            bindings,
            root_identity,
            directory_flags,
        )
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _revalidate_conflict_descriptor_chain(
    cwd: Path,
    leaf_name: str,
    descriptor: int,
    parent_fd: int,
    bindings: list[_ConflictPathBinding],
    root_identity: _ConflictFileIdentity,
    directory_flags: int,
) -> None:
    """Confirm that the conflict path still names all retained descriptors."""
    try:
        current_leaf = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
        if _conflict_file_identity(current_leaf) != _conflict_file_identity(os.fstat(descriptor)):
            raise _RebaseConflictContextError("conflict source changed during read")
        for bound_parent, component, child_fd, expected in bindings:
            current = os.stat(component, dir_fd=bound_parent, follow_symlinks=False)
            if (
                _conflict_file_identity(current) != expected
                or _conflict_file_identity(os.fstat(child_fd)) != expected
            ):
                raise _RebaseConflictContextError("conflict source changed during read")
        current_root_fd = os.open(cwd, directory_flags)
        try:
            if _conflict_file_identity(os.fstat(current_root_fd)) != root_identity:
                raise _RebaseConflictContextError("conflict source changed during read")
        finally:
            os.close(current_root_fd)
    except _RebaseConflictContextError:
        raise
    except OSError as exc:
        raise _RebaseConflictContextError("conflict source changed during read") from exc


def _read_bounded_conflict_file(cwd: Path, path: str) -> bytes:
    """Read one regular conflict file without following links or exceeding its bound."""
    try:
        validated_root = cwd.lstat()
        if not stat.S_ISDIR(validated_root.st_mode) or stat.S_ISLNK(validated_root.st_mode):
            raise _RebaseConflictContextError("conflict source path is unsafe")
        target = _validated_conflict_path(cwd, path)
        relative = target.relative_to(cwd)
        parts = relative.parts
        if not parts or any(component in {"", ".", ".."} for component in parts):
            raise _RebaseConflictContextError("conflict source path is unsafe")
        (
            descriptors,
            descriptor,
            parent_fd,
            bindings,
            root_identity,
            directory_flags,
        ) = _open_conflict_descriptor_chain(cwd, parts, validated_root)
        try:
            raw = _read_bounded_conflict_descriptor(descriptor)
            _revalidate_conflict_descriptor_chain(
                cwd,
                parts[-1],
                descriptor,
                parent_fd,
                bindings,
                root_identity,
                directory_flags,
            )
            return raw
        finally:
            for open_descriptor in reversed(descriptors):
                os.close(open_descriptor)
    except _RebaseConflictContextError:
        raise
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise _RebaseConflictContextError("conflict source cannot be read") from exc
