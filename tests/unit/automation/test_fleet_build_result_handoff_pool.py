"""Keep the actual supervisor handoff through a pool's pending receipt write.

Apply as tests/unit/automation/test_fleet_build_result_handoff_pool.py.
Admission and scheduler identities are controlled fixtures. SDK requests, the
harmless recipe, source snapshots, journals, publication and collection are real.
The explicit source lock is a fixture capability. This does not qualify source
isolation, a deployed worker, Slurm, Pyxis, or either cluster.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from hephaestus.automation.fleet_build_contract import start_command, validate_command
from hephaestus.automation.fleet_build_jobs import FleetBuildJobContext, FleetBuildJobRunner
from hephaestus.automation.fleet_build_receipts import read_fleet_build_receipt
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.jobs import JobHandle, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.io.utils import write_secure
from tests.unit.automation.test_fleet_build_jobs import PreparedCase, lock_is_held
from tests.unit.automation.test_fleet_build_result_handoff import (
    _acknowledged_state,
    _expected_from_state,
    _root_is_held,
)

pytestmark = pytest.mark.precommit
type Json = dict[str, Any]


@dataclass(frozen=True)
class ReceiptWrite:
    """Retain actual receipt bytes and exclusion at the completed write boundary."""

    path: Path
    payload: Json
    source_held: bool
    root_held: bool


@dataclass
class PendingReceiptGate:
    """Pause after the real pending write until the test releases this exact job."""

    case: PreparedCase
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    writes: list[ReceiptWrite] = field(default_factory=list)

    def write(self, path: str | Path, content: str, permissions: int = 0o600) -> None:
        """Delegate the real write before observing bytes and source/root exclusion."""
        write_secure(path, content, permissions=permissions)
        actual_path = Path(path)
        payload = json.loads(actual_path.read_text())
        assert payload == json.loads(content)
        self.writes.append(
            ReceiptWrite(
                actual_path,
                payload,
                lock_is_held(self.case.source_lock),
                _root_is_held(self.case.build.publisher.evidence_root),
            )
        )
        if payload.get("fleet_receipt_state") == "pending":
            self.entered.set()
            if not self.release.wait(4):
                raise RuntimeError("fixture pending receipt release did not arrive")


class MutationProbe:
    """Call actual supervisor mutations without blocking the test control thread."""

    def __init__(self, case: PreparedCase) -> None:
        """Prepare a validated cancel and one exact thread for both public calls."""
        self.service = case.build.publisher.service
        cancel = copy.deepcopy(case.build.publisher.command)
        cancel.update(
            operation="cancel", commandId="publisher-stop", idempotencyKey="publisher-stop"
        )
        cancel["payload"]["stopStartCommandId"] = case.build.publisher.command["commandId"]
        self.cancel = validate_command(cancel, case.build.publisher.command["payload"]["policy"])
        assert start_command(self.cancel) == case.build.publisher.command
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.results: list[tuple[str, Any, BaseException | None]] = []
        self.thread = threading.Thread(target=self.run, name="fixture-pinned-owner-mutation")

    def run(self) -> None:
        """Record the actual refusal or return from cancel and close."""
        self.entered.set()
        try:
            for name, operation in (
                ("cancel", lambda: self.service.handle(self.cancel)),
                ("close", self.service.close),
            ):
                try:
                    value = operation()
                except BaseException as error:
                    self.results.append((name, None, error))
                else:
                    self.results.append((name, value, None))
        finally:
            self.finished.set()

    def join(self) -> None:
        """Join this exact probe after the pending receipt gate is released."""
        if self.thread.ident is not None:
            self.thread.join(5)
            assert not self.thread.is_alive(), "the actual supervisor mutation did not exit"


@dataclass
class PoolCase:
    """Keep one pool, queue, callback and actual publisher lifetime together."""

    case: PreparedCase
    gate: PendingReceiptGate
    shutdown: threading.Event
    completions: CompletionQueue
    pool: WorkerPool
    probe: MutationProbe
    callbacks: list[tuple[JobHandle, bool, bool]] = field(default_factory=list)


def _actual_runner(case: PreparedCase) -> FleetBuildJobRunner:
    """Bind the actual supervisor method directly, without a fixture result handoff."""
    command = case.build.publisher.command
    context = FleetBuildJobContext(
        owner=case.build.owner,
        repository=command["payload"]["policy"]["workspace"]["repository"],
        source=case.build.publisher.source,
        submission=copy.deepcopy(case.build.submission),
        parent=copy.deepcopy(command["payload"]["parent"]),
        snapshot=case.build.publisher.snapshot,
        source_lease=case.source_lease,
        result_handoff=case.build.publisher.service.result_handoff,
    )
    return FleetBuildJobRunner(loop=case.build.loop, contexts={"job-context": context})


def _observe_callback(monkeypatch: pytest.MonkeyPatch, fixture: PoolCase) -> None:
    """Delegate the actual completion callback before recording released leases."""
    real_callback = fixture.pool._on_future_done

    def callback(handle: JobHandle, future: Future[JobResult]) -> None:
        """Publish through the real callback and retain its exact handle identity."""
        real_callback(handle, future)
        fixture.callbacks.append(
            (
                handle,
                lock_is_held(fixture.case.source_lock),
                _root_is_held(fixture.case.build.publisher.evidence_root),
            )
        )

    monkeypatch.setattr(fixture.pool, "_on_future_done", callback)


@contextmanager
def _pool_case(root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PoolCase]:
    """Keep borrowed owners until accepted pool work and callbacks have exited."""
    case = PreparedCase(root)
    gate = PendingReceiptGate(case)
    pool: WorkerPool | None = None
    probe: MutationProbe | None = None

    def no_local_execution(*args: Any, **kwargs: Any) -> None:
        """Refuse an unexpected local heavy-tool fallback."""
        raise AssertionError("selected Fleet work reached the local process boundary")

    try:
        monkeypatch.setattr(worker_pool, "run_subprocess", no_local_execution)
        monkeypatch.setattr(worker_pool, "write_secure", gate.write)
        shutdown = threading.Event()
        completions = CompletionQueue(maxsize=1)
        pool = WorkerPool(
            size=1,
            shutdown=shutdown,
            completion_q=completions,
            fleet_build_runner=_actual_runner(case),
            lock_dir=case.build.root / "pipeline-locks",
            evidence_receipt_dir=case.build.root / "pipeline-receipts",
        )
        probe = MutationProbe(case)
        fixture = PoolCase(case, gate, shutdown, completions, pool, probe)
        _observe_callback(monkeypatch, fixture)
        yield fixture
    finally:
        gate.release.set()
        try:
            if probe is not None:
                probe.join()
        finally:
            if pool is not None:
                pool.wait_for_exit()
            case.close()


def _observe_pending(fixture: PoolCase) -> Json:
    """Require actual pending bytes and prompt refusal while both leases remain held."""
    case, gate, probe = fixture.case, fixture.gate, fixture.probe
    assert gate.entered.wait(10), "the actual pool did not persist its pending receipt"
    assert case.published.wait(2) and case.publisher_error is None
    state = _acknowledged_state(case.build.publisher)
    assert len(gate.writes) == 1
    pending = gate.writes[0]
    assert pending.payload["fleet_receipt_state"] == "pending"
    assert pending.payload["ok"] is False and pending.payload["succeeded"] is False
    assert pending.source_held and pending.root_held
    assert lock_is_held(case.source_lock)
    assert _root_is_held(case.build.publisher.evidence_root)
    assert case.evidence_deadlines == []
    assert not lock_is_held(case.evidence_lock)
    assert fixture.callbacks == []
    with pytest.raises(Empty):
        fixture.completions.get_nowait()
    probe.thread.start()
    assert probe.entered.wait(1), "the actual mutation probe did not enter"
    assert probe.finished.wait(1), "supervisor mutation waited for result collection"
    assert [name for name, _value, _error in probe.results] == ["cancel", "close"]
    for name, value, error in probe.results:
        assert isinstance(error, RuntimeError) and "reader" in str(error), (name, value, error)
    assert case.build.publisher.state() == state
    assert not case.build.publisher.service.journal.snapshot()["closed"]
    return state


def _assert_completion(
    fixture: PoolCase, handle: JobHandle, state: Json, *, interrupt: bool
) -> None:
    """Read final bytes only after the real handoff and source lease have exited."""
    completed, result = fixture.completions.get(timeout=10)
    fixture.pool.wait_for_exit()
    assert completed is handle
    assert fixture.callbacks == [(handle, False, False)]
    assert not lock_is_held(fixture.case.source_lock)
    assert not _root_is_held(fixture.case.build.publisher.evidence_root)
    with pytest.raises(Empty):
        fixture.completions.get_nowait()
    reference, expected = _expected_from_state(state)
    assert isinstance(result.value, dict)
    assert result.value["reference"] == reference and result.value["identity"] == expected
    assert result.value["status"] == "verified_current" and result.value["sourceCurrent"] is True
    writes = fixture.gate.writes
    assert len({write.path for write in writes}) == 1
    assert all(not write.source_held and not write.root_held for write in writes[1:])
    final = json.loads(writes[0].path.read_text())
    assert final == writes[-1].payload
    assert fixture.case.build.publisher.state() == state
    if interrupt:
        assert result.ok is False and result.interrupted and result.error
        assert result.fleet_receipt is None
        assert all(write.payload["fleet_receipt_state"] != "finalized" for write in writes)
        assert final["fleet_receipt_state"] == "failed"
        assert final["ok"] is False and final["succeeded"] is False
        with pytest.raises((ValueError, RuntimeError)):
            read_fleet_build_receipt(result, expected=expected, deadline=time.monotonic() + 5)
    else:
        assert result.ok is True and not result.interrupted and result.error is None
        assert result.fleet_receipt is not None
        assert [write.payload["fleet_receipt_state"] for write in writes] == [
            "pending",
            "finalized",
        ]
        assert final["ok"] is True and final["succeeded"] is True
        assert (
            read_fleet_build_receipt(result, expected=expected, deadline=time.monotonic() + 5)
            == result.value
        )
    assert len(fixture.case.source_deadlines) == 1
    http = fixture.case.build.http
    assert http is not None
    assert sum(request["method"] == "POST" for request in http.requests) == 1
    assert not any(request["path"].endswith("/cancel") for request in http.requests)
    assert fixture.case.build.publisher.client.facts == [state["terminal"]]


@pytest.mark.parametrize("interrupt", [False, True], ids=["release", "shutdown-after-pending"])
def test_actual_supervisor_handoff_remains_owned_through_pool_receipt_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    """Require lease release before eligible completion, and refuse exit-time shutdown."""
    with _pool_case(tmp_path, monkeypatch) as fixture:
        handle = fixture.pool.submit(
            replace(fixture.case.job(), timeout_s=15),
            StageName.IMPLEMENTATION,
            claim_key="fixture#17",
        )
        state = _observe_pending(fixture)
        if interrupt:
            fixture.shutdown.set()
        fixture.gate.release.set()
        _assert_completion(fixture, handle, state, interrupt=interrupt)
