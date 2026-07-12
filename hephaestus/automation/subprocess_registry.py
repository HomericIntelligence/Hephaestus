"""Thread-safe registry of live agent subprocess groups (#2059).

The pipeline worker pool runs each agent (``claude``) invocation on a
``ThreadPoolExecutor`` thread that blocks in :func:`subprocess.run`/``Popen``.
``ThreadPoolExecutor.shutdown(cancel_futures=True)`` cancels only *un-started*
futures; a job already blocked in ``communicate()`` keeps running to completion
or timeout, holding a non-daemon worker thread and (via the interpreter's
``atexit`` join) the whole process open — the ~19-minute leak reported in #2059.

This registry lets the spawning code publish each live child's process-group id
so a teardown path (``WorkerPool.shutdown``) can send it ``SIGTERM`` and free
the worker promptly. It is a *shared* seam: :mod:`hephaestus.automation.claude_invoke`
registers, the worker pool terminates. Registration is opt-in via
:func:`track_process_group` (a context manager) so non-pipeline callers are
unaffected.

Windows has no process groups / ``killpg``; there registration is a no-op and
:func:`terminate_all` returns 0 (the pool falls back to its prior behavior).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

#: True when the platform supports POSIX process groups + killpg.
_HAVE_KILLPG = hasattr(os, "killpg") and hasattr(os, "getpgid")

_lock = threading.Lock()
#: Live child process-group ids currently tracked.
_live_pgids: set[int] = set()


def supported() -> bool:
    """Return True when process-group tracking/termination is available."""
    return _HAVE_KILLPG


def _register(pgid: int) -> None:
    with _lock:
        _live_pgids.add(pgid)


def _unregister(pgid: int) -> None:
    with _lock:
        _live_pgids.discard(pgid)


@contextmanager
def track_process_group(pid: int) -> Iterator[None]:
    """Track *pid*'s process group for the duration of the ``with`` block.

    *pid* must be the leader of its own process group (spawn the child with
    ``start_new_session=True`` so its pgid equals its pid). On unsupported
    platforms this is a no-op. The group is always unregistered on exit, so a
    child that finishes normally is never left in the registry.

    Args:
        pid: The child process id (also its process-group id).

    """
    if not _HAVE_KILLPG:
        yield
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        # Child already gone (raced to exit) — nothing to track.
        yield
        return
    _register(pgid)
    try:
        yield
    finally:
        _unregister(pgid)


def terminate_all(sig: int = signal.SIGTERM) -> int:
    """Signal every tracked process group and clear the registry.

    Idempotent and safe to call with nothing tracked (returns 0). Groups whose
    leader has already exited are skipped without error. Returns the number of
    groups signalled — the worker pool logs this on teardown so a leak is
    visible.

    Args:
        sig: The signal to deliver (default ``SIGTERM``).

    Returns:
        The count of process groups that were signalled.

    """
    if not _HAVE_KILLPG:
        return 0
    with _lock:
        pgids = list(_live_pgids)
        _live_pgids.clear()
    signalled = 0
    for pgid in pgids:
        try:
            os.killpg(pgid, sig)
            signalled += 1
        except ProcessLookupError:
            # Group already gone — fine.
            continue
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("failed to signal process group %s: %s", pgid, exc)
    if signalled:
        logger.info("terminated %d in-flight agent process group(s) on teardown", signalled)
    return signalled


def live_count() -> int:
    """Return the number of currently tracked process groups (for tests/diagnostics)."""
    with _lock:
        return len(_live_pgids)
