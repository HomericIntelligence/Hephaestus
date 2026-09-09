"""Tests for failures before a publication can start."""

import queue
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding, WorkspaceBindingError
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import (
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
)


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
    binding = WorkspaceBinding.source(
        cwd=tmp_path / "writer",
        reusable_root=tmp_path,
        repository="repo",
        ownership_key="repo:1:impl",
        item_number=1,
        lane=SourceLane.IMPLEMENTATION,
        revision="a" * 40,
        generation=1,
        detached=False,
    )
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    manager = Mock()
    manager.implementation_local_commit.side_effect = failure
    monkeypatch.setattr(pool, "_source_git_manager", lambda job: (manager, binding))
    publish = Mock()
    monkeypatch.setattr(pool, "_git_commit_push_inner", publish)
    job = GitJob(
        "repo",
        "commit_push",
        30,
        workspace=binding,
        kwargs={
            "source_lane": "impl",
            "repo_root": str(tmp_path),
            "worktree_path": str(binding.cwd),
            "issue_number": 1,
            "branch": "writer",
        },
    )
    try:
        result = pool._run_git(job)
        assert not result.ok
        assert (result.error or "").startswith(expected_error)
        publish.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)
