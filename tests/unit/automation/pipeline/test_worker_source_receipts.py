"""Tests for source receipt publication from held worker leases."""

import os
import queue
import threading
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.test_rebase_recovery import (
    _registered_git_fixture,
    _source_registration_fixture,
)
from tests.unit.automation.test_source_worktree import _repository


@pytest.fixture(autouse=True)
def _controlled_source_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Control host discovery and parent permissions without remote execution."""
    monkeypatch.setattr(
        worker_pool, "_trusted_gh_executable", lambda extra_path_root=None: "/usr/bin/gh"
    )
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_classified_publication_requires_the_before_publish_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published result cannot invoke source accounting after the network step."""
    pool = WorkerPool(size=1, shutdown=threading.Event(), completion_q=queue.Queue())
    result = pool._writer_publication_receipt("published", "b" * 40, "a" * 40, "b" * 40)
    monkeypatch.setattr(pool, "_git_commit_push_inner", lambda *args, **kwargs: result)
    record = Mock()
    job = GitJob("repo", "commit_push", 30)
    try:
        with ExitStack() as stack:
            with pytest.raises(RuntimeError, match="callback"):
                pool._git_commit_ordinary_writer(job, stack, record)
        record.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("operation", ["create_worktree", "inspect_implementation_worktree"])
def test_source_job_returns_its_full_receipt_from_the_held_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """The coordinator must receive full source facts without a second live read."""
    root, _, revision = _repository(tmp_path, origin_repository="example/repo")
    _registered_git_fixture(root, monkeypatch)
    _source_registration_fixture(root, monkeypatch)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    receipt = manager._require_receipt(42, SourceLane.IMPLEMENTATION)
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    monkeypatch.setattr(pool, "_recover_pretest_candidate", lambda *args: None)
    monkeypatch.setattr(pool, "_recover_prepared_remediation_worktree", lambda *args: None)
    create = Mock(return_value=JobResult(ok=True, value={"path": str(binding.cwd)}))
    inspect = Mock(return_value=JobResult(ok=True, value={}))
    monkeypatch.setattr(pool, "_git_create_worktree_with_handoff", create)
    monkeypatch.setattr(pool, "_git_inspect_implementation_worktree", inspect)
    job = GitJob(
        "repo",
        operation,
        30,
        workspace=binding,
        expected_repository="example/repo",
        kwargs={
            "repo_root": str(root),
            "worktree_path": str(binding.cwd),
            "source_lane": "impl",
            "issue_number": 42,
            "branch": "writer",
        },
    )
    try:
        result = pool._run_git(job)
        assert result.ok
        assert result.value["source_receipt"] == receipt.to_dict()
        assert result.value["source_workspace"] == binding.to_dict()
        (create if operation == "create_worktree" else inspect).assert_called_once()
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize(
    "failure",
    [
        JobResult(
            ok=False, error="signing unavailable", value={"failure_kind": "signing_unavailable"}
        ),
        JobResult(
            ok=False, error="scope retraction incomplete", value={"scope_retraction_failure": True}
        ),
    ],
)
def test_source_publication_preserves_a_failure_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: JobResult
) -> None:
    """A valid source lease must preserve a closed signing or scope failure."""
    root, _, revision = _repository(tmp_path, origin_repository="example/repo")
    _registered_git_fixture(root, monkeypatch)
    _source_registration_fixture(root, monkeypatch)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    commit = Mock(return_value=failure)
    monkeypatch.setattr(pool, "_git_commit_push_inner", commit)
    job = GitJob(
        "repo",
        "commit_push",
        30,
        workspace=binding,
        expected_repository="example/repo",
        kwargs={
            "repo_root": str(root),
            "worktree_path": str(binding.cwd),
            "source_lane": "impl",
            "issue_number": 42,
            "branch": "writer",
        },
    )
    try:
        assert pool._run_git(job) == failure
        commit.assert_called_once()
        assert manager._require_receipt(42, SourceLane.IMPLEMENTATION).revision == revision
    finally:
        pool.shutdown(mark_interrupted=False)
