"""Check stopped-loop admission and actual pool callback exit.

Apply as tests/unit/automation/test_fleet_worker_runtime_races.py together with
test_fleet_worker_runtime.py. These are proposed qualification tests, not run
evidence. The bridge cases use a separate public runner and pool over the actual
runtime loop, client and journal. They do not qualify dynamic registration.
The callback case uses the concrete factory's own runner and pool.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_jobs import (
    FleetBuildJobContext,
    FleetBuildJobRunner,
    _OwnerObservation,
)
from hephaestus.automation.fleet_build_service import FleetBuildOwner, FleetBuildService
from hephaestus.automation.fleet_worker_runtime import FleetWorkerRuntime
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from tests.unit.automation.test_fleet_build_jobs import PreparedCase, lock_is_held
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP
from tests.unit.automation.test_fleet_worker_runtime import (
    runtime_case as runtime_case,
    writer_held,
)

pytestmark = pytest.mark.precommit


@dataclass
class Gate:
    """Hold an actual operation until the test permits its bounded exit."""

    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def hold(self) -> None:
        """Acknowledge entry and fail if the test does not release the gate."""
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError("the test did not release its operation gate")


class CleanupObserver:
    """Keep actual failed-loop task gathering pending without blocking the loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.future = asyncio.run_coroutine_threadsafe(self.observe(), loop)
        assert self.started.wait(2), "the actual loop observer did not start"

    async def observe(self) -> None:
        """Acknowledge real cancellation before a finite asynchronous cleanup."""
        self.started.set()
        try:
            try:
                await asyncio.Future()
            except asyncio.CancelledError as error:
                self.cancelled.set()
                deadline = time.monotonic() + 10
                while not self.release.is_set():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "the test did not release failed-loop cleanup"
                        ) from error
                    await asyncio.sleep(0.01)
        finally:
            self.exited.set()

    def close(self) -> None:
        """Release the actual task and wait for its coroutine to exit."""
        self.release.set()
        if not self.cancelled.is_set() and not self.exited.is_set():
            self.future.cancel()
        assert self.exited.wait(3), "the actual loop observer did not exit"


@dataclass
class BridgeCase:
    """Keep real borrowed source and runtime resources through pool completion."""

    prepared: PreparedCase
    runtime: FleetWorkerRuntime
    runner: FleetBuildJobRunner
    pool: WorkerPool
    completions: CompletionQueue
    observers: list[CleanupObserver] = field(default_factory=list)

    def observer(self) -> CleanupObserver:
        """Retain one actual task so failed-loop cleanup stays observable."""
        loop = self.runtime.loop
        assert loop is not None
        observer = CleanupObserver(loop)
        self.observers.append(observer)
        return observer


@pytest.fixture
def bridge_case(
    tmp_path: Path,
    runtime_case: tuple[FleetWorkerRuntime, BuildConsumerHTTP],
) -> Iterator[BridgeCase]:
    """Bind a public runner mapping to actual runtime and source capabilities."""
    prepared = PreparedCase(tmp_path)
    pool: WorkerPool | None = None
    bridge: BridgeCase | None = None
    try:
        prepared.stop_publication.set()
        prepared.publication.join(2)
        assert not prepared.publication.is_alive(), "the fixture publisher did not stop"
        assert prepared.publisher_error is None
        runtime, http = runtime_case
        runtime.start()
        loop, journal, client = runtime.loop, runtime.journal, runtime.client
        assert loop is not None and journal is not None
        # Copy controlled admission identities for the same actual fixture source.
        assert prepared.build.http is not None
        http.data = copy.deepcopy(prepared.build.http.data)
        http.record = copy.deepcopy(prepared.build.http.record)

        async def owner() -> FleetBuildOwner:
            return FleetBuildOwner(
                FleetBuildService(client, prepared.build.submission),
                journal,
                "job-context",
                cancellation_ids=lambda: ("job-stop", "job-stop-key"),
            )

        actual_owner = asyncio.run_coroutine_threadsafe(owner(), loop).result(timeout=2)
        command = prepared.build.publisher.command
        context = FleetBuildJobContext(
            owner=actual_owner,
            repository=command["payload"]["policy"]["workspace"]["repository"],
            source=prepared.build.publisher.source,
            submission=prepared.build.submission,
            parent=command["payload"]["parent"],
            snapshot=prepared.build.publisher.snapshot,
            source_lease=prepared.source_lease,
            result_handoff=prepared.result_handoff,
        )
        runner = FleetBuildJobRunner(
            loop=loop,
            contexts={"job-context": context},
            loop_failed=runtime._loop_failed,
        )
        completions = CompletionQueue(maxsize=2)
        pool = WorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=completions,
            lock_dir=tmp_path / "pool-locks",
            evidence_receipt_dir=tmp_path / "pool-receipts",
            fleet_build_runner=runner,
        )
        bridge = BridgeCase(prepared, runtime, runner, pool, completions)
        yield bridge
    finally:
        try:
            if bridge is not None:
                for observer in bridge.observers:
                    observer.close()
        finally:
            try:
                # The factory's own pool remains open, so cleanup retains its loop.
                # End every separate bridge borrower before fixture teardown closes it.
                if pool is not None:
                    pool.wait_for_exit()
            finally:
                prepared.close()


def _is_begin(callback: Callable[..., Any]) -> bool:
    """Identify only the real owner observation scheduling boundary."""
    return isinstance(getattr(callback, "__self__", None), _OwnerObservation) and (
        getattr(callback, "__name__", None) == "begin"
    )


def _assert_refused(
    case: BridgeCase,
    handle: JobHandle,
    before: bytes,
    http: BuildConsumerHTTP,
) -> None:
    """Observe real completion, source release and absence of owner effects."""
    completed, result = case.completions.get(timeout=5)
    case.pool.wait_for_exit()
    assert completed is handle
    assert not result.ok and not result.interrupted
    assert result.error == "fleet_build_refused: RuntimeError"
    assert len(case.prepared.source_deadlines) == 1
    assert case.prepared.evidence_deadlines == []
    assert not lock_is_held(case.prepared.source_lock)
    assert not case.prepared.build.publisher.scheduler.children
    assert http.requests == []
    journal = case.runtime.journal
    assert journal is not None
    assert (journal.directory / "receipts.jsonl").read_bytes() == before
    assert writer_held(journal)
    assert case.completions.empty()


def test_failed_loop_cleanup_cannot_admit_a_new_runner_observation(
    bridge_case: BridgeCase,
    runtime_case: tuple[FleetWorkerRuntime, BuildConsumerHTTP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running cleanup loop cannot clear the runner's shared failure latch."""
    case = bridge_case
    runtime, http = runtime_case
    loop, journal = runtime.loop, runtime.journal
    assert loop is not None and journal is not None
    observer = case.observer()
    loop.call_soon_threadsafe(loop.stop)
    assert observer.cancelled.wait(2), "actual failed-loop cleanup did not cancel its task"
    assert runtime._loop_failed.is_set() and loop.is_running()
    scheduled: list[Callable[..., Any]] = []
    actual_schedule = loop.call_soon_threadsafe

    def schedule(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if _is_begin(callback):
            scheduled.append(callback)
        return actual_schedule(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", schedule)
    before = (journal.directory / "receipts.jsonl").read_bytes()
    handle = case.pool.submit(replace(case.prepared.job(), timeout_s=4), StageName.IMPLEMENTATION)
    _assert_refused(case, handle, before, http)
    assert scheduled == [], "the runner queued begin on an already failed loop"
    assert loop.is_running() and not observer.exited.is_set()


def test_begin_queued_behind_loop_stop_cannot_start_owner_http(
    bridge_case: BridgeCase,
    runtime_case: tuple[FleetWorkerRuntime, BuildConsumerHTTP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real queued callback must recheck failure before creating owner work."""
    case = bridge_case
    runtime, http = runtime_case
    loop, journal = runtime.loop, runtime.journal
    assert loop is not None and journal is not None
    observer = case.observer()
    queued = threading.Event()
    actual_schedule = loop.call_soon_threadsafe

    def schedule(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if not _is_begin(callback):
            return actual_schedule(callback, *args, **kwargs)

        def queue_begin() -> None:
            loop.call_soon(callback, *args, **kwargs)

        def stop_then_queue() -> None:
            loop.stop()
            # Two actual ready-queue turns put begin after cleanup's task snapshot.
            # A missing begin fence can then perform its real SDK HTTP operation.
            loop.call_soon(queue_begin)

        result = actual_schedule(stop_then_queue)
        queued.set()
        return result

    monkeypatch.setattr(loop, "call_soon_threadsafe", schedule)
    before = (journal.directory / "receipts.jsonl").read_bytes()
    handle = case.pool.submit(replace(case.prepared.job(), timeout_s=4), StageName.IMPLEMENTATION)
    assert queued.wait(2), "the real runner did not schedule begin"
    assert observer.cancelled.wait(2), "the stopped loop did not enter task cleanup"
    assert runtime._loop_failed.is_set() and loop.is_running()
    _assert_refused(case, handle, before, http)
    assert not observer.exited.is_set()


def test_failed_loop_closes_sdk_only_after_actual_factory_pool_callback_exit(
    runtime_case: tuple[FleetWorkerRuntime, BuildConsumerHTTP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actual SDK task exit alone cannot release the factory pool's borrower."""
    runtime, http = runtime_case
    runtime.start()
    loop, pool, runner = runtime.loop, runtime.pool, runtime.runner
    worker, journal, client = runtime.worker, runtime.journal, runtime.client
    assert loop is not None and pool is not None and runner is not None
    assert worker is not None and journal is not None
    result_gate, callback_gate = Gate(), Gate()
    callback_exited, pool_exited = threading.Event(), threading.Event()
    release_callback = threading.Event()
    observations: list[dict[str, Any]] = []
    helper_errors: list[BaseException] = []
    actual_run = runner.run
    actual_done = pool._on_future_done
    actual_wait = pool.wait_for_exit
    actual_close = client.aclose

    def run(*args: Any, **kwargs: Any) -> JobResult:
        result = actual_run(*args, **kwargs)
        # Delay the real refusal so submit can register its real done callback.
        result_gate.hold()
        return result

    def done(handle: JobHandle, future: Future[JobResult]) -> None:
        try:
            callback_gate.hold()
            actual_done(handle, future)
        finally:
            callback_exited.set()

    def wait_for_exit() -> None:
        actual_wait()
        pool_exited.set()

    async def close_client() -> None:
        observations.append(
            {
                "callback_exited": callback_exited.is_set(),
                "pool_wait_returned": pool_exited.is_set(),
                "on_owner_loop": asyncio.get_running_loop() is loop,
                "journal_held": writer_held(journal),
            }
        )
        # On faulty ordering, record the violation before permitting callback exit.
        release_callback.set()
        await actual_close()

    def release_after_cleanup() -> None:
        try:
            assert release_callback.wait(3), "failed-loop cleanup made no progress"
        except BaseException as error:
            helper_errors.append(error)
        finally:
            callback_gate.release.set()

    def stop_with_sentinel() -> None:
        loop.call_later(0.05, release_callback.set)
        loop.stop()

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(pool, "_on_future_done", done)
    monkeypatch.setattr(pool, "wait_for_exit", wait_for_exit)
    monkeypatch.setattr(client, "aclose", close_client)
    helper: threading.Thread | None = None
    try:
        handle = pool.submit(
            BuildTestJob(
                repo="fixture/runtime-callback",
                cwd=worker.workspace_root,
                argv=("just", "test-unit"),
                timeout_s=4,
                descr="unregistered_fleet_build",
                fleet_context_id="unregistered-context",
            ),
            StageName.IMPLEMENTATION,
        )
        assert result_gate.entered.wait(2), "the actual factory runner did not return"
        result_gate.release.set()
        assert callback_gate.entered.wait(2), "the actual pool callback did not enter"
        assert runtime.completions.empty()
        helper = threading.Thread(target=release_after_cleanup, name="fixture-callback-release")
        helper.start()
        loop.call_soon_threadsafe(stop_with_sentinel)
        runtime.close()
    finally:
        result_gate.release.set()
        callback_gate.release.set()
        release_callback.set()
        if helper is not None:
            helper.join(3)
            assert not helper.is_alive(), "the callback release helper did not exit"

    assert helper_errors == []
    assert observations == [
        {
            "callback_exited": True,
            "pool_wait_returned": True,
            "on_owner_loop": True,
            "journal_held": True,
        }
    ]
    completed, result = runtime.completions.get(timeout=2)
    assert completed is handle
    assert not result.ok and not result.interrupted
    assert result.error == "fleet_build_refused: ValueError"
    assert runtime.completions.empty()
    assert http.requests == []
    assert callback_exited.is_set() and pool_exited.is_set()
    assert loop.is_closed() and client._client.is_closed
