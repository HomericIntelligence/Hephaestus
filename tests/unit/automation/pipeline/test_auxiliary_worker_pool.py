"""Behavior tests for the closed auxiliary worker pool."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.automation.pipeline.jobs import AgentJob


class _Host:
    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        return AthenaSkillResult(
            kind=str(request.kind), receipt={"worker": threading.current_thread().name}
        )


def _learning_job(tmp_path: Path) -> AthenaSkillJob:
    """Return one valid host-learning job for worker tests."""
    return AthenaSkillJob(
        request=AthenaSkillRequest(
            kind="learn",
            repo="Hephaestus",
            issue=3051,
            agent="codex",
            model="",
            cwd=tmp_path,
            timeout_s=10,
        )
    )


@pytest.mark.parametrize("exception_type", [SystemExit, KeyboardInterrupt, GeneratorExit])
@pytest.mark.parametrize("job_kind", ["learning", "cleanup"])
def test_process_control_failure_on_worker_thread_publishes_once(
    tmp_path: Path,
    exception_type: type[BaseException],
    job_kind: str,
) -> None:
    """A process-control failure on a worker thread releases its completion."""
    started = threading.Event()
    release = threading.Event()
    completions: queue.Queue[tuple[JobHandle, JobResult]] = queue.Queue(maxsize=1)
    wakeup = threading.Event()
    saturation = threading.Event()

    class RaisingHost:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            del request
            started.set()
            assert release.wait(timeout=2)
            raise exception_type("controlled failure")

    def raising_cleanup(job: GitJob) -> JobResult:
        del job
        started.set()
        assert release.wait(timeout=2)
        raise exception_type("controlled failure")

    pool = AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=RaisingHost(),
        cleanup_runner=raising_cleanup,
    )
    pool.set_completion_notifiers(wakeup=wakeup, saturation=saturation)
    job = (
        _learning_job(tmp_path)
        if job_kind == "learning"
        else GitJob(repo="Hephaestus", op="remove_worktree", timeout_s=10)
    )

    handle = pool.submit(job, "DONE")
    assert started.wait(timeout=1)
    release.set()
    done, result = completions.get(timeout=2)

    assert done is handle
    assert not result.ok
    assert result.error == f"worker_crash: {exception_type.__name__}: controlled failure"
    assert result.worker_id.startswith("hephaestus-learning-worker-")
    assert result.duration_s >= 0
    assert wakeup.is_set()
    assert not saturation.is_set()
    assert completions.empty()
    assert not pool._futures
    pool.shutdown(mark_interrupted=False)


class _CompletedFutureExecutor:
    """Return an exceptional future before callback registration."""

    def __init__(self, exception: BaseException) -> None:
        self.future: Future[JobResult] = Future()
        self.future.set_exception(exception)

    def submit(self, function: object, job: object) -> Future[JobResult]:
        del function, job
        return self.future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        del wait, cancel_futures


@pytest.mark.parametrize("exception_type", [SystemExit, KeyboardInterrupt, GeneratorExit])
def test_immediate_callback_process_control_failure_publishes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    """An already-complete future releases its handle and sends one wakeup."""
    executor = _CompletedFutureExecutor(exception_type("controlled failure"))
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.auxiliary_worker_pool.ThreadPoolExecutor",
        lambda **_kwargs: executor,
    )
    completions: queue.Queue = queue.Queue(maxsize=1)
    wakeup = threading.Event()
    saturation = threading.Event()
    pool = AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=_Host(),
    )
    pool.set_completion_notifiers(wakeup=wakeup, saturation=saturation)

    handle = pool.submit(_learning_job(tmp_path), "DONE")
    done, result = completions.get_nowait()

    assert done is handle
    assert not result.ok
    assert result.error == f"worker_crash: {exception_type.__name__}: controlled failure"
    assert wakeup.is_set()
    assert not saturation.is_set()
    assert not pool._futures
    pool.shutdown(mark_interrupted=False)


def test_process_control_failure_with_full_completion_queue_signals_fault(
    tmp_path: Path,
) -> None:
    """A full completion queue releases the future and signals saturation."""
    completions: queue.Queue = queue.Queue(maxsize=1)
    completions.put_nowait((object(), JobResult(ok=True)))
    wakeup = threading.Event()
    saturation = threading.Event()
    pool = AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=_Host(),
    )
    pool.set_completion_notifiers(wakeup=wakeup, saturation=saturation)
    future: Future[JobResult] = Future()
    future.set_exception(SystemExit("controlled failure"))
    pool._futures.add(future)
    job_handle = JobHandle(job=_learning_job(tmp_path), on_done_state="DONE")

    started = time.monotonic()
    pool._publish(job_handle, future)

    assert time.monotonic() - started < 1
    assert not pool._futures
    assert saturation.is_set()
    assert wakeup.is_set()
    assert completions.qsize() == 1
    pool.shutdown(mark_interrupted=False)


def test_learning_workers_are_distinct_and_reject_generic_agents(tmp_path: Path) -> None:
    """The lane has distinct workers and no generic agent dispatch surface."""
    assert (
        importlib.util.find_spec("hephaestus.automation.pipeline.auxiliary_worker_pool") is not None
    )
    auxiliary = importlib.import_module("hephaestus.automation.pipeline.auxiliary_worker_pool")
    completions: queue.Queue = queue.Queue(maxsize=1)
    pool = auxiliary.AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=_Host(),
    )
    request = AthenaSkillRequest(
        kind="learn",
        repo="Hephaestus",
        issue=2705,
        agent="codex",
        model="",
        cwd=tmp_path,
        timeout_s=10,
    )
    handle = pool.submit(AthenaSkillJob(request=request), "DONE")
    done, result = completions.get(timeout=2)

    assert done is handle
    assert result.ok
    learning_worker = result.value.receipt["worker"]
    assert learning_worker.startswith("hephaestus-learning-worker-")
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="hephaestus-pipeline-worker-"
    ) as main:
        main_worker = main.submit(lambda: threading.current_thread().name).result()
    assert learning_worker != main_worker
    with pytest.raises(TypeError, match="does not accept"):
        pool.submit(AgentJob("r", 1, "codex", "", lambda: "", tmp_path, 1), "DONE")
    pool.shutdown(mark_interrupted=False)


def test_learning_dependency_graph_excludes_agent_runtime_and_pi() -> None:
    """The closed auxiliary module has no provider or generic-worker import."""
    spec = importlib.util.find_spec("hephaestus.automation.pipeline.auxiliary_worker_pool")
    assert spec is not None and spec.origin is not None
    module_path = Path(spec.origin)
    imported = {
        alias.name
        for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
    )

    assert not any(
        "worker_pool" in name and "auxiliary_worker_pool" not in name for name in imported
    )
    assert not any("agents.runtime" in name or "pi_" in name for name in imported)


def test_graceful_coordinator_signal_does_not_rewrite_completed_result(tmp_path: Path) -> None:
    """A graceful stop lets active host work publish its real result."""
    auxiliary = importlib.import_module("hephaestus.automation.pipeline.auxiliary_worker_pool")
    graceful = threading.Event()
    forced = threading.Event()
    completions: queue.Queue = queue.Queue(maxsize=1)

    class BlockingHost(_Host):
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            graceful.wait(timeout=1)
            time.sleep(0.01)
            return super().execute(request)

    pool = auxiliary.AuxiliaryWorkerPool(
        size=1,
        shutdown=forced,
        completion_q=completions,
        athena_skill_executor=BlockingHost(),
    )
    request = AthenaSkillRequest(
        kind="learn",
        repo="Hephaestus",
        issue=1,
        agent="codex",
        model="",
        cwd=tmp_path,
        timeout_s=10,
    )
    pool.submit(AthenaSkillJob(request=request), "DONE")
    graceful.set()
    _handle, result = completions.get(timeout=2)

    assert result.ok
    assert not result.interrupted
    pool.shutdown(mark_interrupted=False)


def test_forced_shutdown_publishes_cancelled_queued_job(tmp_path: Path) -> None:
    """A queued host job becomes an explicit resumable completion."""
    auxiliary = importlib.import_module("hephaestus.automation.pipeline.auxiliary_worker_pool")
    forced = threading.Event()
    completions: queue.Queue = queue.Queue(maxsize=2)

    class BlockingHost(_Host):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            self.started.set()
            self.release.wait(timeout=2)
            return super().execute(request)

    host = BlockingHost()
    pool = auxiliary.AuxiliaryWorkerPool(
        size=1, shutdown=forced, completion_q=completions, athena_skill_executor=host
    )
    request = AthenaSkillRequest(
        kind="learn",
        repo="Hephaestus",
        issue=1,
        agent="codex",
        model="",
        cwd=tmp_path,
        timeout_s=10,
    )
    pool.submit(AthenaSkillJob(request=request), "DONE")
    pool.submit(AthenaSkillJob(request=request), "DONE")
    assert host.started.wait(timeout=1)

    pool.shutdown()
    host.release.set()
    results = [completions.get(timeout=2)[1], completions.get(timeout=2)[1]]

    assert any(result.error == "interrupted_before_start" for result in results)
