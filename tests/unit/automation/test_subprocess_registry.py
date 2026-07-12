"""Tests for the live agent subprocess-group registry (#2059)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from hephaestus.automation import subprocess_registry as reg


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Each test starts and ends with an empty registry."""
    reg.terminate_all()  # clear any leakage from prior tests (no-op if empty)
    yield
    reg.terminate_all()


skip_no_pg = pytest.mark.skipif(not reg.supported(), reason="process groups unavailable")


def test_terminate_all_empty_returns_zero() -> None:
    """Signalling with nothing tracked is a safe no-op."""
    assert reg.terminate_all() == 0
    assert reg.live_count() == 0


@skip_no_pg
def test_track_registers_and_unregisters_on_exit() -> None:
    """A tracked pid is live inside the block and gone after it."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        with reg.track_process_group(proc.pid):
            assert reg.live_count() == 1
        # Context exit unregisters even though the child is still alive.
        assert reg.live_count() == 0
    finally:
        proc.kill()
        proc.wait(timeout=5)


@skip_no_pg
def test_terminate_all_kills_tracked_group() -> None:
    """terminate_all sends SIGTERM to the tracked group, killing the child fast."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    reg._register(os.getpgid(proc.pid))
    assert reg.live_count() == 1

    signalled = reg.terminate_all()

    assert signalled == 1
    assert reg.live_count() == 0  # registry cleared
    # Child dies from SIGTERM promptly (not after its 30s sleep).
    proc.wait(timeout=5)
    assert proc.returncode is not None
    assert proc.returncode != 0  # terminated by signal


@skip_no_pg
def test_terminate_all_skips_already_exited_group() -> None:
    """A group whose leader already exited is skipped without error."""
    proc = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    proc.wait(timeout=5)
    # Give the OS a moment to reap the group leader.
    time.sleep(0.05)
    reg._register(pgid)

    # No exception even though the group is gone; count may be 0 or 1.
    signalled = reg.terminate_all()

    assert signalled in (0, 1)
    assert reg.live_count() == 0


def test_track_no_pg_platform_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a platform without killpg, tracking is a no-op and terminate returns 0."""
    monkeypatch.setattr(reg, "_HAVE_KILLPG", False)
    with reg.track_process_group(999999):
        assert reg.live_count() == 0
    assert reg.terminate_all() == 0
