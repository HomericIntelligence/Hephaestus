#!/usr/bin/env python3
"""Cross-process advisory file lock for ProjectHephaestus.

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

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


class LockUnavailableError(RuntimeError):
    """Raised by :func:`file_lock` with ``blocking=False`` when held elsewhere."""


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


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
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

    Yields:
        None. Use as ``with file_lock(path): ...``.

    Raises:
        LockUnavailableError: ``blocking=False`` and the lock is already held.
        RuntimeError: ``path`` exists and is a symlink.

    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows path
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
            if not blocking:
                raise LockUnavailableError(f"Lock already held: {path}") from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
