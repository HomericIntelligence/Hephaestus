"""Tests for the job budget during dirty writer publication."""

import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import DirtyPlanIdentity, SourceLane, WorkspaceBinding
from hephaestus.automation import commit_runtime, source_worktree
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.github_jobs import DirtyDirectPrStateRead
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspaceManager,
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
)
from hephaestus.automation.worktree_snapshot import _dirty_worktree_content_snapshot
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.utils.file_lock import file_lock
from tests.unit.agents.test_dirty_workspace import _claim
from tests.unit.automation.test_source_worktree import _git, _repository


def _consumed_dirty_writer(
    tmp_path: Path,
) -> tuple[Path, SourceWorkspaceManager, WorkspaceBinding]:
    """Prepare a dirty writer with a consumed claim and retained content."""
    root, _, revision = _repository(tmp_path, origin_repository="repo")
    manager = SourceWorkspaceManager(root, repository="repo")
    claim = replace(_claim(), reservation_base_sha=revision)
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, revision, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(
        12, claim=claim, expected_generation=original.generation
    )
    identity = DirtyPlanIdentity(
        claim.plan_revision, claim.plan_fingerprint, claim.review_fingerprint, claim.allowed_paths
    )
    with manager.acquire(binding, dirty_plan_identity=identity):
        pass
    return root, manager, binding


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "shutdown"])
@pytest.mark.parametrize(
    "operation",
    [
        "claim_dirty_direct_continuation",
        "publish_dirty_direct_continuation",
        "finish_dirty_direct_publication",
    ],
)
def test_dirty_publication_stops_while_its_source_lane_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, operation: str
) -> None:
    """A consumed writer claim must retain the same lock budget as its job."""
    root, manager, binding = _consumed_dirty_writer(tmp_path)
    consumed = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
    shutdown = threading.Event()
    pool = WorkerPool(
        size=1, shutdown=shutdown, completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    attempted = threading.Event()

    @contextmanager
    def observed_lock(path: Path, **kwargs: Any) -> Iterator[None]:
        if path == manager._lane_lock_path(12, SourceLane.IMPLEMENTATION):
            attempted.set()
        with file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(source_worktree, "file_lock", observed_lock)
    monkeypatch.setattr(worker_pool, "file_lock", observed_lock)
    verify_plan = Mock(side_effect=AssertionError("publication reached live plan validation"))
    monkeypatch.setattr(pool, "_verify_dirty_direct_plan", verify_plan)
    job = GitJob(
        "repo",
        operation,
        30,
        deadline_s=time.monotonic() + (20 if cancel else 0.3),
        kwargs={
            "source_workspace": binding.to_dict(),
            "repo_root": str(root),
            "issue_number": 12,
            "expected_head": binding.revision,
            "pr_number": 17,
        },
    )
    results: list[JobResult] = []
    thread = threading.Thread(target=lambda: results.append(pool._run_git(job)), daemon=True)
    try:
        with file_lock(
            manager._lane_lock_path(12, SourceLane.IMPLEMENTATION), require_exclusive=True
        ):
            thread.start()
            if cancel:
                assert attempted.wait(timeout=1)
                shutdown.set()
            thread.join(timeout=1)
            stopped_while_locked = not thread.is_alive()
        thread.join(timeout=2)
        assert stopped_while_locked
        assert len(results) == 1 and not results[0].ok
        assert attempted.is_set()
        if cancel:
            assert results[0].interrupted
            assert results[0].error == "interrupted"
            assert results[0].value is None
        else:
            assert results[0].error == "timeout"
        verify_plan.assert_not_called()
        assert manager._require_receipt(12, SourceLane.IMPLEMENTATION) == consumed
    finally:
        thread.join(timeout=2)
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("timeout", [True, False], ids=["timeout", "failure"])
def test_dirty_claim_failure_keeps_its_operation_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: bool
) -> None:
    """Claim failures retain the error contract and the pending writer content."""
    root, _, revision = _repository(tmp_path, origin_repository="repo")
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(12, SourceLane.IMPLEMENTATION, revision, branch=_claim().branch)
    (binding.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(binding.cwd, timeout=10)
    receipt = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
    failure = (
        SourceWorkspacePreparationError(SourceWorkspacePreparationCause.GIT_TIMEOUT)
        if timeout
        else SourceWorkspaceError("dirty direct state is unavailable")
    )
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    read_state = Mock(side_effect=failure)
    monkeypatch.setattr(pool, "_read_dirty_direct_state", read_state)
    try:
        result = pool._run_git(
            GitJob(
                "repo",
                "claim_dirty_direct_continuation",
                30,
                kwargs={"repo_root": str(root), "issue_number": 12},
            )
        )
        assert not result.ok
        assert result.error == ("timeout" if timeout else "dirty_direct_claim_failed")
        assert result.value == (None if timeout else {"preserved_worktree": str(binding.cwd)})
        read_state.assert_called_once()
        assert manager._require_receipt(12, SourceLane.IMPLEMENTATION) == receipt
        assert _dirty_worktree_content_snapshot(binding.cwd, timeout=10) == snapshot
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("timeout", [True, False], ids=["timeout", "failure"])
@pytest.mark.parametrize("phase", ["pre_stage", "pre_push"])
def test_dirty_publication_failure_keeps_its_operation_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: bool, phase: str
) -> None:
    """Publication failures retain the cause, progress, and exact local head."""
    root, manager, binding = _consumed_dirty_writer(tmp_path)
    snapshot = _dirty_worktree_content_snapshot(binding.cwd, timeout=10)
    failure = (
        SourceWorkspacePreparationError(SourceWorkspacePreparationCause.GIT_TIMEOUT)
        if timeout
        else SourceWorkspaceError("dirty direct plan is unavailable")
    )
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    verify_plan = Mock(side_effect=[None, failure] if phase == "pre_push" else failure)
    monkeypatch.setattr(pool, "_verify_dirty_direct_plan", verify_plan)
    monkeypatch.setattr(pool, "_read_remote_branch_head", lambda *args, **kwargs: binding.revision)
    monkeypatch.setattr(
        worker_pool, "_controlled_git_signing_env", lambda *args, **kwargs: build_git_child_env()
    )

    def commit(*args: object, **kwargs: object) -> None:
        _git(binding.cwd, "commit", "-m", "controlled dirty writer commit")

    commit_call = Mock(side_effect=commit)
    monkeypatch.setattr(commit_runtime, "_commit_with_signature", commit_call)
    monkeypatch.setattr(pool, "_is_exact_recovery_commit", lambda *args, **kwargs: True)
    push = Mock(side_effect=AssertionError("failed validation reached publication"))
    monkeypatch.setattr(pool, "_publish_commit_push", push)
    try:
        result = pool._run_git(
            GitJob(
                "repo",
                "publish_dirty_direct_continuation",
                30,
                kwargs={
                    "repo_root": str(root),
                    "issue_number": 12,
                    "source_workspace": binding.to_dict(),
                },
            )
        )
        local_head = _git(binding.cwd, "rev-parse", "HEAD")
        committed = phase == "pre_push"
        assert not result.ok
        assert result.error == ("timeout" if timeout else "dirty_direct_publication_failed")
        assert result.value == {
            "phase": phase,
            "reason": "SourceWorkspacePreparationError" if timeout else "SourceWorkspaceError",
            "committed": committed,
            "pushed": False,
            "local_head": local_head,
        }
        assert (local_head != binding.revision) is committed
        assert verify_plan.call_count == (2 if committed else 1)
        assert commit_call.call_count == int(committed)
        push.assert_not_called()
        receipt = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
        assert receipt.revision == local_head
        assert receipt.generation == binding.generation + int(committed)
        assert receipt.dirty_claim is not None and receipt.dirty_claim.state == "consumed"
        if not committed:
            assert _dirty_worktree_content_snapshot(binding.cwd, timeout=10) == snapshot
    finally:
        pool.shutdown(mark_interrupted=False)


def test_dirty_plan_read_keeps_the_existing_git_job_deadline(tmp_path: Path) -> None:
    """The nested GitHub call must not restart the writer's operation budget."""
    claim = _claim()
    receipt = DirtyDirectPrStateRead("example/repo", 12, claim.branch, (), None)
    runner = Mock()
    runner.run.return_value = receipt
    shutdown = threading.Event()
    pool = WorkerPool(
        size=1, shutdown=shutdown, completion_q=queue.Queue(), github_job_runner=runner
    )
    job = GitJob(
        "repo",
        "publish_dirty_direct_continuation",
        30,
        deadline_s=time.monotonic() + 10,
        expected_repository="example/repo",
    )
    try:
        assert (
            pool._read_dirty_direct_state(job, repo_root=tmp_path, issue=12, branch=claim.branch)
            == receipt
        )
        assert runner.run.call_args.kwargs["deadline_s"] == job.deadline_s
        assert runner.run.call_args.kwargs["shutdown"] is shutdown
    finally:
        pool.shutdown(mark_interrupted=False)
