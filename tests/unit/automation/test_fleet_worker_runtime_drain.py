"""Close the actual CLI runtime while an admitted build owns live resources.

Apply as tests/unit/automation/test_fleet_worker_runtime_drain.py with the
registration fixture module. Admission and allocation identities are fixtures.
The provider, source, SDK HTTP, publication, collection and pool receipts are
real. The fixture supplies source and evidence capabilities in process; it does
not supply a production attachment or qualify Slurm, Pyxis or a deployed worker.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from pathlib import Path
from queue import Empty
from typing import Any, NoReturn

import pytest

from hephaestus.automation import fleet_worker_cli
from hephaestus.automation.fleet_worker import FleetWorker
from hephaestus.automation.fleet_worker_runtime import FleetWorkerRuntime
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.io.utils import write_secure
from hephaestus.utils import subprocess_registry
from tests.unit.automation.test_fleet_build_jobs import PreparedCase, lock_is_held
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP
from tests.unit.automation.test_fleet_worker_runtime import writer_held
from tests.unit.automation.test_fleet_worker_runtime_cli import WorkerCLI
from tests.unit.automation.test_fleet_worker_runtime_registration import (
    ReceiptObservation,
    _capabilities,
    _close_unused_consumer,
    _session_command,
)

pytestmark = pytest.mark.precommit
_MODULE = "tests.unit.automation.test_fleet_worker_runtime_drain"


@dataclass
class DrainGates:
    """Hold only actual HTTP, receipt and callback operations."""

    http_entered: threading.Event = field(default_factory=threading.Event)
    join_entered: threading.Event = field(default_factory=threading.Event)
    pending_entered: threading.Event = field(default_factory=threading.Event)
    pending_release: threading.Event = field(default_factory=threading.Event)
    callback_entered: threading.Event = field(default_factory=threading.Event)
    callback_release: threading.Event = field(default_factory=threading.Event)
    callback_exited: threading.Event = field(default_factory=threading.Event)
    pool_exited: threading.Event = field(default_factory=threading.Event)
    sdk_exited: threading.Event = field(default_factory=threading.Event)


@dataclass
class ActiveDrain:
    """Retain the observations for this one actual admitted build."""

    prepared: PreparedCase
    gates: DrainGates = field(default_factory=DrainGates)
    runtime: FleetWorkerRuntime | None = None
    handle: JobHandle | None = None
    helper: threading.Thread | None = None
    helper_errors: list[BaseException] = field(default_factory=list)
    retained: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[ReceiptObservation] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)

    @property
    def http(self) -> BuildConsumerHTTP:
        """Return this prepared case's actual HTTP owner."""
        http = self.prepared.build.http
        assert http is not None
        return http

    def observe_retained(self, phase: str, *, source: bool, evidence: bool) -> None:
        """Observe actual ownership while the main thread is closing the runtime."""
        runtime = self.runtime
        assert runtime is not None
        worker, journal, loop = runtime.worker, runtime.journal, runtime.loop
        assert worker is not None and journal is not None and loop is not None
        process = worker.provider.process
        assert process is not None
        journal.require_writable()
        observed = {
            "phase": phase,
            "closing": runtime._closing,
            "source": lock_is_held(self.prepared.source_lock),
            "evidence": lock_is_held(self.prepared.evidence_lock),
            "journal": writer_held(journal),
            "loop": loop.is_running() and not loop.is_closed(),
            "client": not runtime.client._client.is_closed,
            "provider": process.poll() is None,
            "shutdown": runtime._shutdown.is_set(),
        }
        self.retained.append(observed)
        assert observed == {
            "phase": phase,
            "closing": True,
            "source": source,
            "evidence": evidence,
            "journal": True,
            "loop": True,
            "client": True,
            "provider": True,
            "shutdown": False,
        }

    def release_in_order(self) -> None:
        """Release each real operation after observing the preceding close barrier."""
        try:
            assert self.gates.join_entered.wait(3), "close did not reach actual pool joining"
            self.observe_retained("http", source=True, evidence=False)
            self.http.release.set()
            assert self.gates.pending_entered.wait(10), "the real pending receipt was not written"
            self.observe_retained("pending", source=True, evidence=True)
            self.gates.pending_release.set()
            assert self.gates.callback_entered.wait(10), "the actual pool callback did not enter"
            self.observe_retained("callback", source=False, evidence=False)
            runtime = self.runtime
            assert runtime is not None and runtime.completions.qsize() == 1
            assert not self.gates.callback_exited.is_set()
        except BaseException as error:
            self.helper_errors.append(error)
        finally:
            self.release()

    def release(self) -> None:
        """Permit every owned gate to leave after success or failure."""
        self.http.release.set()
        self.gates.pending_release.set()
        self.gates.callback_release.set()

    def drive(self, worker: FleetWorker, *, check_health: Callable[[], None] | None = None) -> None:
        """Return from the CLI serve seam only while actual HTTP work is active."""
        runtime = self.runtime
        assert runtime is not None
        assert worker is runtime.worker and check_health == runtime.check_health
        admitted = worker.handle(_session_command(self.prepared, "start"))
        assert admitted["status"] == "completed", admitted
        parent = self.prepared.build.publisher.command["payload"]["parent"]
        session = worker.journal.snapshot()["sessions"][parent["sessionId"]]
        assert session["workspace"] == str(self.prepared.build.publisher.source)
        assert session["providerThreadId"] and session["admissionReserved"]
        assert worker.provider.request("fixture/last-request", {"method": "turn/start"}) == {}
        runtime.register_build(
            "drain-context", **_capabilities(self.prepared, self.prepared.source_lease)
        )
        self.http.mode = "stall"
        self.handle = runtime.submit_build(
            replace(self.prepared.job(), fleet_context_id="drain-context", timeout_s=30)
        )
        assert self.gates.http_entered.wait(3), "the admitted SDK request did not reach HTTP"
        assert len(self.http.requests) == 1 and self.http.requests[0]["method"] == "POST"
        assert lock_is_held(self.prepared.source_lock)
        assert runtime.completions.empty()
        assert not runtime._closing and not self.gates.join_entered.is_set()
        self.helper = threading.Thread(target=self.release_in_order, name="fixture-drain-release")
        self.helper.start()
        # The real CLI finally block now calls close on this same control thread.


def _install_http_and_receipt_gates(case: ActiveDrain, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate the real transport wait and persisted receipt without replacing their work."""
    actual_http_wait = case.http.release.wait

    def wait_http(_timeout: float | None = None) -> bool:
        """Acknowledge the actual HTTP handler wait before its bounded release."""
        case.gates.http_entered.set()
        return actual_http_wait(10)

    def write_receipt(path: Path, content: str, *args: Any, **kwargs: Any) -> None:
        """Write the actual receipt before holding its return under both leases."""
        value = json.loads(content)
        write_secure(path, content, *args, **kwargs)
        case.receipts.append(
            ReceiptObservation(
                path,
                value,
                lock_is_held(case.prepared.source_lock),
                lock_is_held(case.prepared.evidence_lock),
            )
        )
        if value.get("fleet_receipt_state") == "pending":
            case.order.append("pending")
            case.gates.pending_entered.set()
            if not case.gates.pending_release.wait(10):
                raise TimeoutError("the pending receipt gate was not released")

    def reject_local(*_args: Any, **_kwargs: Any) -> NoReturn:
        """Reject local heavy-tool fallback before it starts any process."""
        case.forbidden.append("local-build")
        raise AssertionError("the registered build entered local heavy-tool execution")

    def reject_global() -> NoReturn:
        """Record a forbidden global cleanup request without sending signals."""
        case.forbidden.append("global-termination")
        raise AssertionError("runtime close requested global process termination")

    monkeypatch.setattr(case.http.release, "wait", wait_http)
    monkeypatch.setattr(worker_pool, "write_secure", write_receipt)
    monkeypatch.setattr(worker_pool, "run_subprocess", reject_local)
    monkeypatch.setattr(subprocess_registry, "terminate_all", reject_global)


def _install_pool_observers(case: ActiveDrain, monkeypatch: pytest.MonkeyPatch) -> None:
    """Observe real sealing, executor joining and completion callback exit."""
    runtime = case.runtime
    assert runtime is not None
    pool = runtime.pool
    assert pool is not None
    actual_join = pool._executor.shutdown
    actual_wait = pool.wait_for_exit
    actual_done = pool._on_future_done

    def join(wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Acknowledge the executor join after the real pool seals submissions."""
        assert wait and not cancel_futures
        case.order.append("pool-join")
        case.gates.join_entered.set()
        actual_join(wait=wait, cancel_futures=cancel_futures)

    def wait_for_exit() -> None:
        """Acknowledge the real pool barrier only after it returns."""
        actual_wait()
        case.order.append("pool-exit")
        case.gates.pool_exited.set()

    def done(handle: JobHandle, future: Future[JobResult]) -> None:
        """Publish the actual completion and hold its callback until release."""
        try:
            actual_done(handle, future)
            case.gates.callback_entered.set()
            if not case.gates.callback_release.wait(10):
                raise TimeoutError("the completion callback gate was not released")
        finally:
            case.order.append("callback-exit")
            case.gates.callback_exited.set()

    def reject_shutdown(*_args: Any, **_kwargs: Any) -> NoReturn:
        """Record a forbidden legacy shutdown before it can cancel work."""
        case.forbidden.append("legacy-shutdown")
        raise AssertionError("runtime close called the legacy pool shutdown")

    monkeypatch.setattr(pool._executor, "shutdown", join)
    monkeypatch.setattr(pool, "wait_for_exit", wait_for_exit)
    monkeypatch.setattr(pool, "_on_future_done", done)
    monkeypatch.setattr(pool, "shutdown", reject_shutdown)


def _install_resource_observers(case: ActiveDrain, monkeypatch: pytest.MonkeyPatch) -> None:
    """Observe real resource closure after all pool work and callbacks have exited."""
    runtime = case.runtime
    assert runtime is not None
    worker, journal, loop, client = runtime.worker, runtime.journal, runtime.loop, runtime.client
    assert worker is not None and journal is not None and loop is not None
    process = worker.provider.process
    assert process is not None
    actual_sdk_close = client.aclose
    actual_provider_close = worker.provider.close
    actual_journal_close = journal.close

    async def close_sdk() -> None:
        """Close the actual SDK on its loop after the real pool barrier."""
        case.observe_retained("sdk-close", source=False, evidence=False)
        assert asyncio.get_running_loop() is loop
        assert case.gates.callback_exited.is_set() and case.gates.pool_exited.is_set()
        assert runtime.completions.qsize() == 1
        await actual_sdk_close()
        case.order.append("sdk-close")
        case.gates.sdk_exited.set()

    def close_provider() -> bool:
        """Close the actual provider after SDK closure while retaining its journal."""
        assert case.gates.sdk_exited.is_set() and client._client.is_closed
        assert loop.is_closed() and writer_held(journal)
        result = actual_provider_close()
        assert result and process.poll() is not None
        case.order.append("provider-close")
        return result

    def close_journal() -> None:
        """Release the actual journal after provider and loop exit."""
        assert case.order[-1] == "provider-close"
        assert process.poll() is not None and loop.is_closed()
        actual_journal_close()
        case.order.append("journal-close")

    monkeypatch.setattr(client, "aclose", close_sdk)
    monkeypatch.setattr(worker.provider, "close", close_provider)
    monkeypatch.setattr(journal, "close", close_journal)


def _assert_drained(case: ActiveDrain) -> None:
    """Check actual collected bytes, completed receipts and final resource ownership."""
    runtime = case.runtime
    assert runtime is not None
    worker, journal, loop = runtime.worker, runtime.journal, runtime.loop
    assert worker is not None and journal is not None and loop is not None
    assert case.helper_errors == [] and case.forbidden == []
    assert [row["phase"] for row in case.retained] == [
        "http",
        "pending",
        "callback",
        "sdk-close",
    ]
    assert case.order == [
        "pool-join",
        "pending",
        "callback-exit",
        "pool-exit",
        "sdk-close",
        "provider-close",
        "journal-close",
    ]
    completed, result = runtime.take_completion(timeout=1)
    assert completed is case.handle and result.ok and not result.interrupted
    assert result.fleet_receipt is not None
    assert isinstance(result.value, dict)
    assert result.value == case.prepared.collect(deadline=time.monotonic() + 5)
    assert result.value["status"] == "verified_current"
    with pytest.raises(Empty):
        runtime.take_completion(timeout=0)
    pending = [row for row in case.receipts if row.value.get("fleet_receipt_state") == "pending"]
    finals = [row for row in case.receipts if row.value.get("fleet_receipt_state") == "finalized"]
    assert len(pending) == len(finals) == 1
    assert pending[0].source_held and pending[0].evidence_held
    assert not finals[0].source_held and not finals[0].evidence_held
    assert finals[0].value["ok"] and finals[0].value["succeeded"]
    assert json.loads(finals[0].path.read_text()) == finals[0].value
    assert sum(row["method"] == "POST" for row in case.http.requests) == 1
    assert not any(row["path"].endswith("/cancel") for row in case.http.requests)
    scheduler = case.prepared.build.publisher.scheduler
    assert len(scheduler.starts) == 1
    assert all(child.poll() == 0 for child in scheduler.children.values())
    assert not lock_is_held(case.prepared.source_lock)
    assert not lock_is_held(case.prepared.evidence_lock)
    assert loop.is_closed() and runtime.client._client.is_closed
    state = journal.snapshot()
    assert state["closed"] and not writer_held(journal)
    assert state["runtime_pid"] is None and state["runtime_uncertain"] is False
    assert worker.provider.process is None
    assert not runtime._shutdown.is_set()


def _run_cli(case: ActiveDrain, cli: WorkerCLI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the real CLI selection and final close with one admitted live build."""
    actual_start = FleetWorkerRuntime.start

    def start(runtime: FleetWorkerRuntime) -> None:
        """Start the concrete runtime before attaching observers to its resources."""
        actual_start(runtime)
        worker = runtime.worker
        assert worker is not None
        # The existing protocol fixture performs no model or container execution.
        monkeypatch.setattr(worker, "execution_guard", lambda: None)
        case.runtime = runtime
        _install_pool_observers(case, monkeypatch)
        _install_resource_observers(case, monkeypatch)
        print(json.dumps({"fleetDrainFixture": "runtime-started"}), flush=True)

    monkeypatch.setattr(FleetWorkerRuntime, "start", start)
    monkeypatch.setattr(fleet_worker_cli, "serve", case.drive)
    parent = case.prepared.build.publisher.command["payload"]["parent"]
    code = fleet_worker_cli.main(
        [
            "serve",
            "--state-dir",
            str(cli.state),
            "--workspace-root",
            str(cli.workspaces),
            "--codex-home",
            str(cli.codex),
            "--worker-id",
            parent["claim"]["workerId"],
            "--pool-id",
            "local",
            "--host-id",
            "fixture-host",
            "--capacity",
            "1",
            "--generation",
            str(parent["generation"]),
            "--codex-bin",
            str(cli.launcher),
            "--controller-port",
            str(case.http.port),
            "--controller-timeout",
            "5",
        ]
    )
    assert code == 0


def _drain_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep source, publication and the exact provider within this owned child."""
    build = Path(__file__).resolve().parents[3] / "build"
    build.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fleet-drain-", dir=build) as temporary:
        cli = WorkerCLI(Path(temporary).resolve(strict=True))
        source_root = cli.workspaces / "admitted"
        source_root.mkdir(mode=0o700)
        prepared = PreparedCase(source_root)
        case = ActiveDrain(prepared)
        try:
            _close_unused_consumer(prepared)
            cli.parent_record.write_text(json.dumps(os.getpid()))
            monkeypatch.setenv("AGAMEMNON_API_KEY", "fixture-private-key")
            _install_http_and_receipt_gates(case, monkeypatch)
            _run_cli(case, cli, monkeypatch)
            if case.helper is not None:
                case.helper.join(3)
                assert not case.helper.is_alive(), "the drain release helper did not exit"
            _assert_drained(case)
        finally:
            case.release()
            try:
                if case.helper is not None:
                    case.helper.join(3)
                    assert not case.helper.is_alive(), "the drain release helper did not exit"
            finally:
                prepared.close()


def test_cli_close_drains_actual_http_evidence_and_callback_before_resource_release() -> None:
    """Bound a faulty control-thread close to this exact fixture-owning process."""
    child = subprocess.Popen(
        [sys.executable, "-B", "-u", "-m", _MODULE, "--drain-case"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = forced = False
    stdout = stderr = ""
    try:
        try:
            stdout, stderr = child.communicate(timeout=75)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        if child.poll() is None:
            forced = True
            child.terminate()
            try:
                stdout, stderr = child.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate(timeout=3)
    events = [
        json.loads(line)["fleetDrainFixture"]
        for line in stdout.splitlines()
        if line.startswith('{"fleetDrainFixture":')
    ]
    diagnostic = {"exit": child.returncode, "events": events, "stderr": stderr}
    assert not timed_out and not forced, diagnostic
    assert child.returncode == 0, diagnostic
    assert events == ["runtime-started", "completed"], diagnostic


if __name__ == "__main__":
    if sys.argv[1:] != ["--drain-case"]:
        raise SystemExit("this module requires its exact fixture-owning child invocation")
    with pytest.MonkeyPatch.context() as fixture_patches:
        _drain_case(fixture_patches)
    print(json.dumps({"fleetDrainFixture": "completed"}), flush=True)
