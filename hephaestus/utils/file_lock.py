#!/usr/bin/env python3
"""Cross-process advisory file lock for Hephaestus.

Provides :func:`file_lock`, a context manager that serializes a critical
section across separate processes using ``fcntl.flock`` on a sentinel file.
An in-process ``threading.Lock`` only coordinates threads of one interpreter;
this helper is for the case where independent ``subprocess.run`` children (e.g.
the issue-major automation loop's per-issue phase subprocesses) must not race on
a shared on-disk resource such as a git worktree path or a state-record sweep.

The lock is advisory (cooperating processes must all use it) and POSIX-only.
On platforms without ``fcntl`` (Windows) it degrades to a no-op so callers stay
portable; the underlying race simply isn't guarded there.

Extracted from the previously-inline ``fcntl.flock`` patterns in
``hephaestus.github.rate_limit`` and ``hephaestus.automation.advise_runner`` so
there is a single, tested primitive (DRY).
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TextIO


class LockUnavailableError(RuntimeError):
    """Raised by :func:`file_lock` with ``blocking=False`` when held elsewhere."""


class ExclusiveLockUnavailableError(LockUnavailableError):
    """Raised when the host cannot provide a required exclusive file lock."""


def _open_secure_lock_file(path: Path) -> TextIO:
    """Open ``path`` for locking without following symlinks.

    Args:
        path: Sentinel file to open (created if absent, mode ``0o600``).

    Returns:
        An open file object whose descriptor backs the advisory lock.

    Raises:
        RuntimeError: If ``path`` already exists and is a symlink.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked lock file: {path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "r+")


def _require_exclusive_at_capabilities(name: str) -> None:
    """Require all primitives for safe descriptor-relative lock admission."""
    if (
        not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or not hasattr(os, "geteuid")
    ):
        raise ExclusiveLockUnavailableError(
            f"Exclusive descriptor-relative locking is unavailable for: {name}"
        )


def _open_secure_lock_file_at(parent_fd: int, name: str, *, require_exclusive: bool) -> IO[str]:
    """Create or verify one lock relative to an existing bound directory."""
    if not name or Path(name).name != name:
        raise ValueError("lock name must be one path component")
    if require_exclusive:
        _require_exclusive_at_capabilities(name)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
    except FileExistsError:
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ExclusiveLockUnavailableError(
                f"Descriptor-relative lock entry is unsafe: {name}"
            ) from exc
    except OSError as exc:
        raise ExclusiveLockUnavailableError(
            f"Descriptor-relative lock entry is unavailable: {name}"
        ) from exc
    try:
        metadata = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        expected_owner = os.geteuid() if hasattr(os, "geteuid") else metadata.st_uid
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != expected_owner
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ExclusiveLockUnavailableError(f"Descriptor-relative lock entry is unsafe: {name}")
        return os.fdopen(fd, "r+")
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def file_lock_at(
    parent_fd: int,
    name: str,
    *,
    blocking: bool = True,
    require_exclusive: bool = False,
) -> Iterator[None]:
    """Hold a lock file relative to one existing bound directory."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows path
        if require_exclusive:
            raise ExclusiveLockUnavailableError(
                f"Exclusive file locking is unavailable for: {name}"
            ) from None
        yield
        return
    fh = _open_secure_lock_file_at(
        parent_fd,
        name,
        require_exclusive=require_exclusive,
    )
    try:
        mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fh.fileno(), mode)
        except OSError as exc:
            if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise LockUnavailableError(f"Lock already held: {name}") from exc
            unsupported = {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
            if require_exclusive and exc.errno in unsupported:
                raise ExclusiveLockUnavailableError(
                    f"Exclusive file locking is unavailable for: {name}"
                ) from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


@contextmanager
def file_lock(
    path: Path, *, blocking: bool = True, require_exclusive: bool = False
) -> Iterator[None]:
    """Hold an exclusive cross-process advisory lock on ``path``.

    Acquires ``fcntl.flock(LOCK_EX)`` on a sentinel file for the duration of the
    ``with`` block, releasing it (and closing the descriptor) on exit even if the
    body raises. The sentinel file is intentionally NOT unlinked while the lock
    is held — deleting it would let a second acquirer create a fresh inode and
    lock that instead, defeating the mutual exclusion (inode-reuse hazard).

    Args:
        path: Sentinel file backing the lock. Created if absent.
        blocking: When True (default) block until the lock is free. When False,
            raise :class:`LockUnavailableError` immediately if another holder exists.
        require_exclusive: When True, raise :class:`LockUnavailableError` if
            this platform has no reliable ``fcntl`` lock rather than degrading
            to a no-op. Use for non-idempotent external side effects.

    Yields:
        None. Use as ``with file_lock(path): ...``.

    Raises:
        LockUnavailableError: ``blocking=False`` and the lock is already held,
            or ``require_exclusive=True`` without a supported lock primitive.
        RuntimeError: ``path`` exists and is a symlink.

    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows path
        if require_exclusive:
            raise ExclusiveLockUnavailableError(
                f"Exclusive file locking is unavailable on this platform: {path}"
            ) from None
        # No advisory locking available; degrade to a no-op so callers stay
        # portable. The guarded race is simply unprotected on this platform.
        yield
        return

    fh = _open_secure_lock_file(path)
    try:
        mode = fcntl.LOCK_EX
        if not blocking:
            mode |= fcntl.LOCK_NB
        try:
            fcntl.flock(fh.fileno(), mode)
        except OSError as exc:
            if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise LockUnavailableError(f"Lock already held: {path}") from exc
            unsupported_errors = {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
            if require_exclusive and exc.errno in unsupported_errors:
                raise ExclusiveLockUnavailableError(
                    f"Exclusive file locking is unavailable on this platform: {path}"
                ) from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
