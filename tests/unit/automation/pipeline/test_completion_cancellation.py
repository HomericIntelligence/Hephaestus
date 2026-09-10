"""Distinguish cancelled futures from exceptions raised by running jobs."""

import queue
import threading
from concurrent.futures import CancelledError, Future
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool


@pytest.mark.parametrize("auxiliary", [False, True], ids=["main", "auxiliary"])
@pytest.mark.parametrize("cancelled_before_start", [False, True])
def test_worker_completion_keeps_the_cancellation_origin(
    tmp_path: Path, auxiliary: bool, cancelled_before_start: bool
) -> None:
    """A running job's cancellation exception must produce one completion."""
    completions: queue.Queue[tuple[JobHandle, JobResult]] = queue.Queue(maxsize=1)
    shutdown = threading.Event()
    job = GitJob("repo", "remove_worktree", 10, kwargs={"path": tmp_path})
    handle = JobHandle(job, "DONE")
    future: Future[JobResult] = Future()
    if cancelled_before_start:
        assert future.cancel()
    else:
        future.set_exception(CancelledError("running operation stopped"))

    if auxiliary:
        pool = AuxiliaryWorkerPool(
            size=1, shutdown=shutdown, completion_q=completions, athena_skill_executor=None
        )
        pool._futures.add(future)
        pool._publish(handle, future)
        pool.shutdown(mark_interrupted=False)
    else:
        main_pool = WorkerPool(size=1, shutdown=shutdown, completion_q=completions)
        main_pool._on_future_done(handle, future)
        main_pool.shutdown(mark_interrupted=False)

    returned_handle, result = completions.get_nowait()
    assert returned_handle is handle
    assert not result.ok
    if cancelled_before_start:
        assert result.interrupted
        assert result.error == "interrupted_before_start"
    else:
        assert result.error == "worker_crash: CancelledError: running operation stopped"
    assert completions.empty()


@pytest.mark.parametrize("auxiliary", [False, True], ids=["main", "auxiliary"])
def test_interrupted_shutdown_waits_for_running_work(auxiliary: bool) -> None:
    """Interrupted shutdown must reap active work before it returns."""
    completions: queue.Queue[tuple[JobHandle, JobResult]] = queue.Queue(maxsize=1)
    shutdown = threading.Event()
    pool: WorkerPool | AuxiliaryWorkerPool
    if auxiliary:
        pool = AuxiliaryWorkerPool(
            size=1, shutdown=shutdown, completion_q=completions, athena_skill_executor=None
        )
    else:
        pool = WorkerPool(size=1, shutdown=shutdown, completion_q=completions)
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def active_work() -> None:
        started.set()
        release.wait()

    def stop_pool() -> None:
        pool.shutdown(mark_interrupted=True)
        stopped.set()

    pool._executor.submit(active_work)
    assert started.wait(timeout=1)
    shutdown_thread = threading.Thread(target=stop_pool)
    shutdown_thread.start()
    try:
        assert not stopped.wait(timeout=0.05)
    finally:
        release.set()
        assert stopped.wait(timeout=2)
        shutdown_thread.join(timeout=1)
