"""Track subprocess groups that a host can terminate during shutdown.

A group stays registered until its owner releases it, or until this module
sends ``SIGKILL``. A child that ignores ``SIGTERM`` therefore stays a kill
target for a later escalation instead of leaking out of the registry.

The owner is the code that started the child. It releases the group in the
``finally`` block of :func:`track_process_group` after the child exits. That
release is the signal this module waits on, because it is the only portable
proof that the group is gone: a killed child stays visible as a zombie until
its owner reaps it, so a signal probe cannot tell a zombie from a live process.
The module never calls ``waitpid``, because a competing reap would make the
owning ``Popen`` report success for a child that a signal killed.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_HAVE_KILLPG = hasattr(os, "killpg") and hasattr(os, "getpgid")
_lock = threading.Lock()
_live_pgids: set[int] = set()
_shutdown_started = False

#: Time a child gets to honour ``SIGTERM`` before its group receives ``SIGKILL``.
TERM_GRACE_S = 2.0
_POLL_S = 0.02


def supported() -> bool:
    """Return true when POSIX process-group termination is available."""
    return _HAVE_KILLPG


def begin_shutdown() -> None:
    """Mark the start of teardown and report every later registration.

    A child that starts after this call stays in the live set. Each sweep reads
    that set again, so the final sweep still owns the late child.
    """
    global _shutdown_started
    with _lock:
        _shutdown_started = True


def shutdown_started() -> bool:
    """Return true after :func:`begin_shutdown`."""
    with _lock:
        return _shutdown_started


def _register(pgid: int) -> None:
    """Register one process group for compatibility with direct tests."""
    with _lock:
        late = _shutdown_started
        _live_pgids.add(pgid)
    if late:
        logger.warning("process group %s started during teardown; final sweep owns it", pgid)


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


def _await_released(pgids: Iterable[int], wait_s: float) -> list[int]:
    """Wait for each owner to release its group and return those still held."""
    remaining = list(pgids)
    deadline = time.monotonic() + wait_s
    while True:
        with _lock:
            remaining = [pgid for pgid in remaining if pgid in _live_pgids]
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(_POLL_S)


def _kill(pgids: Iterable[int]) -> list[int]:
    """Send ``SIGKILL`` and release each group that received it."""
    killed: list[int] = []
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed.append(pgid)
        except ProcessLookupError:
            pass
        except OSError as exc:  # pragma: no cover - defensive logging
            logger.warning("failed to kill process group %s: %s", pgid, exc)
            continue
        # SIGKILL cannot be ignored or delayed, so ownership can end here.
        _unregister(pgid)
    return killed


def terminate_all(sig: int = signal.SIGTERM, *, wait_s: float = 0.0) -> int:
    """Signal every tracked group and return how many received the signal.

    ``wait_s`` bounds the time spent waiting for the owners to release their
    groups. A group that is still held stays registered, so a later
    :func:`escalate` or :func:`force_kill` still has a target.
    """
    if not _HAVE_KILLPG:
        return 0
    with _lock:
        pgids = sorted(_live_pgids)
    signalled = 0
    for pgid in pgids:
        try:
            os.killpg(pgid, sig)
            signalled += 1
        except ProcessLookupError:
            _unregister(pgid)
        except OSError as exc:  # pragma: no cover - defensive logging
            logger.warning("failed to signal process group %s: %s", pgid, exc)
    if wait_s > 0:
        _await_released(pgids, wait_s)
    if signalled:
        logger.info("signalled %d in-flight subprocess group(s)", signalled)
    return signalled


def escalate(grace_s: float = TERM_GRACE_S) -> tuple[int, ...]:
    """Stop every owned group: ``SIGTERM``, a bounded wait, then ``SIGKILL``.

    Returns the groups that were still held when the grace period ended and
    therefore received ``SIGKILL``. A child that exits on ``SIGTERM`` releases
    its group immediately, so its teardown stays inside the grace period.
    """
    if not _HAVE_KILLPG:
        return ()
    terminate_all(signal.SIGTERM)
    with _lock:
        watched = sorted(_live_pgids)
    if not watched:
        return ()
    survivors = _await_released(watched, grace_s) if grace_s > 0 else watched
    if not survivors:
        return ()
    killed = _kill(survivors)
    if killed:
        logger.warning(
            "killed %d subprocess group(s) that ignored SIGTERM: %s", len(killed), killed
        )
    return tuple(killed)


def force_kill(*, wait_s: float = 0.0) -> tuple[int, ...]:
    """Send ``SIGKILL`` to every tracked group and return the groups it killed.

    A repeated teardown calls this again. The call is safe from a signal
    handler when ``wait_s`` is zero, because it sends signals and does not wait.
    """
    if not _HAVE_KILLPG:
        return ()
    with _lock:
        pgids = sorted(_live_pgids)
    if not pgids:
        return ()
    if wait_s > 0:
        pgids = _await_released(pgids, wait_s)
        if not pgids:
            return ()
    return tuple(_kill(pgids))


def live_count() -> int:
    """Return the number of tracked process groups."""
    with _lock:
        return len(_live_pgids)
