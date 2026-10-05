"""Check worker journal borrowing with a real local protocol fixture.

Apply as tests/unit/automation/test_fleet_worker_borrowed_journal.py.
The standalone case needs no new parameter and provides an independent control.
An unsupported journal keyword is a new-feature RED, not lifecycle evidence.
The fixture performs no model or tool work.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_provider import ProviderError
from hephaestus.automation.fleet_worker import FleetWorker
from tests.unit.automation.test_fleet_worker import FIXTURE

pytestmark = pytest.mark.precommit


def _options(root: Path) -> dict[str, Any]:
    """Use private roots and the existing deterministic protocol subprocess."""
    workspace = root / "workspaces"
    workspace.mkdir(mode=0o700)
    (workspace / "one").mkdir(mode=0o700)
    codex_home = root / "codex"
    codex_home.mkdir(mode=0o700)
    return {
        "state_dir": root / "state",
        "workspace_root": workspace,
        "codex_home": codex_home,
        "worker_id": "worker-a",
        "pool_id": "local",
        "host_id": "laptop",
        "generation": 1,
        "capacity": 2,
        "provider_command": [sys.executable, "-u", str(FIXTURE)],
    }


def _configure_fixture(worker: FleetWorker) -> None:
    """Keep the existing protocol fixture separate from live execution qualification."""
    worker.storage_guard = lambda: None
    worker.execution_guard = lambda: None


def _require_writer_exclusion(journal: WorkerJournal) -> None:
    """Use a separate real writer attempt and close it if exclusion fails."""
    try:
        contender = WorkerJournal(journal.directory)
    except RuntimeError as error:
        assert "writer" in str(error)
    else:
        contender.close()
        pytest.fail("the borrowed journal released its process writer lock")


@pytest.mark.parametrize("borrowed", [False, True], ids=["standalone-control", "borrowed"])
def test_worker_close_preserves_the_selected_journal_owner(tmp_path: Path, borrowed: bool) -> None:
    """Only the journal owner releases the writer after real provider cleanup."""
    options = _options(tmp_path)
    supplied = WorkerJournal(options["state_dir"]) if borrowed else None
    worker: FleetWorker | None = None
    try:
        worker = FleetWorker(**options, journal=supplied) if borrowed else FleetWorker(**options)
        journal = worker.journal
        if borrowed:
            assert journal is supplied
        assert worker.provider.process is None
        _configure_fixture(worker)
        worker.start()
        process = worker.provider.process
        assert process is not None and process.poll() is None
        assert journal.snapshot()["runtime_pid"] == process.pid

        worker.close()
        assert process.poll() is not None, "worker close did not reap the actual fixture provider"
        after = (journal.directory / "receipts.jsonl").read_bytes()
        worker.close()
        assert (journal.directory / "receipts.jsonl").read_bytes() == after
        assert journal.snapshot()["runtime_pid"] is None
        assert journal.snapshot()["runtime_uncertain"] is False
        assert journal.snapshot()["closed"] is (not borrowed)
        if borrowed:
            _require_writer_exclusion(journal)
            journal.require_writable()
            journal.append("runtime-owner-observation", {"workerExited": True})
            assert json.loads(
                (journal.directory / "receipts.jsonl").read_text().splitlines()[-1]
            ) == {
                "kind": "runtime-owner-observation",
                "value": {"workerExited": True},
            }
            journal.close()
        reopened = WorkerJournal(journal.directory)
        try:
            assert reopened.snapshot()["runtime_pid"] is None
            assert reopened.snapshot()["runtime_uncertain"] is False
        finally:
            reopened.close()
    finally:
        try:
            if worker is not None:
                worker.close()
        finally:
            if supplied is not None:
                supplied.close()


@pytest.mark.parametrize(
    "invalid",
    ["standalone-identity", "borrowed-identity", "borrowed-state-path"],
)
def test_constructor_rejects_invalid_inputs_before_it_takes_journal_ownership(
    tmp_path: Path, invalid: str
) -> None:
    """Rejected identity or path cannot create a writer or close a supplied one."""
    options = _options(tmp_path)
    supplied = None if invalid == "standalone-identity" else WorkerJournal(options["state_dir"])
    if invalid.endswith("identity"):
        options["worker_id"] = ""
    else:
        options["state_dir"] = tmp_path / "different-state"
    before = supplied.snapshot() if supplied is not None else None
    before_bytes = (
        (supplied.directory / "receipts.jsonl").read_bytes() if supplied is not None else None
    )
    worker: FleetWorker | None = None
    try:
        with pytest.raises(ValueError):
            worker = (
                FleetWorker(**options, journal=supplied)
                if supplied is not None
                else FleetWorker(**options)
            )
        if invalid != "borrowed-identity":
            assert not options["state_dir"].exists(), (
                "constructor created journal state before it rejected the invalid input"
            )
        if supplied is not None:
            assert supplied.snapshot() == before
            assert (supplied.directory / "receipts.jsonl").read_bytes() == before_bytes
            supplied.require_writable()
            _require_writer_exclusion(supplied)
            supplied.append("runtime-owner-observation", {"rejectedWorker": True})
    finally:
        try:
            if worker is not None:
                worker.close()
        finally:
            if supplied is not None:
                supplied.close()


@pytest.mark.parametrize("fault", ["provider-close-reply", "journal-uncertain"])
def test_borrowed_worker_close_retains_only_confirmed_journal_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """A missing close reply or failed persistence cannot invent a cleanup fact."""
    options = _options(tmp_path)
    journal = WorkerJournal(options["state_dir"])
    worker: FleetWorker | None = None
    try:
        worker = FleetWorker(**options, journal=journal)
        _configure_fixture(worker)
        worker.start()
        process = worker.provider.process
        assert process is not None and process.poll() is None
        runtime_before = [row for row in journal.snapshot()["records"] if row["kind"] == "runtime"]
        assert runtime_before[-1]["value"]["pid"] == process.pid
        real_close = worker.provider.close
        if fault == "provider-close-reply":

            def lose_reply() -> bool:
                assert real_close() is True
                raise ProviderError("fixture lost close confirmation after actual cleanup")

            with monkeypatch.context() as injection:
                injection.setattr(worker.provider, "close", lose_reply)
                with pytest.raises(ProviderError, match="lost close confirmation"):
                    worker.close()
            state = journal.snapshot()
            assert state["runtime_pid"] is None
            assert state["runtime_uncertain"] is True
            assert state["write_uncertain"] is False
            assert json.loads(
                (journal.directory / "receipts.jsonl").read_text().splitlines()[-1]
            ) == {"kind": "runtime", "value": {"pid": None, "uncertain": True}}
            journal.require_writable()
        else:
            real_fsync = os.fsync

            def lose_sync_reply(descriptor: int) -> None:
                real_fsync(descriptor)
                raise OSError("fixture lost journal synchronization confirmation")

            with monkeypatch.context() as injection:
                injection.setattr(os, "fsync", lose_sync_reply)
                with pytest.raises(OSError, match="lost journal synchronization"):
                    journal.append("runtime-owner-observation", {"writeUncertain": True})
            before_close = (journal.directory / "receipts.jsonl").read_bytes()
            with pytest.raises(RuntimeError, match="recovery"):
                worker.close()
            assert (journal.directory / "receipts.jsonl").read_bytes() == before_close
            state = journal.snapshot()
            assert state["write_uncertain"] is True
            assert [row for row in state["records"] if row["kind"] == "runtime"] == runtime_before
            with pytest.raises(RuntimeError, match="recovery"):
                journal.require_writable()

        assert process.poll() is not None, "worker close did not reap the actual fixture provider"
        assert journal.snapshot()["closed"] is False
        _require_writer_exclusion(journal)
        after_close = (journal.directory / "receipts.jsonl").read_bytes()
        worker.close()
        assert (journal.directory / "receipts.jsonl").read_bytes() == after_close
        assert journal.snapshot() == state
    finally:
        try:
            if worker is not None:
                # The real provider close is idempotent and targets only its owned fixture group.
                worker.provider.close()
        finally:
            journal.close()
