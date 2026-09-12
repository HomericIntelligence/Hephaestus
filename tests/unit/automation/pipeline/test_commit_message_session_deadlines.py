"""Keep commit-message session discovery inside the queued Git deadline."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation import agent_config, claude_invoke, git_runtime, git_utils
from hephaestus.automation.pipeline import worker_pool


def _git_result(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Supply the checkout identity and registered worktree at the Git boundary."""
    if "rev-parse" in command:
        return subprocess.CompletedProcess(command, 0, f"{cwd / '.git'}\n", "")
    assert "worktree" in command
    return subprocess.CompletedProcess(command, 0, f"worktree {cwd}\0", "")


@pytest.mark.parametrize("failure_at", [1, 2], ids=["checkout", "worktree-list"])
@pytest.mark.parametrize("failure", ["timeout", "subsecond", "interrupted"])
def test_commit_message_discovery_stops_before_provider_after_deadline_or_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: int,
    failure: str,
) -> None:
    """Expired or cancelled Git work must not start a commit-message provider."""
    clock = [100.0]
    shutdown = threading.Event()
    calls: list[list[str]] = []
    provider = Mock(return_value=subprocess.CompletedProcess(["claude"], 0, "message", ""))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def git_read(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) == failure_at:
            if failure == "interrupted":
                shutdown.set()
            else:
                clock[0] = 109.5 if failure == "subsecond" else 111.0
        return _git_result(command, tmp_path)

    expected_error = InterruptedError if failure == "interrupted" else subprocess.TimeoutExpired
    with (
        patch.object(git_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0])),
        git_runtime.operation_deadline(110.0, shutdown=shutdown),
        patch.object(git_utils, "get_repo_slug", return_value="repo"),
        patch.object(
            agent_config,
            "subprocess",
            SimpleNamespace(run=git_read, SubprocessError=subprocess.SubprocessError),
        ),
        patch.object(claude_invoke, "_run_tracked", provider),
        pytest.raises(expected_error),
    ):
        worker_pool._invoke_claude_commit_message(
            7, "Write the commit message.", tmp_path, "claude", 10, "test-model", None
        )

    provider.assert_not_called()
    assert len(calls) == failure_at


def test_commit_message_discovery_uses_the_remaining_git_operation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit-message Git reads and the provider use the current Git budget."""
    clock = [100.0]
    git_timeouts: list[int] = []
    provider = Mock(return_value=subprocess.CompletedProcess(["claude"], 0, "message", ""))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def git_read(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        git_timeouts.append(kwargs["timeout"])
        clock[0] += 0.5
        return _git_result(command, tmp_path)

    with (
        patch.object(git_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0])),
        git_runtime.operation_deadline(103.0, shutdown=threading.Event()),
        patch.object(git_utils, "get_repo_slug", return_value="repo"),
        patch.object(
            agent_config,
            "subprocess",
            SimpleNamespace(run=git_read, SubprocessError=subprocess.SubprocessError),
        ),
        patch.object(claude_invoke, "_run_tracked", provider),
    ):
        message = worker_pool._invoke_claude_commit_message(
            7, "Write the commit message.", tmp_path, "claude", 10, "test-model", None
        )

    assert message == "message"
    assert git_timeouts == [3, 2]
    assert provider.call_args.kwargs["timeout"] == 2
