"""Track subprocess groups that a host can terminate during shutdown."""

from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_HAVE_KILLPG = hasattr(os, "killpg") and hasattr(os, "getpgid")
_lock = threading.Lock()
_live_pgids: set[int] = set()


def supported() -> bool:
    """Return true when POSIX process-group termination is available."""
    return _HAVE_KILLPG


def _register(pgid: int) -> None:
    """Register one process group for compatibility with direct tests."""
    with _lock:
        _live_pgids.add(pgid)


def _unregister(pgid: int) -> None:
    """Remove one process group from the registry."""
    with _lock:
        _live_pgids.discard(pgid)


@contextmanager
def track_process_group(pid: int) -> Iterator[None]:
    """Track a child that is the leader of its own process group."""
    if not _HAVE_KILLPG:
        yield
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        yield
        return
    _register(pgid)
    try:
        yield
    finally:
        _unregister(pgid)


def terminate_all(sig: int = signal.SIGTERM) -> int:
    """Signal all tracked process groups and return the signal count."""
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
            continue
        except OSError as exc:  # pragma: no cover - defensive logging
            logger.warning("failed to signal process group %s: %s", pgid, exc)
    if signalled:
        logger.info("terminated %d in-flight subprocess group(s)", signalled)
    return signalled


def live_count() -> int:
    """Return the number of tracked process groups."""
    with _lock:
        return len(_live_pgids)
