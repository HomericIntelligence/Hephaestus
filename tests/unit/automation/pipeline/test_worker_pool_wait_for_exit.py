"""Check pool completion and submission ordering with actual jobs and callbacks."""

from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import hephaestus.utils.subprocess_registry as subprocess_registry
from hephaestus.automation.pipeline import worker_pool as pool_module
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.utils.helpers import run_subprocess


@dataclass
class Gate:
    """Stop at a real operation until the test or cleanup permits progress."""

    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def hold(self) -> None:
        """Report entry and require a bounded release."""
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError("test operation gate was not released")


class Call:
    """Run an owner operation and retain its actual return or exception."""

    def __init__(self, function: Callable[[], object], *, name: str) -> None:
        """Start one bounded test helper thread."""
        self.started = threading.Event()
        self.done = threading.Event()
        self.result: object = None
        self.error: BaseException | None = None

        def invoke() -> None:
            """Keep the operation's outcome and actual exit separate."""
            self.started.set()
            try:
                self.result = function()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=invoke, name=name)
        self.thread.start()
        assert self.started.wait(5)

    def finish(self) -> object:
        """Join the helper and propagate its actual exception."""
        self.thread.join(10)
        assert not self.thread.is_alive(), "test helper did not exit"
        if self.error is not None:
            raise self.error
        return self.result


class AthenaObserver:
    """Observe cancellation without constructing a skill runtime."""

    def __init__(self) -> None:
        """Start with no cancellation effects."""
        self.cancel_calls = 0

    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        """Reject unexpected skill execution in a local build fixture."""
        raise AssertionError(f"unexpected skill execution: {request.kind}")

    def cancel(self) -> None:
        """Record the caller's cancellation request."""
        self.cancel_calls += 1


class PoolCase:
    """Run real local commands with externally controlled admission gates."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prepare a pool without provider, controller or source discovery."""
        self.root = root
        self.shutdown = threading.Event()
        self.completions: CompletionQueue = queue.Queue()
        self.athena = AthenaObserver()
        self.pool = WorkerPool(
            size=1,
            shutdown=self.shutdown,
            completion_q=self.completions,
            athena_skill_executor=self.athena,
            lock_dir=root / "locks",
        )
        self.gates: list[Gate] = []
        self.calls: list[Call] = []
        self.commands: dict[str, Gate] = {}
        self.termination_calls: list[None] = []
        self.borrowed_released = threading.Event()

        @contextmanager
        def borrowed() -> Iterator[None]:
            """Hold a real context until its owner releases it."""
            try:
                yield
            finally:
                self.borrowed_released.set()

        lease = borrowed()
        lease.__enter__()
        self.pool._repo_intake_leases[root] = lease

        def execute(argv: list[str], **kwargs: Any) -> Any:
            """Gate the command, then execute the real subprocess helper."""
            self.commands[argv[-1]].hold()
            return run_subprocess(argv, **kwargs)

        def terminate() -> None:
            """Observe a forbidden global effect without killing other work."""
            self.termination_calls.append(None)

        monkeypatch.setattr(pool_module, "run_subprocess", execute)
        monkeypatch.setattr(subprocess_registry, "terminate_all", terminate)

    def gate(self) -> Gate:
        """Retain one gate for unconditional cleanup."""
        gate = Gate()
        self.gates.append(gate)
        return gate

    def job(self, name: str, *, returncode: int = 0) -> tuple[BuildTestJob, Gate]:
        """Prepare a command that writes its own completion output."""
        marker = self.root / name
        gate = self.gate()
        self.commands[str(marker)] = gate
        code = (
            "import sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text('executed'); "
            f"sys.exit({returncode})"
        )
        return (
            BuildTestJob(
                repo="example/pool-exit",
                cwd=self.root,
                argv=(sys.executable, "-I", "-c", code, str(marker)),
                timeout_s=30,
                descr=name,
            ),
            gate,
        )

    def call(self, function: Callable[[], object], *, name: str = "pool-owner") -> Call:
        """Keep a helper operation until cleanup joins it."""
        call = Call(function, name=name)
        self.calls.append(call)
        return call

    def close(self) -> None:
        """Release all test gates and join every real accepted operation."""
        for gate in self.gates:
            gate.release.set()
        errors: list[BaseException] = []
        for call in self.calls:
            try:
                call.finish()
            except BaseException as exc:
                errors.append(exc)
        try:
            self.pool._executor.shutdown(wait=True, cancel_futures=False)
        except BaseException as exc:
            errors.append(exc)
        try:
            self.pool.release_repo_intake_leases()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            for later in errors[1:]:
                errors[0].add_note(f"Additional cleanup failure: {later!r}")
            raise errors[0]


@pytest.fixture
def pool_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PoolCase]:
    """Keep cleanup outside all contract assertions."""
    case = PoolCase(tmp_path, monkeypatch)
    try:
        yield case
    finally:
        case.close()


@pytest.mark.parametrize("returncode", [0, 7], ids=["success", "failure"])
def test_wait_finishes_running_and_queued_work_without_cancellation(
    pool_case: PoolCase, returncode: int
) -> None:
    """Accepted commands finish, retain their outcomes, and publish once."""
    case = pool_case
    first, first_gate = case.job("first")
    second, second_gate = case.job("second", returncode=returncode)
    handles = [case.pool.submit(first, "done")]
    assert first_gate.entered.wait(5)
    handles.append(case.pool.submit(second, "done"))
    waiting = case.call(case.pool.wait_for_exit)
    assert not waiting.done.wait(0.1), "wait returned while accepted work was active"
    assert not second_gate.entered.is_set()
    first_gate.release.set()
    assert second_gate.entered.wait(5), "queued work was cancelled instead of executed"
    assert not waiting.done.is_set()
    second_gate.release.set()
    waiting.finish()

    completed = [case.completions.get(timeout=5) for _ in handles]
    assert [handle for handle, _ in completed] == handles
    assert [result.ok for _, result in completed] == [True, returncode == 0]
    assert all(not result.interrupted for _, result in completed)
    assert completed[1][1].error == (None if returncode == 0 else "rc=7")
    assert (case.root / "first").read_text() == "executed"
    assert (case.root / "second").read_text() == "executed"
    assert case.completions.empty()
    case.pool.wait_for_exit()
    assert not case.shutdown.is_set()
    assert case.athena.cancel_calls == 0
    assert case.termination_calls == []
    assert not case.borrowed_released.is_set()
    rejected, _ = case.job("rejected")
    with pytest.raises(RuntimeError):
        case.pool.submit(rejected, "done")
    assert not (case.root / "rejected").exists()


def test_wait_includes_callback_exit_after_actual_completion_publication(
    pool_case: PoolCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published result does not permit release while its callback is live."""
    case = pool_case
    callback_gate = case.gate()
    delegate = case.pool._on_future_done

    def callback(handle: JobHandle, future: Future[JobResult]) -> None:
        """Publish through the real callback before holding its return."""
        delegate(handle, future)
        callback_gate.hold()

    monkeypatch.setattr(case.pool, "_on_future_done", callback)
    job, job_gate = case.job("callback")
    handle = case.pool.submit(job, "done")
    assert job_gate.entered.wait(5)
    job_gate.release.set()
    assert callback_gate.entered.wait(5)
    completed, result = case.completions.get(timeout=5)
    assert completed is handle and result.ok
    waits = [case.call(case.pool.wait_for_exit, name=f"pool-owner-{i}") for i in range(2)]
    assert all(not call.done.wait(0.1) for call in waits)
    callback_gate.release.set()
    for call in waits:
        call.finish()
    assert case.completions.empty()


def test_wait_covers_accepted_submit_through_actual_callback_registration(
    pool_case: PoolCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seal cannot pass a completed future whose callback is not registered."""
    case = pool_case
    registration_gate = case.gate()
    delegate = Future.add_done_callback

    def register(future: Future[Any], callback: Callable[[Future[Any]], object]) -> None:
        """Pause this real accepted future after its command has exited."""
        if threading.current_thread().name == "pool-submit-contract":
            result = future.result(timeout=5)
            assert result.ok and (case.root / "registration").read_text() == "executed"
            registration_gate.hold()
        delegate(future, callback)

    monkeypatch.setattr(Future, "add_done_callback", register)
    job, job_gate = case.job("registration")
    job_gate.release.set()
    submission = case.call(lambda: case.pool.submit(job, "done"), name="pool-submit-contract")
    assert registration_gate.entered.wait(5)
    assert case.completions.empty()
    waiting = case.call(case.pool.wait_for_exit)
    assert not waiting.done.wait(0.1), "wait returned before accepted callback registration"
    registration_gate.release.set()
    handle = submission.finish()
    waiting.finish()
    completed, result = case.completions.get(timeout=5)
    assert completed is handle and result.ok and not result.interrupted
    assert case.completions.empty()


def test_wait_seals_submit_before_entering_executor_join(
    pool_case: PoolCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit cannot enter after sealing while executor shutdown is pending."""
    case = pool_case
    join_gate = case.gate()
    delegate = case.pool._executor.shutdown

    def shutdown(wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Pause before the actual executor performs its own seal."""
        join_gate.hold()
        delegate(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(case.pool._executor, "shutdown", shutdown)
    waiting = case.call(case.pool.wait_for_exit)
    assert join_gate.entered.wait(5)
    job, gate = case.job("sealed")
    gate.release.set()
    with pytest.raises(RuntimeError):
        case.pool.submit(job, "done")
    assert not gate.entered.is_set()
    join_gate.release.set()
    waiting.finish()
    assert case.completions.empty()
    assert not (case.root / "sealed").exists()


def test_legacy_shutdown_still_returns_early_and_cancels_queued_work(
    pool_case: PoolCase,
) -> None:
    """Keep the legacy cleanup behavior as a separate diagnostic control."""
    case = pool_case
    first, first_gate = case.job("legacy-running")
    second, second_gate = case.job("legacy-queued")
    first_handle = case.pool.submit(first, "done")
    assert first_gate.entered.wait(5)
    second_handle = case.pool.submit(second, "done")
    case.pool.shutdown(mark_interrupted=False)
    assert not first_gate.release.is_set()
    assert not (case.root / "legacy-running").exists()
    assert not second_gate.entered.is_set()
    cancelled_handle, cancelled = case.completions.get(timeout=5)
    assert cancelled_handle is second_handle
    assert not cancelled.ok and cancelled.interrupted
    assert cancelled.error == "interrupted_before_start"
    assert not case.shutdown.is_set()
    assert case.athena.cancel_calls == 1
    assert len(case.termination_calls) == 1
    first_gate.release.set()
    completed_handle, completed = case.completions.get(timeout=5)
    assert completed_handle is first_handle
    assert completed.ok and not completed.interrupted
    assert (case.root / "legacy-running").read_text() == "executed"
    assert not (case.root / "legacy-queued").exists()
    assert case.completions.empty()
