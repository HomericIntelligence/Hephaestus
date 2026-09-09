"""Tests for the current source binding of host Git work."""

import queue
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from tests.unit.automation.test_source_worktree import _repository


@pytest.mark.parametrize("stale", [False, True])
def test_host_git_operation_uses_current_source_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    """Only the current receipt can admit a host operation under its lane lock."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="example/project")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    expected = replace(binding, generation=binding.generation + 1) if stale else binding
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )

    def inspect(job: GitJob) -> JobResult:
        with pytest.raises(LockUnavailableError):
            with file_lock(
                manager._lane_lock_path(42, SourceLane.IMPLEMENTATION),
                blocking=False,
                require_exclusive=True,
            ):
                pytest.fail("The host operation did not hold its source lease")
        return JobResult(ok=True, value={"outcome": "clean", "head_sha": revision})

    operation = Mock(side_effect=inspect)
    monkeypatch.setattr(pool, "_git_inspect_implementation_worktree", operation)
    try:
        job = GitJob(
            "example/project",
            "inspect_implementation_worktree",
            30,
            workspace=expected,
            kwargs={
                "repo_root": str(root),
                "worktree_path": str(binding.cwd),
                "branch": "writer",
                "expected_head": revision,
                "issue_number": 42,
            },
        )
        result = pool._run_git(job)
        assert result.ok is not stale
        if stale:
            operation.assert_not_called()
        else:
            operation.assert_called_once()
            assert result.value["source_workspace"] == binding.to_dict()
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("wrong_head", [False, True])
def test_host_rebase_continuation_requires_the_paused_head(
    tmp_path: Path, wrong_head: bool
) -> None:
    """A continuation lease must bind the active rebase and its exact head."""
    import subprocess

    from hephaestus.automation.source_worktree import SourceWorkspaceError
    from tests.unit.automation.test_source_worktree import _git

    root, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="example/project")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, first, branch="writer")
    (binding.cwd / "tracked.txt").write_text("writer\n")
    _git(binding.cwd, "commit", "-am", "writer")
    binding = manager.prepare(
        42, SourceLane.IMPLEMENTATION, _git(binding.cwd, "rev-parse", "HEAD"), branch="writer"
    )
    paused = subprocess.run(
        ["git", "rebase", second], cwd=binding.cwd, capture_output=True, text=True, check=False
    )
    assert paused.returncode != 0
    lease = manager.implementation_local_commit(
        42,
        branch="writer",
        path=binding.cwd,
        expected_binding=binding,
        paused_head_sha=first if wrong_head else second,
    )
    if wrong_head:
        with pytest.raises(SourceWorkspaceError):
            with lease:
                pytest.fail("The continuation accepted a different paused head")
    else:
        with lease:
            with pytest.raises(LockUnavailableError):
                with file_lock(
                    manager._lane_lock_path(42, SourceLane.IMPLEMENTATION),
                    blocking=False,
                    require_exclusive=True,
                ):
                    pytest.fail("The continuation did not hold its source lease")
