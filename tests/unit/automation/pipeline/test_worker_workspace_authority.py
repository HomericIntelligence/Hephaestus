"""Tests for the worker's authoritative workspace lease."""

import queue
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import WorkspaceBinding
from hephaestus.automation.pipeline import athena_skill_jobs, worker_pool
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillJob, AthenaSkillRequest
from hephaestus.automation.source_worktree import SourceWorkspacePreparationError


def test_host_skill_rejects_unbound_workspace(tmp_path: Path) -> None:
    """A host skill must carry its explicit workspace authority."""
    executor = Mock()
    pool = worker_pool.WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        athena_skill_executor=executor,
    )
    job = AthenaSkillJob(AthenaSkillRequest("advise", "repo", 1, "claude", "default", tmp_path, 30))
    try:
        with pytest.raises(RuntimeError, match="workspace binding"):
            pool._run_athena_skill(job)
        executor.execute.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)


def test_host_skill_validation_uses_its_execution_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation and host execution must use one deadline."""
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def validate(*args: object, **kwargs: object) -> Path:
        clock[0] = 131.0
        return tmp_path

    monkeypatch.setattr(athena_skill_jobs, "validate_workspace_binding", validate)
    executor = Mock()
    pool = worker_pool.WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        athena_skill_executor=executor,
    )
    job = AthenaSkillJob(
        AthenaSkillRequest(
            "advise",
            "repo",
            1,
            "claude",
            "default",
            tmp_path,
            30,
            workspace=WorkspaceBinding.external(tmp_path),
        )
    )
    try:
        with pytest.raises((subprocess.TimeoutExpired, SourceWorkspacePreparationError)):
            pool._run_athena_skill(job)
        executor.execute.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)


def test_host_skill_does_not_execute_after_cancellation(tmp_path: Path) -> None:
    """A cancellation request must stop host work before its execution."""
    shutdown = threading.Event()
    executor = Mock()
    pool = worker_pool.WorkerPool(
        size=1, shutdown=shutdown, completion_q=queue.Queue(), athena_skill_executor=executor
    )
    job = AthenaSkillJob(
        AthenaSkillRequest(
            "advise",
            "repo",
            1,
            "claude",
            "default",
            tmp_path,
            30,
            workspace=WorkspaceBinding.external(tmp_path),
        )
    )
    shutdown.set()
    try:
        with pytest.raises(InterruptedError):
            pool._run_athena_skill(job)
        executor.execute.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)
