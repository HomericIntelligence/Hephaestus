"""Check shared journal callers through their existing public operations."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from agamemnon_client import AgamemnonClient, AgamemnonConfig

from hephaestus.automation.fleet_build_service import FleetBuildOwner, FleetBuildService
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_worker import FleetWorker
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP
from tests.unit.automation.test_fleet_journal import FaultFile
from tests.unit.automation.test_fleet_worker import command, start, wait_state, worker as worker

pytestmark = pytest.mark.precommit


# Public test-only authentication value for the local fixture server.
FIXTURE_AUTH_VALUE = 'fixture-private-key'


def test_inventory_does_not_let_a_caller_release_a_retained_workspace(
    worker: FleetWorker,
) -> None:
    """An inventory result cannot release another session's workspace claim."""
    assert start(worker)["status"] == "completed"
    observed = worker.inventory()
    expected = copy.deepcopy(observed)
    path = worker.journal.directory / "receipts.jsonl"
    before = path.read_bytes()

    observed["sessions"][0]["released"] = True
    observed["sessions"][0]["issueRefs"].append("caller-owned-copy")
    retained = worker.inventory()
    after_inspection = path.read_bytes()
    competing = start(worker, target="session-2", number=2, workspace="one")

    assert after_inspection == before
    assert competing["status"] == "failed", "an inventory alias released the retained workspace"
    assert competing["receipt"]["error"] == "workspace_owned"
    assert retained == expected


def test_event_cursor_results_do_not_change_retained_event_identity(worker: FleetWorker) -> None:
    """Event result copies cannot change later event identities or cursors."""
    assert start(worker)["status"] == "completed"
    observed = worker.events(0)
    expected = copy.deepcopy(observed)
    path = worker.journal.directory / "receipts.jsonl"
    before = path.read_bytes()

    observed["events"][-1]["seq"] = 999
    observed["events"][-1]["eventId"] = "caller-owned-copy"
    observed["events"][-1]["event"]["issueRefs"].append("caller-owned-copy")
    retained = worker.events(0)

    assert path.read_bytes() == before
    assert retained == expected, "an event alias changed the retained event or cursor"


@pytest.mark.parametrize("stage", ["write", "short-write", "flush", "fsync", "apply"])
@pytest.mark.parametrize("operation", ["submit", "status", "cancel"])
def test_saved_build_owner_stops_after_another_writer_becomes_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    stage: str,
    operation: str,
) -> None:
    """A saved owner must check shared writer health before every new request."""
    journal = WorkerJournal(tmp_path / "journal")
    request.addfinalizer(journal.close)
    http = BuildConsumerHTTP()
    request.addfinalizer(http.close)

    async def exercise() -> None:
        async with AgamemnonClient(
            AgamemnonConfig(
                host="127.0.0.1", port=http.port, api_key=FIXTURE_AUTH_VALUE, timeout=1
            ),
            trust_env=False,
        ) as client:
            owner = FleetBuildOwner(
                FleetBuildService(client, http.data["submission"]),
                journal,
                "fixture-context",
                cancellation_ids=lambda: ("build-stop-1", "build-stop-key-1"),
            )
            admitted = await owner.submit(deadline=time.monotonic() + 1)
            assert admitted == http.data["admission"]["record"]
            assert len(http.requests) == 1
            real_file = journal._file
            real_fsync = os.fsync
            real_apply = journal._apply

            def uncertain_fsync(descriptor: int) -> None:
                real_fsync(descriptor)
                raise OSError("injected fsync uncertainty")

            def uncertain_apply(record: dict[str, Any]) -> None:
                real_apply(record)
                raise OSError("injected apply uncertainty")

            # This synchronous borrower bypasses the build owners' async lock.
            with monkeypatch.context() as fault:
                fault.setattr(journal, "_file", FaultFile(real_file, stage))
                if stage == "fsync":
                    fault.setattr(os, "fsync", uncertain_fsync)
                if stage == "apply":
                    fault.setattr(journal, "_apply", uncertain_apply)
                with ThreadPoolExecutor(max_workers=1) as writer:
                    effect = writer.submit(journal.append, "runtime", {"pid": os.getpid()})
                    with pytest.raises(OSError):
                        effect.result(timeout=2)

            real_file.flush()
            before = (journal.directory / "receipts.jsonl").read_bytes()
            snapshot = journal.snapshot()
            assert snapshot["write_uncertain"] is True
            with pytest.raises(RuntimeError, match="writer"):
                WorkerJournal(journal.directory)

            rejected: RuntimeError | None = None
            try:
                await getattr(owner, operation)(deadline=time.monotonic() + 1)
            except RuntimeError as error:
                rejected = error

            assert len(http.requests) == 1, (
                "the saved owner sent HTTP after shared write uncertainty"
            )
            assert rejected is not None, "the saved owner did not report its shared writer failure"
            assert (journal.directory / "receipts.jsonl").read_bytes() == before
            assert journal.snapshot() == snapshot

    asyncio.run(exercise())


def test_failed_session_append_does_not_change_retained_session_memory(worker: FleetWorker) -> None:
    """A rejected session write must not change its retained cleanup evidence."""
    assert start(worker)["status"] == "completed"
    assert (
        worker.handle(command("input", number=2, payload={"text": "finish"}))["status"]
        == "completed"
    )
    finished = wait_state(worker, "session-1", "idle")
    assert finished["backgroundCleanup"] == "confirmed_empty"
    expected = copy.deepcopy(finished)
    provider_before = worker.provider.request("fixture/last-request", {"method": "turn/start"})
    path = worker.journal.directory / "receipts.jsonl"
    before = path.read_bytes()
    old_limit = worker.journal.max_bytes
    # The small intent fits; the retained session is larger than this remaining budget.
    worker.journal.max_bytes = len(before) + 256
    try:
        with pytest.raises(RuntimeError, match="full"):
            worker.handle(command("input", number=3, payload={"text": "tool"}))
    finally:
        worker.journal.max_bytes = old_limit

    after = path.read_bytes()
    assert after.startswith(before)
    added = [json.loads(line) for line in after[len(before) :].splitlines()]
    assert len(added) == 1, "the fixture budget must allow exactly the new intent"
    assert added[0]["kind"] == "intent"
    assert added[0]["value"]["commandId"] == "cmd-3"
    assert (
        worker.provider.request("fixture/last-request", {"method": "turn/start"}) == provider_before
    )
    assert worker.journal.snapshot()["write_uncertain"] is False
    assert worker.journal.snapshot()["sessions"]["session-1"] == expected
