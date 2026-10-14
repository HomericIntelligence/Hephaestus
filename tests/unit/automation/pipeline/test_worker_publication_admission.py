"""Tests for failures before a publication can start."""

import queue
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBindingError
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import (
    SourceWorkspaceManager,
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
)
from tests.unit.automation.test_source_worktree import _repository


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (
            SourceWorkspacePreparationError(SourceWorkspacePreparationCause.GIT_TIMEOUT),
            "timeout",
        ),
        (WorkspaceBindingError("source binding changed"), "source_workspace_ownership_unavailable"),
    ],
)
def test_publication_admission_returns_a_bounded_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_error: str,
) -> None:
    """An admission timeout or invalid binding must stop before publication."""
    root, _, revision = _repository(tmp_path, origin_repository="example/repo")
    source_manager = SourceWorkspaceManager(root, repository="example/repo")
    binding = source_manager.prepare(1, SourceLane.IMPLEMENTATION, revision, branch="writer")
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    manager = Mock(spec=SourceWorkspaceManager)
    manager.implementation_local_commit.side_effect = failure
    monkeypatch.setattr(pool, "_source_git_manager", lambda job: (manager, binding))
    publish = Mock()
    monkeypatch.setattr(pool, "_git_commit_push_inner", publish)
    job = GitJob(
        "example/repo",
        "commit_push",
        30,
        workspace=binding,
        kwargs={
            "source_lane": "impl",
            "repo_root": str(root),
            "worktree_path": str(binding.cwd),
            "issue_number": 1,
            "branch": "writer",
        },
    )
    try:
        result = pool._run_git(job)
        assert not result.ok
        assert (result.error or "").startswith(expected_error)
        manager.implementation_local_commit.assert_called_once()
        publish.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)
