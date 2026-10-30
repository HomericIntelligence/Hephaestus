"""Tests for the live agent subprocess-group registry (#2059, #2966)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import pytest

from hephaestus.automation import subprocess_registry as reg

_IGNORES_SIGTERM = (
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); time.sleep(60)"
)
_ACCEPTS_SIGTERM = "import time; print('ready', flush=True); time.sleep(60)"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Each test starts and ends with an empty, open registry."""
    reg._shutdown_started = False
    reg.terminate_all()
    reg.force_kill()
    yield
    reg.terminate_all()
    reg.force_kill()
    reg._shutdown_started = False


skip_no_pg = pytest.mark.skipif(not reg.supported(), reason="process groups unavailable")


def _spawn(script: str) -> subprocess.Popen[str]:
    """Start a child that leads its own process group and wait for readiness."""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


class _Owner(threading.Thread):
    """Hold one process group the way a worker holds a live child."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        super().__init__(daemon=True)
        self.proc = proc
        self.released = threading.Event()

    def run(self) -> None:
        with reg.track_process_group(self.proc.pid):
            while self.proc.poll() is None:
                time.sleep(0.01)
        self.released.set()


def _held(proc: subprocess.Popen[str]) -> _Owner:
    """Start an owner thread and wait until its group is registered."""
    owner = _Owner(proc)
    owner.start()
    deadline = time.monotonic() + 10
    while reg.live_count() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert reg.live_count() == 1, "owner did not register its process group"
    return owner


def test_empty_registry_signals_nothing() -> None:
    """Every sweep is a safe no-op when nothing is tracked."""
    assert reg.terminate_all() == 0
    assert reg.force_kill() == ()
    assert reg.escalate() == ()
    assert reg.live_count() == 0


@skip_no_pg
def test_track_registers_and_unregisters_on_exit() -> None:
    """A tracked pid is live inside the block and gone after it."""
    proc = _spawn(_ACCEPTS_SIGTERM)
    try:
        with reg.track_process_group(proc.pid):
            assert reg.live_count() == 1
        # Context exit unregisters even though the child is still alive.
        assert reg.live_count() == 0
    finally:
        _stop(proc)


@skip_no_pg
def test_terminate_all_releases_a_group_that_honours_sigterm() -> None:
    """A child that exits on SIGTERM releases its group inside the bound."""
    proc = _spawn(_ACCEPTS_SIGTERM)
    owner = _held(proc)
    try:
        assert reg.terminate_all(wait_s=10.0) == 1

        assert owner.released.wait(timeout=10)
        assert reg.live_count() == 0
        assert proc.returncode is not None
        assert proc.returncode != 0  # terminated by signal
    finally:
        _stop(proc)


@skip_no_pg
def test_terminate_all_keeps_a_group_that_ignores_sigterm() -> None:
    """An unresponsive child stays a kill target after the first signal."""
    proc = _spawn(_IGNORES_SIGTERM)
    pgid = os.getpgid(proc.pid)
    owner = _held(proc)
    try:
        assert reg.terminate_all() == 1

        time.sleep(0.2)
        assert reg.live_count() == 1  # still owned, so a later sweep can kill it
        assert proc.poll() is None
        assert not owner.released.is_set()

        assert reg.escalate(grace_s=0.3) == (pgid,)
        assert owner.released.wait(timeout=10)
    finally:
        _stop(proc)


@skip_no_pg
def test_escalate_sigkills_a_group_that_ignores_sigterm() -> None:
    """The bounded escalation stops a child that survives SIGTERM."""
    proc = _spawn(_IGNORES_SIGTERM)
    pgid = os.getpgid(proc.pid)
    owner = _held(proc)
    started = time.monotonic()
    try:
        killed = reg.escalate(grace_s=0.3)
        assert owner.released.wait(timeout=10)
    finally:
        _stop(proc)

    assert killed == (pgid,)
    assert reg.live_count() == 0
    assert time.monotonic() - started < 10
    assert proc.returncode != 0


@skip_no_pg
def test_escalate_leaves_a_well_behaved_child_alone() -> None:
    """A child that honours SIGTERM never receives SIGKILL."""
    proc = _spawn(_ACCEPTS_SIGTERM)
    owner = _held(proc)
    try:
        assert reg.escalate(grace_s=10.0) == ()
        assert owner.released.wait(timeout=10)
    finally:
        _stop(proc)
    assert reg.live_count() == 0


@skip_no_pg
def test_force_kill_stops_and_forgets_a_tracked_group() -> None:
    """A repeated teardown can SIGKILL what the first sweep left behind."""
    proc = _spawn(_IGNORES_SIGTERM)
    pgid = os.getpgid(proc.pid)
    owner = _held(proc)
    try:
        assert reg.force_kill() == (pgid,)
        assert owner.released.wait(timeout=10)
        assert proc.returncode != 0
        assert reg.live_count() == 0
        # A repeat call has no target and does not fail.
        assert reg.force_kill() == ()
    finally:
        _stop(proc)


@skip_no_pg
def test_late_registration_is_owned_by_the_next_sweep() -> None:
    """A child that starts after teardown begins is still a kill target."""
    reg.begin_shutdown()
    assert reg.shutdown_started() is True
    proc = _spawn(_IGNORES_SIGTERM)
    pgid = os.getpgid(proc.pid)
    reg._register(pgid)
    try:
        assert reg.live_count() == 1
        assert reg.force_kill() == (pgid,)
        proc.wait(timeout=10)
        assert proc.returncode != 0
    finally:
        _stop(proc)


@skip_no_pg
def test_terminate_all_skips_already_exited_group() -> None:
    """A group whose leader already exited is skipped without error."""
    proc = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    proc.wait(timeout=5)
    reg._register(pgid)

    # No exception even though the group is gone; count may be 0 or 1.
    signalled = reg.terminate_all()

    assert signalled in (0, 1)
    assert reg.live_count() == (0 if signalled == 0 else 1)


def test_no_process_groups_platform_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a platform without killpg, every entry point is inert."""
    monkeypatch.setattr(reg, "_HAVE_KILLPG", False)
    with reg.track_process_group(999999):
        assert reg.live_count() == 0
    assert reg.terminate_all() == 0
    assert reg.escalate() == ()
    assert reg.force_kill() == ()
