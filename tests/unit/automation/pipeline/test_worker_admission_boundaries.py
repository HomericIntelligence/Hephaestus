"""Tests for source identity, cancellation, and the shared job deadline."""

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation import claude_invoke, git_runtime, worktree_snapshot
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.source_worktree import SourceWorkspaceError, SourceWorkspaceManager
from tests.unit.automation.test_source_worktree import _repository


def test_clean_source_lease_requires_current_repository_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean source job must retain its exact repository registration."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="example/project")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    monkeypatch.setattr(manager, "_path_is_registered_to_repository", lambda *args, **kwargs: False)
    with pytest.raises(SourceWorkspaceError, match="registration"):
        with manager.acquire(binding):
            pytest.fail("The source lease accepted missing repository registration")


def test_agent_cancellation_after_validation_stops_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation after admission must stop the provider before it starts."""
    shutdown = threading.Event()

    def validate(*args: object, **kwargs: object) -> Path:
        shutdown.set()
        return tmp_path

    monkeypatch.setattr(worker_pool, "validate_job_workspace", validate)
    monkeypatch.setattr(worker_pool, "resolve_agent", lambda *args, **kwargs: "claude")
    provider = Mock(return_value=("ok", "session"))
    monkeypatch.setattr(claude_invoke, "invoke_claude_with_session", provider)
    pool = worker_pool.WorkerPool(size=1, shutdown=shutdown, completion_q=queue.Queue())
    job = AgentJob(
        "repo",
        1,
        "claude",
        "default",
        lambda: "prompt",
        tmp_path,
        30,
        workspace=WorkspaceBinding.external(tmp_path),
    )
    try:
        result = pool._run_agent(job)
        assert result.interrupted and not result.ok
        provider.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("selector_supported", [True, False])
def test_capture_startup_does_not_renew_the_operation_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector_supported: bool
) -> None:
    """Both capture readers must include child startup in the outer deadline."""
    start_child = subprocess.Popen

    def delayed_start(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = start_child(*args, **kwargs)
        time.sleep(0.2)
        return child

    monkeypatch.setattr(subprocess, "Popen", delayed_start)
    monkeypatch.setattr(
        worktree_snapshot, "_subprocess_pipe_selector_supported", lambda: selector_supported
    )
    with git_runtime.operation_deadline(time.monotonic() + 0.1):
        with pytest.raises(subprocess.TimeoutExpired):
            worktree_snapshot._run_bounded_git_output(
                (sys.executable, "-c", "print('ready')"),
                cwd=tmp_path,
                timeout=30,
                max_bytes=1024,
                retain_text=True,
            )
