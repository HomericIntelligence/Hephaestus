"""Tests for the total budget of ordinary queued Git work."""

import queue
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.automation import git_runtime
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult


def test_git_job_budget_includes_the_repository_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary Git job must expire while another job holds its lock."""
    pool = worker_pool.WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
    )
    started = threading.Event()
    completed = threading.Event()
    results: list[JobResult] = []
    dispatch = Mock(return_value=JobResult(ok=True))
    monkeypatch.setattr(pool, "_dispatch_git_op", dispatch)

    def run_job() -> None:
        started.set()
        try:
            results.append(pool._run_git(GitJob("repo", "clone", 1)))
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
        result = pool._run_git(GitJob("repo", "clone", 1))
        assert not result.ok
        assert result.error == "timeout"
    finally:
        pool.shutdown(mark_interrupted=False)


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
        assert results and results[0].error == "github_timeout"
        runner.run.assert_not_called()
    finally:
        pool.shutdown(mark_interrupted=False)
        thread.join(timeout=2)
