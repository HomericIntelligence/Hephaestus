"""Tests for bounded writer handoff and publication results."""

import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock, create_autospec

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_utils, implementation_writer
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.test_source_worktree import _git, _repository


def _pool(tmp_path: Path, shutdown: threading.Event) -> worker_pool.WorkerPool:
    return worker_pool.WorkerPool(
        size=1, shutdown=shutdown, completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )


def test_source_rebase_metadata_does_not_reach_the_git_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real rebase must use the closed helper contract after source admission."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    pool = _pool(tmp_path, threading.Event())
    rebase = create_autospec(git_utils.rebase_worktree_onto, return_value=False)
    monkeypatch.setattr(git_utils, "rebase_worktree_onto", rebase)
    monkeypatch.setattr(worker_pool, "_required_git_signing_env", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        pool, "_authenticated_remote_revalidator", lambda **kwargs: lambda: ({}, ())
    )
    monkeypatch.setattr(
        git_utils,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1 if command[1:3] == ["merge-base", "--is-ancestor"] else 0, "", ""
        ),
    )
    job = GitJob(
        "repo",
        "rebase",
        30,
        workspace=binding,
        kwargs={
            "cwd": str(binding.cwd),
            "repo_root": str(root),
            "issue_number": 42,
            "branch": "writer",
            "expected_remote_sha": revision,
            "publish_rebased_head": True,
            "abort_on_conflict": True,
        },
    )
    try:
        result = pool._run_git(job)
        assert not result.ok
        assert result.error == "mechanical rebase hit conflicts; aborted"
        rebase.assert_called_once()
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "shutdown"])
def test_writer_creation_stops_while_its_handoff_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    """A contended writer lock must not extend the job budget or delay shutdown."""
    root, _, _ = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    shutdown = threading.Event()
    pool = _pool(tmp_path, shutdown)
    attempted = threading.Event()

    @contextmanager
    def observed_lock(path: Path, **kwargs: Any) -> Iterator[None]:
        attempted.set()
        with file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(implementation_writer, "file_lock", observed_lock)
    monkeypatch.setattr(pool, "_recover_pretest_candidate", lambda *args: None)
    monkeypatch.setattr(pool, "_recover_prepared_remediation_worktree", lambda *args: None)
    inner = Mock(return_value=JobResult(ok=False, error="unexpected writer execution"))
    monkeypatch.setattr(pool, "_git_create_worktree_with_handoff", inner)
    results: list[JobResult] = []
    job = GitJob(
        "repo",
        "create_worktree",
        30,
        deadline_s=time.monotonic() + (20 if cancel else 0.2),
        kwargs={"source_lane": "impl", "repo_root": str(root), "issue_number": 42},
    )
    thread = threading.Thread(target=lambda: results.append(pool._run_git(job)), daemon=True)
    try:
        with file_lock(
            manager._lane_lock_path(42, SourceLane.IMPLEMENTATION), require_exclusive=True
        ):
            thread.start()
            if cancel:
                assert attempted.wait(timeout=1)
                shutdown.set()
            thread.join(timeout=1)
            stopped_while_locked = not thread.is_alive()
        thread.join(timeout=2)
        assert stopped_while_locked
        assert len(results) == 1
        assert results[0].interrupted if cancel else results[0].error == "timeout"
        inner.assert_not_called()
    finally:
        thread.join(timeout=2)
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("refresh", [False, True], ids=["ordinary", "refresh"])
def test_publication_timeout_keeps_the_recorded_local_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refresh: bool
) -> None:
    """An uncertain push must retain the exact local head without a second push."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    pool = _pool(tmp_path, threading.Event())
    monkeypatch.setattr(pool, "_verify_implementation_edit_scope", lambda *args, **kwargs: None)
    monkeypatch.setattr(pool, "_verify_scope_retraction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pool, "_authenticated_remote_revalidator", lambda **kwargs: lambda: ({}, ())
    )
    monkeypatch.setattr(pool, "_writer_tracking_head", lambda *args, **kwargs: revision)
    local_heads: list[str] = []
    observed_receipts: list[str] = []

    def commit(*args: Any, **kwargs: Any) -> bool | str:
        (binding.cwd / "tracked.txt").write_text("controlled writer change\n")
        _git(binding.cwd, "commit", "-am", "controlled writer change")
        local_heads.append(_git(binding.cwd, "rev-parse", "HEAD"))
        return local_heads[-1] if refresh else True

    def push(*args: Any, **kwargs: Any) -> None:
        observed_receipts.append(manager._require_receipt(42, SourceLane.IMPLEMENTATION).revision)
        clock[0] = 131.0
        raise subprocess.TimeoutExpired("git push", 30)

    monkeypatch.setattr(pool, "_commit_if_changes_with_controlled_signing", commit)
    monkeypatch.setattr(pool, "_rebase_publication_writer", commit)
    push_mock = Mock(side_effect=push)
    monkeypatch.setattr(git_utils, "push_branch", push_mock)
    monkeypatch.setattr(git_utils, "push_head_to_branch", push_mock)
    kwargs: dict[str, Any] = {
        "source_lane": "impl",
        "repo_root": str(root),
        "worktree_path": str(binding.cwd),
        "issue_number": 42,
        "branch": "writer",
    }
    if refresh:
        kwargs["writer_refresh"] = {
            "phase": "rebase",
            "source_sha": revision,
            "expected_remote_sha": revision,
        }
    try:
        result = pool._run_git(GitJob("repo", "commit_push", 30, workspace=binding, kwargs=kwargs))
        assert not result.ok
        assert isinstance(result.value, dict)
        assert result.value["publication_state"] == "probe_failed"
        assert result.value["head_sha"] == local_heads[0]
        assert result.value["source_workspace"]["revision"] == local_heads[0]
        assert observed_receipts == local_heads
        assert manager._require_receipt(42, SourceLane.IMPLEMENTATION).revision == local_heads[0]
        push_mock.assert_called_once()
    finally:
        pool.shutdown(mark_interrupted=False)
