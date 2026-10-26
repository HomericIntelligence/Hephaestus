"""Tests for remediation operation-wide monotonic deadlines."""

from __future__ import annotations

import queue
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.automation import git_runtime, git_utils
from hephaestus.automation.pipeline import repository_lock
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.repository_lock import RepositoryOperationLock
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.utils.file_lock import LockUnavailableError, file_lock


def test_git_runtime_refuses_commit_after_the_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An expired operation must not start another Git child."""
    child = Mock()
    monkeypatch.setattr(git_runtime, "_shared_run_git", child)
    monkeypatch.setattr("hephaestus.automation.git_runtime.time.monotonic", lambda: 11.0)

    with (
        git_runtime.operation_deadline(10.0),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        git_runtime.run(["git", "commit"], cwd=tmp_path, timeout=120)

    child.assert_not_called()


def test_git_runtime_forwards_cancellation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The automation Git boundary must retain child cancellation."""
    shutdown = threading.Event()
    child = Mock(return_value=subprocess.CompletedProcess(["git"], 0, "", ""))
    monkeypatch.setattr(git_runtime, "_shared_run_git", child)
    git_runtime.run(["git", "status"], cwd=tmp_path, shutdown=shutdown)
    assert child.call_args.kwargs["shutdown"] is shutdown


def test_checkout_timeout_after_admission_is_not_lock_contention(tmp_path: Path) -> None:
    """A Git timeout after both repository locks stays an operation timeout."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    pool._dispatch_git_op = Mock(  # type: ignore[method-assign]
        side_effect=subprocess.TimeoutExpired("git fetch", 30)
    )
    try:
        result = pool._run_git(
            GitJob(
                "repo",
                "clone",
                30,
                repository_lock_wait_timeout_s=5,
            )
        )
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.error == "timeout"
    assert result.value is None


@pytest.mark.parametrize("layer", ["primary", "owner"])
@pytest.mark.parametrize("contended", [False, True])
def test_git_operation_deadline_expiry_during_file_lock_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
    contended: bool,
) -> None:
    """An expired file-lock admission cannot publish owner data or dispatch."""
    now = [1.0]
    monkeypatch.setattr("hephaestus.automation.pipeline.worker_pool.time.monotonic", lambda: now[0])
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    entry = RepositoryOperationLock("repo", lock_dir=tmp_path / "locks", monotonic=lambda: now[0])
    pool._repo_locks["repo"] = entry
    publish = Mock(wraps=entry._write_owner_record)
    monkeypatch.setattr(entry, "_write_owner_record", publish)
    dispatch = Mock(return_value=JobResult(ok=True))
    monkeypatch.setattr(pool, "_dispatch_locked_git", dispatch)

    @contextmanager
    def expire_on_file_lock(
        path: Path, *, blocking: bool, require_exclusive: bool
    ) -> Iterator[None]:
        selected = path.name.endswith(".owner.lock") == (layer == "owner")
        if selected and contended:
            now[0] = 11.0
            raise LockUnavailableError("held")
        with file_lock(path, blocking=blocking, require_exclusive=require_exclusive):
            if selected:
                now[0] = 11.0
            yield

    monkeypatch.setattr(repository_lock, "file_lock", expire_on_file_lock)
    try:
        result = pool._run_git(GitJob("repo", "clone", 120, deadline_s=10.0))
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.error == "timeout"
    assert not result.ok
    publish.assert_not_called()
    dispatch.assert_not_called()
    assert not list((tmp_path / "locks").glob("*.owner.json"))
    assert entry.users == 0
    assert not entry.lock.locked()


def test_checkout_admission_starts_operation_deadline_after_both_repository_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout starts only after the actual metadata holder releases it."""
    lock_dir = tmp_path / "locks"
    holder = RepositoryOperationLock("repo", lock_dir=lock_dir)
    job = GitJob("repo", "clone", 30, repository_lock_wait_timeout_s=0.01)
    owner_record = Path(f"{repository_lock.repo_lock_path('repo', lock_dir)}.owner.json")
    with holder.acquire(operation="commit_push", timeout_s=1):
        assert owner_record.is_file()
        refusal_pool = WorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=queue.Queue(),
            lock_dir=lock_dir,
        )
        refusal_dispatch = Mock()
        refusal_pool._dispatch_git_op = refusal_dispatch  # type: ignore[method-assign]
        try:
            refused = refusal_pool._run_git(job)
        finally:
            refusal_pool.shutdown(mark_interrupted=False)
        assert refused.error == "lock_timeout"
        assert isinstance(refused.value, dict)
        assert refused.value["holder_operation"] == "commit_push"
        assert refused.value["holder_source"] == "owner_sidecar"
        refusal_dispatch.assert_not_called()
    assert not owner_record.exists()

    now = [100.0]

    def monotonic() -> float:
        return now[0]

    def advance(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("hephaestus.automation.pipeline.worker_pool.time.monotonic", monotonic)
    admitted_pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=lock_dir,
    )
    dispatch = Mock(return_value=JobResult(ok=True))
    admitted_pool._dispatch_git_op = dispatch  # type: ignore[method-assign]
    original_dispatch = admitted_pool._dispatch_admitted_git
    post_admission: list[float] = []

    def dispatch_after_admission(admitted_job: GitJob, prepared: Any) -> JobResult:
        advance(0.02)
        post_admission.append(monotonic())
        return original_dispatch(admitted_job, prepared)

    monkeypatch.setattr(admitted_pool, "_dispatch_admitted_git", dispatch_after_admission)
    try:
        admitted = admitted_pool._run_git(job)
    finally:
        admitted_pool.shutdown(mark_interrupted=False)

    assert admitted.ok
    dispatch.assert_called_once()
    dispatched_job = dispatch.call_args.args[0]
    assert isinstance(dispatched_job, GitJob)
    assert post_admission == [100.02]
    assert dispatched_job.deadline_s == post_admission[0] + job.timeout_s
    assert dispatched_job.repository_lock_wait_timeout_s is None


def test_checkout_interrupt_after_admission_is_not_lock_contention(tmp_path: Path) -> None:
    """A shutdown from the Git operation stays an interrupted operation."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    pool._dispatch_git_op = Mock(side_effect=InterruptedError("stop"))  # type: ignore[method-assign]
    try:
        result = pool._run_git(
            GitJob(
                "repo",
                "sync_checkout",
                30,
                kwargs={"dest": str(tmp_path / "checkout")},
                repository_lock_wait_timeout_s=5,
            )
        )
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.error == "interrupted"
    assert result.interrupted


def test_repository_lock_wait_ends_on_shutdown(tmp_path: Path) -> None:
    """A waiting job releases its lock reference before the holder exits."""
    shutdown = threading.Event()
    pool = WorkerPool(size=1, shutdown=shutdown, completion_q=queue.Queue(), lock_dir=tmp_path)
    entered = threading.Event()
    completed = threading.Event()
    failures: list[BaseException] = []

    def wait_for_lock() -> None:
        entered.set()
        try:
            with pool._repo_lock("repo"):
                pass
        except BaseException as error:
            failures.append(error)
        finally:
            completed.set()

    waiter = threading.Thread(target=wait_for_lock)
    try:
        with pool._repo_lock("repo"):
            waiter.start()
            assert entered.wait(1)
            shutdown.set()
            stopped_while_held = completed.wait(1)
        waiter.join(timeout=2)
    finally:
        pool.shutdown(mark_interrupted=False)

    assert stopped_while_held
    assert failures
    assert not pool._repo_locks


def test_publication_starts_neither_push_nor_probe_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An expired publication must not start its push or uncertainty probe."""
    child = Mock()
    monkeypatch.setattr(git_runtime, "_shared_run_git", child)
    monkeypatch.setattr("hephaestus.automation.git_runtime.time.monotonic", lambda: 11.0)

    with (
        git_runtime.operation_deadline(10.0),
        pytest.raises(git_utils.BranchPublicationRemoteProbeError) as raised,
    ):
        git_utils.push_head_to_branch(
            "fix/one",
            "a" * 40,
            tmp_path,
            source_sha="b" * 40,
            timeout=120,
        )

    assert raised.value.failure_kind == "timeout"
    child.assert_not_called()


def test_failed_push_does_not_probe_after_the_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed push must not start its uncertainty probe after expiry."""
    child = Mock(side_effect=subprocess.CalledProcessError(1, ["git", "push"]))
    monkeypatch.setattr(git_runtime, "_shared_run_git", child)
    clock = iter((1.0, 3.0))
    monkeypatch.setattr("hephaestus.automation.git_runtime.time.monotonic", lambda: next(clock))

    with (
        git_runtime.operation_deadline(2.0),
        pytest.raises(git_utils.BranchPublicationRemoteProbeError) as raised,
    ):
        git_utils.push_head_to_branch(
            "fix/one",
            "a" * 40,
            tmp_path,
            source_sha="b" * 40,
            timeout=120,
        )

    assert raised.value.failure_kind == "timeout"
    assert child.call_count == 1


def test_delivery_file_lock_wait_stops_at_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The PR reply lock wait cannot consume time after delivery expires."""
    now = [1.0]
    adapter = PipelineGitHub("org", repo="repo", repo_root=tmp_path)

    def unavailable(*_args: object, **_kwargs: object) -> object:
        now[0] = 11.0
        raise LockUnavailableError("held")

    monkeypatch.setattr("hephaestus.automation.operation_deadlines.file_lock", unavailable)
    monkeypatch.setattr(
        "hephaestus.automation.operation_deadlines.time.sleep", lambda _seconds: None
    )
    monkeypatch.setattr("hephaestus.automation.operation_deadlines.time.monotonic", lambda: now[0])

    with (
        adapter.operation_deadline(10.0),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        with adapter._operation_file_lock(tmp_path / "reply.lock", require_exclusive=True):
            raise AssertionError("the held lock must not be acquired")
