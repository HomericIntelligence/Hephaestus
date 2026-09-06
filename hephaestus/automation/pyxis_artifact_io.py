"""Closed host I/O helpers for Linux Pyxis artifact verification."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_AUTHORITY_MAX_BYTES = 64 * 1024


class PyxisArtifactIOError(OSError):
    """Raised when a Pyxis artifact does not satisfy the host I/O contract."""


def effective_user_id() -> int:
    """Return the effective host user ID."""
    return os.geteuid()


def read_private_regular_file(path: Path) -> bytes:
    """Read a bounded owner-only regular file without following a symlink."""
    descriptor = -1
    try:
        descriptor = os.open(path.expanduser().absolute(), os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        _require_private_regular(before)
        chunks: list[bytes] = []
        remaining = _AUTHORITY_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or len(value) > _AUTHORITY_MAX_BYTES:
            raise PyxisArtifactIOError("private artifact changed or exceeded its bound")
        return value
    except OSError as exc:
        if isinstance(exc, PyxisArtifactIOError):
            raise
        raise PyxisArtifactIOError("private artifact read failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def stage_private_content_addressed_file(
    source: Path, destination_root: Path, expected_sha256: str
) -> Path:
    """Copy exact owner-read-only bytes to a private digest-named path."""
    root = destination_root.expanduser().absolute()
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise PyxisArtifactIOError("staging root is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) & 0o077
    ):
        raise PyxisArtifactIOError("staging root is not private")
    target = root / f"sha256-{expected_sha256}.sqsh"
    source_descriptor = -1
    target_descriptor = -1
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(source_descriptor)
        _require_private_regular(before)
        target_descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
        )
        digest = hashlib.sha256()
        while chunk := os.read(source_descriptor, 1024 * 1024):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(target_descriptor, view) :]
        os.fsync(target_descriptor)
        after = os.fstat(source_descriptor)
        if _identity(before) != _identity(after) or digest.hexdigest() != expected_sha256:
            raise PyxisArtifactIOError("source changed during staging")
        return target
    except OSError as exc:
        target.unlink(missing_ok=True)
        if isinstance(exc, PyxisArtifactIOError):
            raise
        raise PyxisArtifactIOError("content-addressed staging failed") from exc
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)


def validate_private_capacity_root(root: Path) -> tuple[Path, int]:
    """Return one private directory and its filesystem capacity."""
    candidate = root.expanduser()
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        capacity = os.statvfs(resolved)
    except OSError as exc:
        raise PyxisArtifactIOError("capacity root is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise PyxisArtifactIOError("capacity root is not private")
    return resolved, capacity.f_frsize * capacity.f_blocks


def _require_private_regular(metadata: os.stat_result) -> None:
    """Require owner-only, read-only regular-file metadata."""
    if not stat.S_ISREG(metadata.st_mode):
        raise PyxisArtifactIOError("artifact is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise PyxisArtifactIOError("artifact has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) & 0o277:
        raise PyxisArtifactIOError("artifact has unsafe permissions")


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    """Return the identity fields that must stay stable during one read."""
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


__all__ = [
    "PyxisArtifactIOError",
    "effective_user_id",
    "read_private_regular_file",
    "stage_private_content_addressed_file",
    "validate_private_capacity_root",
]
