"""Tests for passive lock waits and bounded Git command execution."""

import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from hephaestus.automation import git_runtime
from hephaestus.automation.pipeline import repository_lock, worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from tests.unit.automation.test_source_worktree import _repository


def test_git_job_budget_includes_the_repository_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary Git job must use the configured passive lock wait limit."""
    pool = worker_pool.WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
        git_lock_timeout=1,
    )
    started = threading.Event()
    completed = threading.Event()
    results: list[JobResult] = []
    dispatch = Mock(return_value=JobResult(ok=True))
    monkeypatch.setattr(pool, "_dispatch_git_op", dispatch)

    def run_job() -> None:
        started.set()
        try:
            results.append(pool._run_git(GitJob("repo", "commit_push", 60)))
        finally:
            completed.set()

    thread = threading.Thread(target=run_job)
    try:
        with pool._repo_lock("repo"):
            thread.start()
            assert started.wait(1)
            completed_while_locked = completed.wait(2)
        thread.join(timeout=2)
        assert completed_while_locked
        assert results and results[0].error == "lock_timeout"
        dispatch.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)
        thread.join(timeout=2)


def test_git_job_children_use_the_remaining_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git children must not receive a new budget after the lock wait."""
    clock = {"now": 10.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    pool = worker_pool.WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
    )

    def dispatch(job: GitJob) -> JobResult:
        clock["now"] += 1.1
        git_runtime.remaining_operation_timeout(job.timeout_s)
        return JobResult(ok=True)

    monkeypatch.setattr(pool, "_dispatch_git_op", dispatch)
    try:
        result = pool._run_git(GitJob("repo", "commit_push", 1))
        assert not result.ok
        assert result.error == "timeout"
    finally:
        pool.shutdown(mark_interrupted=False)


def test_intake_common_dir_lock_uses_configured_lock_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intake jobs for one common directory use the configured wait budget."""
    pool = worker_pool.WorkerPool(
        size=2,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    caller, _, _ = _repository(tmp_path, origin_repository="acme/repo")
    caller = caller.resolve()
    common_dir = caller / ".git"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_complete = threading.Event()
    first_results: list[JobResult] = []
    second_results: list[JobResult] = []

    receipt = MagicMock()
    receipt.to_dict.return_value = {"revision": "a" * 40}
    first_manager = MagicMock(caller_root=caller, common_dir=common_dir, repository="acme/repo")
    first_manager.run_lease.return_value = nullcontext()

    def first_prepare(**_kwargs: object) -> object:
        first_entered.set()
        assert release_first.wait(5.0)
        return receipt

    first_manager.prepare.side_effect = first_prepare
    second_manager = MagicMock(caller_root=caller, common_dir=common_dir, repository="acme/repo")
    second_manager.run_lease.return_value = nullcontext()
    second_manager.prepare.return_value = receipt
    managers = iter((first_manager, second_manager))

    monkeypatch.setattr(worker_pool, "_checkout_preflight_error", lambda *_args: None)
    monkeypatch.setattr(worker_pool, "_trusted_gh_executable", lambda *_args: "gh")
    monkeypatch.setattr(worker_pool, "_trusted_remote_git_config", lambda *_args: ())
    monkeypatch.setattr(worker_pool, "RepoIntakeManager", lambda *_args, **_kwargs: next(managers))

    first_job = GitJob(
        "repo-one",
        "prepare_intake",
        5,
        kwargs={"repo": "acme/repo", "caller_root": str(caller)},
    )
    second_job = GitJob(
        "repo-two",
        "prepare_intake",
        30,
        kwargs={"repo": "acme/repo", "caller_root": str(caller)},
        repository_lock_wait_timeout_s=1,
    )

    def run_first() -> None:
        first_results.append(pool._run_git(first_job))

    def run_second() -> None:
        try:
            second_results.append(pool._run_git(second_job))
        finally:
            second_complete.set()

    first = threading.Thread(target=run_first)
    second = threading.Thread(target=run_second)
    try:
        first.start()
        assert first_entered.wait(2.0)
        second.start()
        completed_within_budget = second_complete.wait(2.0)
    finally:
        release_first.set()
        first.join(timeout=5.0)
        second.join(timeout=5.0)
        pool.shutdown(mark_interrupted=False)

    assert completed_within_budget
    assert not first.is_alive()
    assert not second.is_alive()
    assert first_results and first_results[0].ok
    assert second_results and second_results[0].error == "lock_timeout"
    second_manager.prepare.assert_not_called()


def test_checkout_network_budget_starts_after_repository_lock_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkout admission time does not reduce the Git operation budget."""
    clock = {"now": 10.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    pool = worker_pool.WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
    )
    waiting = threading.Event()
    results: list[JobResult] = []

    # Record real file-lock contention. Keep the actual acquisition and release.
    @contextmanager
    def observe_lock(
        path: Path, *, blocking: bool = True, require_exclusive: bool = False
    ) -> Iterator[None]:
        try:
            with file_lock(path, blocking=blocking, require_exclusive=require_exclusive):
                yield
        except LockUnavailableError:
            waiting.set()
            raise

    def dispatch(job: GitJob) -> JobResult:
        assert job.deadline_s == 120.0
        return JobResult(ok=True)

    monkeypatch.setattr(repository_lock, "file_lock", observe_lock)
    monkeypatch.setattr(pool, "_dispatch_git_op", dispatch)
    holder = repository_lock.RepositoryOperationLock("repo", lock_dir=tmp_path)

    def run_job() -> None:
        results.append(
            pool._run_git(GitJob("repo", "clone", 60, repository_lock_wait_timeout_s=120))
        )

    thread = threading.Thread(target=run_job)
    try:
        with holder.acquire(operation="commit_push", timeout_s=1):
            thread.start()
            assert waiting.wait(2)
            assert not results
            clock["now"] = 60.0
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert results and results[0].ok
    finally:
        pool.shutdown(mark_interrupted=False)
        thread.join(timeout=2)


def test_github_job_budget_includes_the_repository_lock(tmp_path: Path) -> None:
    """A GitHub job must use its configured budget before its runner starts."""
    from hephaestus.automation.pipeline.github_jobs import (
        AppendReplyJournalRequest,
        GitHubJob,
    )

    runner = Mock(gh_timeout=1)
    pool = worker_pool.WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
        github_job_runner=runner,
    )
    started = threading.Event()
    completed = threading.Event()
    results: list[JobResult] = []
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=1:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(1, marker, f"{marker}\n<!-- payload -->")
    job = GitHubJob("repo", tmp_path, request, descr="append journal")

    def run_job() -> None:
        started.set()
        try:
            results.append(pool._run_github(job))
        finally:
            completed.set()

    thread = threading.Thread(target=run_job)
    try:
        with pool._repo_lock("repo"):
            thread.start()
            assert started.wait(1)
            completed_while_locked = completed.wait(2)
        thread.join(timeout=2)
        assert completed_while_locked
        assert results and results[0].error == "lock_timeout"
        assert isinstance(results[0].value, dict)
        assert results[0].value["holder_operation"] == "repository_operation"
        assert results[0].value["holder_source"] == "in_process"
        runner.run.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)
        thread.join(timeout=2)
