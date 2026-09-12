"""Keep session discovery inside the agent operation deadline."""

from __future__ import annotations

import queue
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.workspace import WorkspaceBinding
from hephaestus.automation import agent_config, claude_invoke
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.resilience.circuit_breaker import get_circuit_breaker, reset_all_circuit_breakers


def _job(cwd: Path, *, timeout_s: int = 10) -> AgentJob:
    """Build a planner job with an explicit external workspace lease."""
    return AgentJob(
        repo="repo",
        issue=7,
        agent="claude",
        model="test-model",
        prompt_builder=lambda: "Plan the change.",
        cwd=cwd,
        timeout_s=timeout_s,
        workspace=WorkspaceBinding.external(cwd),
        session_agent=agent_config.AGENT_PLANNER,
        execution_request=ExecutionRequest(
            AgentRole.PLANNER, AgentOperation.PLAN, SessionLifecycle.START_NEW
        ),
        allowed_tools="Read,Glob,Grep",
        sandbox="read-only",
    )


def _git_result(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Supply the checkout identity and its registered worktree."""
    if "rev-parse" in command:
        return subprocess.CompletedProcess(command, 0, f"{cwd / '.git'}\n", "")
    assert "worktree" in command
    return subprocess.CompletedProcess(command, 0, f"worktree {cwd}\0", "")


@pytest.mark.parametrize("failure_at", [1, 2], ids=["checkout", "worktree-list"])
@pytest.mark.parametrize("failure", ["timeout", "interrupted"])
@pytest.mark.parametrize("git_failed", [False, True], ids=["git-result", "git-error"])
def test_session_discovery_stops_before_provider_after_deadline_or_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: int,
    failure: str,
    git_failed: bool,
) -> None:
    """An expired or cancelled session read must not start a provider turn."""
    clock = [100.0]
    shutdown = threading.Event()
    calls: list[tuple[list[str], int]] = []
    provider = Mock(return_value=subprocess.CompletedProcess(["claude"], 0, "ok", ""))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def git_read(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["timeout"]))
        if len(calls) == failure_at:
            if failure == "timeout":
                clock[0] = 111.0
            else:
                shutdown.set()
            if git_failed:
                raise subprocess.CalledProcessError(128, command)
        return _git_result(command, tmp_path)

    reset_all_circuit_breakers()
    pool = worker_pool.WorkerPool(1, shutdown, queue.Queue())
    try:
        with (
            patch.object(worker_pool, "time", SimpleNamespace(monotonic=lambda: clock[0])),
            patch.object(worker_pool, "resolve_agent", return_value="claude"),
            patch.object(
                agent_config,
                "subprocess",
                SimpleNamespace(run=git_read, SubprocessError=subprocess.SubprocessError),
            ),
            patch.object(claude_invoke, "_run_tracked", provider),
        ):
            result = pool._run_agent(_job(tmp_path))
            assert not result.ok
            assert result.error == failure
            assert result.interrupted is (failure == "interrupted")
            provider.assert_not_called()
            assert len(calls) == failure_at
            assert get_circuit_breaker("agent:claude").snapshot()["failure_count"] == 0

            shutdown.clear()
            healthy = pool._run_agent(replace(_job(tmp_path), issue=8))
            assert healthy.ok
            provider.assert_called_once()
    finally:
        pool.shutdown(mark_interrupted=False)
        reset_all_circuit_breakers()


def test_session_discovery_uses_the_remaining_operation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git reads and the provider share the worker's original time budget."""
    clock = [100.0]
    git_timeouts: list[int] = []
    provider = Mock(return_value=subprocess.CompletedProcess(["claude"], 0, "ok", ""))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def git_read(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        git_timeouts.append(kwargs["timeout"])
        clock[0] += 0.5
        return _git_result(command, tmp_path)

    reset_all_circuit_breakers()
    pool = worker_pool.WorkerPool(1, threading.Event(), queue.Queue())
    try:
        with (
            patch.object(worker_pool, "time", SimpleNamespace(monotonic=lambda: clock[0])),
            patch.object(worker_pool, "resolve_agent", return_value="claude"),
            patch.object(
                agent_config,
                "subprocess",
                SimpleNamespace(run=git_read, SubprocessError=subprocess.SubprocessError),
            ),
            patch.object(claude_invoke, "_run_tracked", provider),
        ):
            result = pool._run_agent(_job(tmp_path, timeout_s=3))
        assert result.ok
        assert git_timeouts == [3, 2]
        assert provider.call_args.kwargs["timeout"] == 2
    finally:
        pool.shutdown(mark_interrupted=False)
        reset_all_circuit_breakers()


@pytest.mark.parametrize("failure", ["timeout", "interrupted"])
def test_provider_launch_rechecks_the_operation_after_command_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A stop during command preparation prevents the provider process start."""
    clock = [100.0]
    shutdown = threading.Event()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def git_read(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _git_result(command, tmp_path)

    def child_environment() -> dict[str, str]:
        if failure == "timeout":
            clock[0] = 111.0
        else:
            shutdown.set()
        return {}

    process = Mock(pid=-1, returncode=0)
    process.communicate.return_value = ("ok", "")
    reset_all_circuit_breakers()
    pool = worker_pool.WorkerPool(1, shutdown, queue.Queue())
    try:
        with (
            patch.object(worker_pool, "time", SimpleNamespace(monotonic=lambda: clock[0])),
            patch.object(worker_pool, "resolve_agent", return_value="claude"),
            patch.object(
                agent_config,
                "subprocess",
                SimpleNamespace(run=git_read, SubprocessError=subprocess.SubprocessError),
            ),
            patch.object(claude_invoke, "build_claude_child_env", child_environment),
            patch.object(subprocess, "Popen", return_value=process) as provider,
        ):
            result = pool._run_agent(_job(tmp_path))

        assert not result.ok
        assert result.error == failure
        assert result.interrupted is (failure == "interrupted")
        provider.assert_not_called()
        assert get_circuit_breaker("agent:claude").snapshot()["failure_count"] == 0
    finally:
        pool.shutdown(mark_interrupted=False)
        reset_all_circuit_breakers()


def test_manual_session_invocation_retains_independent_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual callers retain five-second Git reads and their provider timeout."""
    git_timeouts: list[int] = []
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def git_read(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        git_timeouts.append(kwargs["timeout"])
        return _git_result(command, tmp_path)

    with (
        patch.object(
            agent_config,
            "subprocess",
            SimpleNamespace(run=git_read, SubprocessError=subprocess.SubprocessError),
        ),
        patch.object(
            claude_invoke,
            "_run_tracked",
            return_value=subprocess.CompletedProcess(["claude"], 0, "ok", ""),
        ) as provider,
    ):
        stdout, _ = claude_invoke.invoke_claude_with_session(
            repo="repo",
            issue=7,
            agent=agent_config.AGENT_PLANNER,
            prompt="Plan the change.",
            model="test-model",
            cwd=tmp_path,
            timeout=17,
        )

    assert stdout == "ok"
    assert git_timeouts == [5, 5]
    assert provider.call_args.kwargs["timeout"] == 17


@pytest.mark.parametrize("failure", ["timeout", "interrupted"])
def test_provider_start_race_stops_and_reaps_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A stop during process creation must close the child before return."""
    clock = [100.0]
    shutdown = threading.Event()
    process = Mock(pid=-1, returncode=0)
    process.communicate.return_value = ("", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def git_read(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _git_result(command, tmp_path)

    def start_process(*_args: Any, **_kwargs: Any) -> Mock:
        if failure == "timeout":
            clock[0] = 111.0
        else:
            shutdown.set()
        return process

    reset_all_circuit_breakers()
    pool = worker_pool.WorkerPool(1, shutdown, queue.Queue())
    try:
        with (
            patch.object(worker_pool, "time", SimpleNamespace(monotonic=lambda: clock[0])),
            patch.object(worker_pool, "resolve_agent", return_value="claude"),
            patch.object(
                agent_config,
                "subprocess",
                SimpleNamespace(run=git_read, SubprocessError=subprocess.SubprocessError),
            ),
            patch.object(subprocess, "Popen", side_effect=start_process) as provider,
        ):
            result = pool._run_agent(_job(tmp_path))

        assert not result.ok
        assert result.error == failure
        assert result.interrupted is (failure == "interrupted")
        provider.assert_called_once()
        process.kill.assert_called_once()
        process.communicate.assert_called_once_with()
        assert get_circuit_breaker("agent:claude").snapshot()["failure_count"] == 0
    finally:
        pool.shutdown(mark_interrupted=False)
        reset_all_circuit_breakers()
