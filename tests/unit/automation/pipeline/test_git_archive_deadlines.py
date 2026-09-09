"""Test archive capture within the active worker deadline and cancellation."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import git_runtime, worktree_snapshot
from hephaestus.automation.pipeline import worker_pool


@pytest.mark.parametrize("stop_kind", ["deadline", "cancel"])
def test_silent_archive_child_stops_with_the_active_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_kind: str
) -> None:
    """A child with no output must stop before test cleanup terminates it."""
    shutdown = threading.Event()
    spawned = threading.Event()
    completed = threading.Event()
    children: list[subprocess.Popen[bytes]] = []
    errors: list[BaseException] = []
    real_popen = subprocess.Popen

    def spawn_silent_child(argv: tuple[str, ...], **options: Any) -> subprocess.Popen[bytes]:
        assert argv == ("git", "archive", "--format=tar", "a" * 40)
        process = real_popen(
            (sys.executable, "-I", "-c", "import time; time.sleep(30)"),
            **options,
        )
        children.append(process)
        spawned.set()
        return process

    def capture_archive() -> None:
        try:
            remaining = 0.5 if stop_kind == "deadline" else 30.0
            with git_runtime.operation_deadline(time.monotonic() + remaining, shutdown=shutdown):
                worker_pool._bounded_git_archive(tmp_path, "a" * 40, timeout_s=30)
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    monkeypatch.setattr(subprocess, "Popen", spawn_silent_child)
    capture = threading.Thread(target=capture_archive, daemon=True)
    capture.start()
    try:
        assert spawned.wait(3), "The archive child did not start"
        if stop_kind == "cancel":
            shutdown.set()
        stopped_without_cleanup = completed.wait(2)
        reaped_without_cleanup = all(child.returncode is not None for child in children)
    finally:
        shutdown.set()
        for child in children:
            if child.poll() is None:
                with suppress(ProcessLookupError):
                    child.kill()
            child.wait(timeout=5)
        capture.join(timeout=5)
        if not capture.is_alive():
            for child in children:
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()

    assert not capture.is_alive(), "The archive reader did not stop after child cleanup"
    assert stopped_without_cleanup, "Silent archive capture ignored the active operation stop"
    assert reaped_without_cleanup, "Archive capture did not reap its stopped child"
    assert len(errors) == 1
    expected_error = subprocess.TimeoutExpired if stop_kind == "deadline" else InterruptedError
    assert isinstance(errors[0], expected_error)


@pytest.mark.parametrize("selector_supported", [True, False])
@pytest.mark.parametrize("outcome", ["binary", "limit", "failed"])
def test_archive_capture_preserves_bytes_limits_and_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector_supported: bool,
    outcome: str,
) -> None:
    """Both readers preserve binary output and the archive failure categories."""
    payload = bytes(range(256))
    code = 3 if outcome == "failed" else 0
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({payload!r}); "
        "sys.stderr.write('x' * 600 + 'archive failed'); "
        f"sys.exit({code})"
    )
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn_archive_child(argv: tuple[str, ...], **options: Any) -> subprocess.Popen[bytes]:
        assert argv == ("git", "archive", "--format=tar", "a" * 40)
        child = real_popen((sys.executable, "-I", "-c", script), **options)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn_archive_child)
    monkeypatch.setattr(
        worktree_snapshot, "_subprocess_pipe_selector_supported", lambda: selector_supported
    )
    limit = len(payload) - 1 if outcome == "limit" else len(payload)
    monkeypatch.setattr(worker_pool, "_HOST_VERIFICATION_ARCHIVE_MAX_BYTES", limit)
    if outcome == "binary":
        archive, _stderr = worker_pool._bounded_git_archive(tmp_path, "a" * 40, timeout_s=5)
        assert archive == payload
    else:
        with pytest.raises(worker_pool._HostVerificationBoundaryError) as raised:
            worker_pool._bounded_git_archive(tmp_path, "a" * 40, timeout_s=5)
        if outcome == "limit":
            assert str(raised.value) == "git_archive_size_limit_exceeded"
        else:
            expected_tail = ("x" * 600 + "archive failed")[-500:]
            assert str(raised.value) == f"immutable_source_snapshot_failed:{expected_tail}"
    assert len(children) == 1
    assert children[0].returncode is not None
