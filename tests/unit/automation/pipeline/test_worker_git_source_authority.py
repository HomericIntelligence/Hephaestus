"""Tests for source authority on host Git operations."""

import queue
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool


@pytest.mark.parametrize(
    "operation",
    [
        "inspect_implementation_worktree",
        "recover_dirty_worktree",
        "rebase",
        "continue_rebase",
    ],
)
def test_host_git_source_operation_rejects_missing_source_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A host operation must not access source without the current binding."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    operation_runner = Mock(return_value=JobResult(ok=True))
    monkeypatch.setattr(pool, f"_git_{operation}", operation_runner)
    job = GitJob(
        "example/project",
        operation,
        30,
        kwargs={
            "repo_root": str(tmp_path),
            "worktree_path": str(tmp_path / "writer"),
            "cwd": str(tmp_path / "writer"),
            "branch": "writer",
            "expected_head": "a" * 40,
            "issue_number": 1,
        },
    )
    try:
        result = pool._run_git(job)
        assert not result.ok
        assert "source" in (result.error or "")
        operation_runner.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)


def test_commit_push_has_no_unbound_source_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing lane authority must not select an unrestricted commit path."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    publish = Mock(return_value=JobResult(ok=True))
    monkeypatch.setattr(pool, "_git_commit_push_inner", publish)
    try:
        result = pool._run_git(
            GitJob(
                "example/project",
                "commit_push",
                30,
                kwargs={"worktree_path": str(tmp_path / "writer")},
            )
        )
        assert not result.ok
        assert "source" in (result.error or "")
        publish.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)
