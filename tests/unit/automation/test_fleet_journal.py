"""Check shared journal storage, copies, and uncertainty through actual bytes."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from hephaestus.automation import fleet_journal as module

pytestmark = pytest.mark.precommit


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[module.WorkerJournal]:
    """Keep one actual private writer until the test releases it."""
    value = module.WorkerJournal(tmp_path / "journal")
    yield value
    value.close()


def command(number: int = 1) -> dict[str, Any]:
    """Return one fixed worker command with a distinct identity."""
    return {
        "schema": "hi/fleet/v1",
        "commandId": f"command-{number}",
        "idempotencyKey": f"key-{number}",
        "workerId": "worker-1",
        "generation": 1,
        "targetKind": "sessions",
        "targetId": "session-1",
        "operation": "input",
        "payload": {"text": "fixture input"},
    }


class FaultFile:
    """Apply the real file operation before an injected uncertain reply."""

    def __init__(self, wrapped: BinaryIO, stage: str) -> None:
        self.wrapped = wrapped
        self.stage = stage

    def __getattr__(self, name: str) -> Any:
        """Delegate unchanged operations to the actual file stream."""
        return getattr(self.wrapped, name)

    def write(self, data: bytes) -> int:
        """Retain real bytes before the write error or short count."""
        if self.stage == "short-write":
            return self.wrapped.write(data[: len(data) // 2])
        result = self.wrapped.write(data)
        if self.stage == "write":
            raise OSError("injected write uncertainty")
        return result

    def flush(self) -> None:
        """Flush the real stream before the injected reply error."""
        self.wrapped.flush()
        if self.stage == "flush":
            raise OSError("injected flush uncertainty")


@pytest.mark.parametrize("stage", ["write", "short-write", "flush", "fsync", "apply"])
def test_uncertain_append_fences_every_mutator(
    journal: module.WorkerJournal, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """An uncertain effect blocks new and cached admission without releasing the flock."""
    original = command()
    assert journal.begin(original) is None
    journal.complete(original, {"status": "completed", "receipt": {"value": "retained"}})
    real_file = journal._file
    real_fsync = os.fsync
    real_apply = journal._apply

    def uncertain_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        raise OSError("injected fsync uncertainty")

    def uncertain_apply(record: dict[str, Any]) -> None:
        real_apply(record)
        raise OSError("injected apply uncertainty")

    with monkeypatch.context() as fault:
        fault.setattr(journal, "_file", FaultFile(real_file, stage))
        if stage == "fsync":
            fault.setattr(os, "fsync", uncertain_fsync)
        if stage == "apply":
            fault.setattr(journal, "_apply", uncertain_apply)
        with pytest.raises((OSError, RuntimeError)):
            journal.append("probe", {"effect": "uncertain"})

    real_file.flush()
    before = (journal.directory / "receipts.jsonl").read_bytes()
    for operation in (
        lambda: journal.append("probe", {"effect": "forbidden"}),
        lambda: journal.begin(command(2)),
        lambda: journal.begin(original),
        lambda: journal.complete(original, {"status": "overwritten"}),
    ):
        with pytest.raises(RuntimeError, match="recovery"):
            operation()
        assert (journal.directory / "receipts.jsonl").read_bytes() == before
    with pytest.raises(RuntimeError, match="writer"):
        module.WorkerJournal(journal.directory)


@pytest.mark.parametrize("bad_value", [{"bad": object()}, {"bad": "x" * (1024 * 1024)}])
def test_rejected_input_before_write_does_not_poison(
    journal: module.WorkerJournal, bad_value: dict[str, Any]
) -> None:
    """A reversible input rejection cannot disable an otherwise usable writer."""
    path = journal.directory / "receipts.jsonl"
    before = path.read_bytes()
    with pytest.raises((TypeError, RuntimeError)):
        journal.append("probe", bad_value)
    assert path.read_bytes() == before
    journal.append("probe", {"accepted": True})
    assert json.loads(path.read_text().splitlines()[-1])["value"] == {"accepted": True}


def test_snapshot_is_deep_and_does_not_change_after_later_writes(
    journal: module.WorkerJournal,
) -> None:
    """Callers own snapshot copies while retained state and later snapshots stay intact."""
    journal.append("session", {"sessionId": "one", "nested": {"values": [1]}})
    before = journal.snapshot()
    before["records"][0]["value"]["nested"]["values"].append(2)
    before["sessions"]["one"]["nested"]["values"].append(3)
    assert journal.snapshot()["sessions"]["one"]["nested"]["values"] == [1]
    retained = journal.snapshot()
    journal.append("event", {"seq": 1})
    assert retained["events"] == []
    assert journal.snapshot()["events"] == [{"seq": 1}]


def test_transaction_allows_snapshot_and_append_without_self_contention(
    journal: module.WorkerJournal,
) -> None:
    """A short compound event update uses one reentrant storage transaction."""
    with journal.transaction():
        with journal.transaction():
            sequence = len(journal.snapshot()["events"]) + 1
            journal.append("event", {"seq": sequence})
    assert journal.snapshot()["events"] == [{"seq": 1}]


def test_concurrent_transaction_assigns_each_event_sequence_once(
    journal: module.WorkerJournal,
) -> None:
    """Two callers derive event identities from one protected retained state."""
    started = threading.Barrier(3)

    def append_event() -> None:
        started.wait(timeout=2)
        with journal.transaction():
            sequence = len(journal.snapshot()["events"]) + 1
            journal.append("event", {"seq": sequence})

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(append_event) for _ in range(2)]
        started.wait(timeout=2)
        for future in futures:
            future.result(timeout=2)
    path = journal.directory / "receipts.jsonl"
    assert [json.loads(line)["value"]["seq"] for line in path.read_text().splitlines()] == [1, 2]


def test_competing_begin_retains_only_one_intent(journal: module.WorkerJournal) -> None:
    """Only one caller can obtain an uncompleted command's first admission."""
    started = threading.Barrier(3)

    def begin() -> dict[str, Any] | None:
        started.wait(timeout=2)
        return journal.begin(command())

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(begin) for _ in range(2)]
        started.wait(timeout=2)
        results = [future.result(timeout=2) for future in futures]
    assert sum(result is None for result in results) == 1
    retained = next(result for result in results if result is not None)
    assert retained["receipt"]["error"] == "outcome_unknown"
    path = journal.directory / "receipts.jsonl"
    assert len(path.read_text().splitlines()) == 1


class GatedFile:
    """Pause the first real write so another public operation can contend."""

    def __init__(self, wrapped: BinaryIO) -> None:
        self.wrapped = wrapped
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._gate_lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        """Delegate unchanged operations to the actual file stream."""
        return getattr(self.wrapped, name)

    def write(self, data: bytes) -> int:
        """Release the first write only after the contender starts its call."""
        with self._gate_lock:
            first = self._first
            self._first = False
        if first:
            self.entered.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test write gate was not released")
        return self.wrapped.write(data)


@pytest.mark.parametrize("contender", ["append", "close"])
def test_active_write_excludes_another_append_or_close(
    journal: module.WorkerJournal, monkeypatch: pytest.MonkeyPatch, contender: str
) -> None:
    """A second storage operation cannot complete inside the first write."""
    gate = GatedFile(journal._file)
    monkeypatch.setattr(journal, "_file", gate)
    attempted = threading.Event()
    finished = threading.Event()

    def compete() -> None:
        attempted.set()
        try:
            if contender == "append":
                journal.append("probe", {"order": 2})
            else:
                journal.close()
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(journal.append, "probe", {"order": 1})
        second = None
        try:
            assert gate.entered.wait(timeout=2)
            second = workers.submit(compete)
            assert attempted.wait(timeout=2)
            premature = finished.wait(timeout=0.2)
        finally:
            gate.release.set()
        first_error = first.exception(timeout=2)
        assert second is not None
        second_error = second.exception(timeout=2)
    assert not premature, f"{contender} completed while the first write was active"
    assert first_error is None
    assert second_error is None
    actual = [
        json.loads(line)["value"]["order"]
        for line in (journal.directory / "receipts.jsonl").read_text().splitlines()
    ]
    assert actual == ([1, 2] if contender == "append" else [1])


def test_uncertain_writer_still_exposes_a_safe_recovery_snapshot(
    journal: module.WorkerJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only copies survive a write fence without granting another transaction."""
    journal.append("probe", {"confirmed": [1]})
    real_fsync = os.fsync

    def fail(descriptor: int) -> None:
        real_fsync(descriptor)
        raise OSError("injected fsync uncertainty")

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fail)
        with pytest.raises(OSError):
            journal.append("probe", {"unconfirmed": [2]})
    snapshot = journal.snapshot()
    assert snapshot["write_uncertain"] is True
    assert snapshot["closed"] is False
    assert snapshot["records"] == [{"kind": "probe", "value": {"confirmed": [1]}}]
    snapshot["records"][0]["value"]["confirmed"].append(3)
    assert journal.snapshot()["records"][0]["value"]["confirmed"] == [1]
    with pytest.raises(RuntimeError, match="recovery"):
        journal.require_writable()
    with pytest.raises(RuntimeError, match="recovery"), journal.transaction():
        pytest.fail("an uncertain writer entered a new transaction")


def test_close_is_idempotent_and_preserves_observations(journal: module.WorkerJournal) -> None:
    """A closed writer cannot admit work and its final copied state stays readable."""
    journal.append("event", {"seq": 1})
    journal.close()
    journal.close()
    assert journal.snapshot()["closed"] is True
    assert journal.snapshot()["events"] == [{"seq": 1}]
    with pytest.raises(RuntimeError, match="closed"):
        journal.begin(command())
    with pytest.raises(RuntimeError, match="closed"):
        journal.append("event", {"seq": 2})
    with pytest.raises(RuntimeError, match="closed"):
        journal.require_writable()


def test_health_check_does_not_create_an_admission(journal: module.WorkerJournal) -> None:
    """A healthy writer observation has no journal or execution effect."""
    before = (journal.directory / "receipts.jsonl").read_bytes()
    journal.require_writable()
    assert journal.snapshot()["write_uncertain"] is False
    assert (journal.directory / "receipts.jsonl").read_bytes() == before


@pytest.mark.parametrize("after_effect", [False, True])
def test_failed_stream_close_keeps_the_process_writer_lock(
    journal: module.WorkerJournal, monkeypatch: pytest.MonkeyPatch, after_effect: bool
) -> None:
    """An uncertain close needs explicit resource recovery before another owner opens."""
    real_close = journal._file.close

    def fail() -> None:
        if after_effect:
            real_close()
        raise OSError("injected close uncertainty")

    with monkeypatch.context() as fault:
        fault.setattr(journal._file, "close", fail)
        with pytest.raises(OSError):
            journal.close()
    assert journal.snapshot()["write_uncertain"] is True
    assert journal.snapshot()["closed"] is False
    with pytest.raises(RuntimeError, match="recovery"):
        journal.append("probe", {"forbidden": True})
    with pytest.raises(RuntimeError, match="writer"):
        module.WorkerJournal(journal.directory)
    journal.close()
    reopened = module.WorkerJournal(journal.directory)
    reopened.close()


def test_caller_exception_releases_transaction_without_rollback(
    journal: module.WorkerJournal,
) -> None:
    """A later caller error neither erases an acknowledged write nor poisons the journal."""
    with pytest.raises(ValueError, match="caller error"), journal.transaction():
        journal.append("probe", {"confirmed": 1})
        raise ValueError("caller error")

    def later_writer() -> None:
        with journal.transaction():
            journal.require_writable()
            journal.append("probe", {"confirmed": 2})

    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(later_writer).result(timeout=2)
    actual = [
        json.loads(line)["value"]
        for line in (journal.directory / "receipts.jsonl").read_text().splitlines()
    ]
    assert actual == [{"confirmed": 1}, {"confirmed": 2}]
    assert journal.snapshot()["write_uncertain"] is False
