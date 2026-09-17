"""Tests for the current source binding of host Git work."""

import queue
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import worktree_manager
from hephaestus.automation.git_utils import run
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from tests.unit.automation.test_source_worktree import _git, _repository


def _supply_registered_worktree_listing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply NUL output from real registration in the controlled Git fixture."""
    listing = _git(root, "worktree", "list", "--porcelain")
    original_run = run

    def registered_worktrees(argv: list[str], **kwargs: Any) -> Any:
        if argv == ["git", "worktree", "list", "--porcelain", "-z"]:
            assert kwargs["cwd"] == root
            return subprocess.CompletedProcess(argv, 0, listing.replace("\n", "\0") + "\0\0", "")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(worktree_manager, "run", registered_worktrees)


@pytest.mark.parametrize("case", ["valid", "stale", "changed_manifest", "expired"])
def test_repository_validation_source_preparation_holds_review_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Inspect source under current review ownership without writer authority."""
    import time

    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationSourceRead,
        RepositoryValidationSourceRequest,
    )
    from tests.unit.automation.pipeline.test_repository_validation import _api, _plan

    root, _, revision = _repository(tmp_path, origin_repository="LLM360/comet")
    manager = SourceWorkspaceManager(root, repository="LLM360/comet")
    binding = manager.prepare(1623, SourceLane.REVIEW, revision)
    _supply_registered_worktree_listing(root, monkeypatch)
    expected = replace(binding, generation=binding.generation + 1) if case == "stale" else binding
    plan = replace(
        _plan(_api(), binding.cwd),
        source_workspace=expected,
        reviewed_head=revision,
        reviewed_base=revision,
        diff_base_sha=revision,
    )
    request = RepositoryValidationSourceRequest(
        plan.repository,
        plan.issue_number,
        plan.pr_number,
        expected,
        revision,
        revision,
        revision,
        plan.changes,
        1,
        "f" * 32,
        time.monotonic() + (-1 if case == "expired" else 60),
    )
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )

    def inspect(*args: object, **kwargs: object) -> object:
        with pytest.raises(LockUnavailableError):
            with file_lock(
                manager._lane_lock_path(1623, SourceLane.REVIEW),
                blocking=False,
                require_exclusive=True,
            ):
                pytest.fail("Source preparation did not hold the review lease.")
        if case == "changed_manifest":
            return replace(plan, changes=(("D", "src/comet/proxy/app.py"),))
        return plan

    operation = Mock(side_effect=inspect)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.worker_pool.comet_plan_for_workspace", operation
    )
    monkeypatch.setattr(
        pool,
        "_source_git_binding",
        Mock(side_effect=AssertionError("Review is not writer authority.")),
    )
    monkeypatch.setattr(
        pool,
        "_authenticated_remote_git_configuration",
        Mock(side_effect=AssertionError("Local source reads need no transport.")),
    )
    try:
        result = pool._run_git(
            GitJob(
                repo="comet",
                expected_repository="LLM360/comet",
                op="prepare_repository_validation",
                timeout_s=60,
                deadline_s=request.deadline_s,
                workspace=expected,
                repository_validation_preparation=request,
            )
        )
        assert result.ok is (case == "valid")
        if case == "valid":
            assert type(result.value) is RepositoryValidationSourceRead
            assert result.value.request == request
            assert result.value.plan == plan
            operation.assert_called_once()
        elif case in {"stale", "expired"}:
            operation.assert_not_called()
        else:
            operation.assert_called_once()
            assert result.value.plan is None
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("stale", [False, True])
def test_host_git_operation_uses_current_source_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    """Only the current receipt can admit a host operation under its lane lock."""
    root, _, revision = _repository(tmp_path, origin_repository="example/project")
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
    transport = Mock(return_value=({}, ()))
    monkeypatch.setattr(pool, "_authenticated_remote_git_configuration", transport)
    _supply_registered_worktree_listing(root, monkeypatch)
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
        transport.assert_called_once()
        assert transport.call_args.kwargs["cwd"] == binding.cwd
        assert transport.call_args.kwargs["expected_repo"] == "example/project"
        assert result.ok is not stale
        if stale:
            assert result.error == (
                "source_workspace_ownership_unavailable: implementation publication binding changed"
            )
            operation.assert_not_called()
        else:
            operation.assert_called_once()
            assert result.value["source_workspace"] == binding.to_dict()
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("wrong_head", [False, True])
def test_host_rebase_continuation_requires_the_paused_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_head: bool
) -> None:
    """A continuation lease must bind the active rebase and its exact head."""
    from hephaestus.automation.source_worktree import SourceWorkspaceError

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
    assert _git(binding.cwd, "rev-parse", "HEAD") == second
    _supply_registered_worktree_listing(root, monkeypatch)
    head_read = Mock(wraps=manager._head_revision)
    monkeypatch.setattr(manager, "_head_revision", head_read)
    lease = manager.implementation_local_commit(
        42,
        branch="writer",
        path=binding.cwd,
        expected_binding=binding,
        paused_head_sha=first if wrong_head else second,
    )
    if wrong_head:
        with pytest.raises(
            SourceWorkspaceError, match="implementation publication binding changed"
        ):
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
    head_read.assert_called_once()
    assert head_read.call_args.args[0] == binding.cwd
