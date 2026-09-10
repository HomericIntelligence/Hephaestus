"""Build jobs must stop their child processes within the job deadline."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool

_CHILD = """
import os
import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
(root / "child.pid").write_text(str(os.getpid()))
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    (root / "heartbeat").write_text(str(time.monotonic_ns()))
    time.sleep(0.02)
"""

_PARENT = """
import os
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
(root / "parent.pid").write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, "-c", sys.argv[2], str(root)])
child.wait(timeout=12)
"""


def _wait_for_file(path: Path, timeout_s: float = 3.0) -> None:
    """Wait for a child marker within a fixed test deadline."""
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        assert time.monotonic() < deadline, f"Child did not create {path.name}"
        threading.Event().wait(0.01)


def _kill_test_children(root: Path) -> None:
    """Stop the two controlled children if a regression leaves them active."""
    for name in ("child.pid", "parent.pid"):
        path = root / name
        if not path.exists():
            continue
        pid = int(path.read_text())
        assert pid > 1 and pid != os.getpid()
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="The test requires POSIX process groups")
@pytest.mark.parametrize("stop_reason", ["shutdown", "timeout"])
def test_submitted_build_job_stops_its_descendant(tmp_path: Path, stop_reason: str) -> None:
    """Shutdown and expiry publish a result after child cleanup."""
    shutdown = threading.Event()
    completion_q = CompletionQueue(maxsize=1)
    pool = WorkerPool(
        size=1,
        shutdown=shutdown,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    job = BuildTestJob(
        repo="example/project",
        cwd=tmp_path,
        argv=(sys.executable, "-c", _PARENT, str(tmp_path), _CHILD),
        timeout_s=8 if stop_reason == "shutdown" else 1,
    )
    try:
        handle = pool.submit(job, StageName.IMPLEMENTATION)
        _wait_for_file(tmp_path / "heartbeat")
        if stop_reason == "shutdown":
            pool.shutdown()
        completed_handle, result = completion_q.get(timeout=3)
        assert completed_handle is handle
        assert not result.ok
        if stop_reason == "shutdown":
            assert result.interrupted
        else:
            assert result.error == "timeout"
        heartbeat = (tmp_path / "heartbeat").read_text()
        threading.Event().wait(0.15)
        assert (tmp_path / "heartbeat").read_text() == heartbeat
    finally:
        _kill_test_children(tmp_path)
        pool.shutdown()


def test_build_job_does_not_start_after_preparation_uses_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime preparation and child execution use the same deadline."""
    from hephaestus.automation.pipeline import worker_pool

    def prepare_environment(_cwd: Path) -> dict[str, str]:
        threading.Event().wait(1.1)
        return dict(os.environ)

    monkeypatch.setattr(worker_pool, "build_python_phase_env", prepare_environment)
    completion_q = CompletionQueue(maxsize=1)
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    marker = tmp_path / "started"
    job = BuildTestJob(
        repo="example/project",
        cwd=tmp_path,
        argv=(
            sys.executable,
            "-c",
            "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()",
            str(marker),
        ),
        timeout_s=1,
    )
    try:
        pool.submit(job, StageName.IMPLEMENTATION)
        _handle, result = completion_q.get(timeout=3)
    finally:
        pool.shutdown()

    assert not result.ok
    assert result.error == "timeout"
    assert not marker.exists()
