"""Closed worker lane for host learning and terminal cleanup."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

from .athena_skill_jobs import AthenaSkillExecutor, AthenaSkillJob
from .git_jobs import GitJob
from .job_results import JobHandle, JobResult

AuxiliaryJob = AthenaSkillJob | GitJob
CleanupRunner = Callable[[GitJob], JobResult]
_CLEANUP_OPS = frozenset({"remove_worktree", "release_branch_reservation"})


class AuxiliaryWorkerPool:
    """Execute a strict auxiliary-job allowlist on independent threads."""

    def __init__(
        self,
        *,
        size: int,
        shutdown: threading.Event,
        completion_q: queue.Queue[tuple[JobHandle, JobResult]],
        athena_skill_executor: AthenaSkillExecutor,
        cleanup_runner: CleanupRunner | None = None,
    ) -> None:
        """Create a bounded host-only executor."""
        if size < 1:
            raise ValueError("auxiliary worker size must be positive")
        self._shutdown = shutdown
        self._completion_q = completion_q
        self.completion_q = completion_q
        self._athena_skill_executor = athena_skill_executor
        self._cleanup_runner = cleanup_runner
        self._executor = ThreadPoolExecutor(
            max_workers=size,
            thread_name_prefix="hephaestus-learning-worker-",
        )
        self._completion_wakeup: threading.Event | None = None
        self._completion_saturation: threading.Event | None = None

    def set_completion_notifiers(
        self, *, wakeup: threading.Event, saturation: threading.Event
    ) -> None:
        """Install coordinator latches without adding a data channel."""
        self._completion_wakeup = wakeup
        self._completion_saturation = saturation

    def submit(self, job: object, on_done_state: str) -> JobHandle:
        """Submit one allowlisted auxiliary job."""
        if isinstance(job, AthenaSkillJob):
            if job.request.kind != "learn":
                raise TypeError("auxiliary worker pool accepts only Athena learn jobs")
        elif isinstance(job, GitJob):
            if job.op not in _CLEANUP_OPS:
                raise TypeError("auxiliary worker pool accepts only cleanup Git jobs")
        else:
            raise TypeError(f"auxiliary worker pool does not accept {type(job).__name__}")
        handle = JobHandle(job=job, on_done_state=on_done_state)
        future = self._executor.submit(self._run, job)
        future.add_done_callback(lambda completed: self._publish(handle, completed))
        return handle

    def _run(self, job: AuxiliaryJob) -> JobResult:
        start = time.monotonic()
        if self._shutdown.is_set():
            return JobResult(ok=False, interrupted=True, error="interrupted_before_start")
        try:
            if isinstance(job, AthenaSkillJob):
                value = self._athena_skill_executor.execute(job.request)
                result = JobResult(ok=value.ok, value=value, error=value.error)
            else:
                if self._cleanup_runner is None:
                    raise RuntimeError("cleanup job submitted without a cleanup runner")
                result = self._cleanup_runner(job)
        except Exception as exc:
            result = JobResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if self._shutdown.is_set():
            result = replace(result, ok=False, interrupted=True)
        return replace(
            result,
            duration_s=time.monotonic() - start,
            worker_id=threading.current_thread().name,
        )

    def _publish(self, handle: JobHandle, future: Future[JobResult]) -> None:
        try:
            result = future.result()
        except BaseException as exc:
            result = JobResult(ok=False, error=f"worker_crash: {type(exc).__name__}: {exc}")
        try:
            self._completion_q.put_nowait((handle, result))
        except queue.Full:
            if self._completion_saturation is not None:
                self._completion_saturation.set()
        finally:
            if self._completion_wakeup is not None:
                self._completion_wakeup.set()

    def shutdown(self, *, mark_interrupted: bool = True) -> None:
        """Stop pending work and optionally mark active work interrupted."""
        if mark_interrupted:
            self._shutdown.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
