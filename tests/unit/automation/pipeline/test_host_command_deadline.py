"""The host validation child must retain the current operation deadline."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.automation import git_runtime
from hephaestus.automation.pipeline import worker_pool


@pytest.mark.parametrize("stop_reason", ["shutdown", "timeout"])
def test_host_check_does_not_start_after_pre_launch_stops_the_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_reason: str
) -> None:
    """Check cancellation and the remaining time after launch preparation."""
    shutdown = threading.Event()
    start = Mock(side_effect=AssertionError("The stopped operation started a child"))
    monkeypatch.setattr(subprocess, "Popen", start)

    def prepare() -> None:
        if stop_reason == "shutdown":
            shutdown.set()
        else:
            threading.Event().wait(0.08)

    deadline = time.monotonic() + (5 if stop_reason == "shutdown" else 0.04)
    with git_runtime.operation_deadline(deadline, shutdown=shutdown):
        result = worker_pool._run_bounded_host_command(
            (sys.executable, "-c", "pass"),
            validation_argv=("pytest",),
            source=tmp_path,
            scratch=tmp_path,
            environment={},
            timeout_s=5,
            shutdown=shutdown,
            pre_launch=prepare,
        )

    start.assert_not_called()
    assert not result.ok
    if stop_reason == "shutdown":
        assert result.interrupted
    else:
        assert result.error == "timeout"


def test_host_check_child_uses_only_the_remaining_operation_time(tmp_path: Path) -> None:
    """A host child cannot reset the time limit after preparation."""
    shutdown = threading.Event()
    started = time.monotonic()
    with git_runtime.operation_deadline(started + 0.15, shutdown=shutdown):
        result = worker_pool._run_bounded_host_command(
            (sys.executable, "-c", "import time; time.sleep(1)"),
            validation_argv=("pytest",),
            source=tmp_path,
            scratch=tmp_path,
            environment={},
            timeout_s=5,
            shutdown=shutdown,
        )

    assert not result.ok
    assert result.error == "timeout"
    assert time.monotonic() - started < 0.8
