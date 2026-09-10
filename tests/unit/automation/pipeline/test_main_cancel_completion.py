"""Each submitted operation must publish one completion on shutdown."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool


def test_shutdown_publishes_one_result_for_each_submitted_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that is cancelled before it starts retains its completion."""
    started = threading.Event()
    release = threading.Event()
    completion_q = CompletionQueue(maxsize=2)
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    executed: list[BuildTestJob] = []

    def hold_first_job(_pool: WorkerPool, job: BuildTestJob) -> JobResult:
        executed.append(job)
        started.set()
        assert release.wait(timeout=3), "The test did not release the running job"
        return JobResult(ok=True)

    monkeypatch.setattr(WorkerPool, "_run_build_test", hold_first_job)
    running = BuildTestJob("example/project", tmp_path, ("unused",), 5)
    queued = BuildTestJob("example/project", tmp_path, ("unused",), 5)
    try:
        running_handle = pool.submit(running, StageName.IMPLEMENTATION)
        assert started.wait(timeout=1)
        queued_handle = pool.submit(queued, StageName.IMPLEMENTATION)
        pool.shutdown()
        release.set()
        completions = dict(completion_q.get(timeout=2) for _ in range(2))
        assert set(completions) == {running_handle, queued_handle}
        assert completions[running_handle].interrupted
        assert not completions[running_handle].ok
        assert completions[queued_handle].interrupted
        assert not completions[queued_handle].ok
        assert completions[queued_handle].error == "interrupted_before_start"
        assert executed == [running]
        with pytest.raises(queue.Empty):
            completion_q.get_nowait()
    finally:
        release.set()
        pool.shutdown()
