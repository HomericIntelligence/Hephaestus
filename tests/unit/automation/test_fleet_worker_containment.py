"""Require container disposal before a cancelled worker releases its ownership."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from hephaestus.automation.fleet_attachment import AttachmentEndpoint
from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry
from tests.unit.automation.test_fleet_containment import specification, supervisor
from tests.unit.automation.test_fleet_worker import command, start, wait_state, worker as worker

pytestmark = pytest.mark.precommit


@pytest.fixture
def contained_worker(worker):
    """Use real journals and registry with only provider, engine, and kernel substitutes."""
    with tempfile.TemporaryDirectory(prefix="hf-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        owner = supervisor(root)
        spec = replace(
            specification(root),
            worker_id="worker-a",
            execution_id="session-1-exec",
            workspace=worker.workspace_root / "one",
        )
        lease = owner.create(spec)
        endpoint = AttachmentEndpoint(owner, lease["leaseId"])
        try:
            registry = EnvironmentRegistry(
                worker.codex_home,
                [EnvironmentLease.from_endpoint(endpoint, "session-1-env", Path(sys.executable))],
            )
            registry.write_configuration()
            worker.environment_registry = registry
            worker.containment_supervisor = owner
            yield worker, owner, lease
        finally:
            endpoint.close()
            owner.close()


def request_stop(worker, operation, active):
    """Send ordinary worker commands through the real provider protocol fixture."""
    assert start(worker)["status"] == "completed"
    if active:
        assert worker.handle(command("input", number=2, payload={"text": "tool"}))["status"] == (
            "completed"
        )
        wait_state(worker, "session-1", "tool_running")
    return worker.handle(command(operation, number=3))


@pytest.mark.parametrize("active", [False, True], ids=["idle", "active"])
@pytest.mark.parametrize("failure", ["remove", "kernel", "missing-supervisor"])
def test_cancel_keeps_ownership_until_container_disposal_is_confirmed(
    contained_worker, active, failure
):
    """An empty provider inventory does not prove container and descendant absence."""
    worker, owner, lease = contained_worker
    if failure == "remove":
        owner.engine.fail_remove = True
    elif failure == "kernel":
        owner.kernel.empty = False
    else:
        worker.containment_supervisor = None
    result = request_stop(worker, "cancel", active)
    if not active:
        assert result["status"] == "failed"
    session = wait_state(worker, "session-1", "unknown")
    assert session["released"] is False
    assert session["admissionReserved"] is True
    assert session["outcome"] is None
    assert session["waitingReason"] == "container_disposal_unconfirmed"
    assert owner.inspect(lease["leaseId"])["phase"] != "disposed"
    assert worker.inventory()["activeReservations"] == 1
    assert session.get("backgroundCleanup") != "confirmed_empty"
    for event in worker.events(0)["events"]:
        assert event["event"].get("backgroundCleanup") != "confirmed_empty"


@pytest.mark.parametrize("active", [False, True], ids=["idle", "active"])
def test_cancel_records_bound_disposal_before_releasing_the_session(contained_worker, active):
    """A confirmed cancellation retains a reference to its durable disposal receipt."""
    worker, owner, lease = contained_worker
    result = request_stop(worker, "cancel", active)
    assert result["status"] == ("accepted" if active else "completed")
    session = wait_state(worker, "session-1", "idle")
    disposed = owner.inspect(lease["leaseId"])
    assert disposed["phase"] == "disposed"
    assert session["released"] is True
    assert session["admissionReserved"] is False
    assert session["outcome"] == "cancelled"
    assert session["containmentDisposal"] == {
        "leaseId": lease["leaseId"],
        "digest": disposed["disposal"]["digest"],
    }
    assert owner.engine.calls.count("remove") == 1
    before = worker.events(0)["cursor"]
    worker.handle(command("cancel", number=3))
    assert owner.engine.calls.count("remove") == 1
    assert worker.events(before)["events"] == []


def test_interrupt_retains_container_and_reservation_without_a_release_fact(contained_worker):
    """An interrupted conversation retains its execution boundary for reconciliation."""
    worker, owner, lease = contained_worker
    assert request_stop(worker, "interrupt", True)["status"] == "accepted"
    session = wait_state(worker, "session-1", "unknown")
    assert session["outcome"] is None
    assert session["providerOutcome"] == "interrupted"
    assert session["waitingReason"] == "contained_interrupt_requires_reconciliation"
    assert session["released"] is False
    assert session["admissionReserved"] is True
    assert owner.inspect(lease["leaseId"])["phase"] == "created"
    assert "remove" not in owner.engine.calls
    assert worker.inventory()["activeReservations"] == 1
    assert session.get("backgroundCleanup") != "confirmed_empty"
    for event in worker.events(0)["events"]:
        assert event["event"].get("backgroundCleanup") != "confirmed_empty"
        assert "providerOutcome" not in event["event"]


def test_completed_turn_does_not_claim_container_cleanup(contained_worker):
    """A finished turn must not supply Agamemnon's cleanup marker for a retained lease."""
    worker, owner, lease = contained_worker
    assert start(worker)["status"] == "completed"
    worker.handle(command("input", number=2, payload={"text": "finish"}))
    session = wait_state(worker, "session-1", "idle")
    assert session["outcome"] == "completed"
    assert session["providerBackgroundCleanup"] == "confirmed_empty"
    assert session["backgroundCleanup"] != "confirmed_empty"
    assert owner.inspect(lease["leaseId"])["phase"] != "disposed"
    for event in worker.events(0)["events"]:
        assert event["event"].get("backgroundCleanup") != "confirmed_empty"
        assert "providerBackgroundCleanup" not in event["event"]


@pytest.mark.parametrize(
    "field,value",
    [("workerId", "other"), ("executionId", "other"), ("generation", 2)],
)
def test_cancel_does_not_remove_a_lease_with_changed_ownership(contained_worker, field, value):
    """Reject a changed immutable binding before any engine removal."""
    worker, owner, lease = contained_worker
    owner.leases[lease["leaseId"]]["spec"][field] = value
    result = request_stop(worker, "cancel", False)
    assert result["status"] == "failed"
    session = wait_state(worker, "session-1", "unknown")
    assert session["admissionReserved"] is True
    assert session["released"] is False
    assert "remove" not in owner.engine.calls


def test_cancel_rejects_a_damaged_retained_disposal_receipt(contained_worker):
    """A disposed phase alone cannot replace the bound causal receipt."""
    worker, owner, lease = contained_worker
    assert owner.dispose(lease["leaseId"])["phase"] == "disposed"
    owner.leases[lease["leaseId"]]["disposal"]["digest"] = "0" * 64
    result = request_stop(worker, "cancel", False)
    assert result["status"] == "failed"
    session = wait_state(worker, "session-1", "unknown")
    assert session["released"] is False
    assert session.get("containmentDisposal") is None
    assert owner.engine.calls.count("remove") == 1


def test_replayed_stop_observes_uncertain_disposal_without_repeating_removal(contained_worker):
    """Only a later confirmed absence can finish a retained cancellation."""
    worker, owner, lease = contained_worker
    owner.kernel.empty = False
    assert request_stop(worker, "cancel", True)["status"] == "accepted"
    session = wait_state(worker, "session-1", "unknown")
    notification = {
        "method": "turn/completed",
        "params": {
            "threadId": session["providerThreadId"],
            "turn": {"id": session["providerTurnId"], "status": "interrupted"},
        },
    }
    worker.provider.request("fixture/message", {"message": notification})
    worker.poll()
    assert owner.engine.calls.count("remove") == 1
    assert owner.inspect(lease["leaseId"])["phase"] == "uncertain"
    assert worker.inventory()["activeReservations"] == 1
    owner.kernel.empty = True
    worker.provider.request("fixture/message", {"message": notification})
    session = wait_state(worker, "session-1", "idle")
    assert session["outcome"] == "cancelled"
    assert session["released"] is True
    assert owner.engine.calls.count("remove") == 1


def test_new_contained_turn_invalidates_provider_cleanup_observation(contained_worker):
    """Previous provider cleanup does not describe a new active turn."""
    worker, _owner, _lease = contained_worker
    assert start(worker)["status"] == "completed"
    worker.handle(command("input", number=2, payload={"text": "finish"}))
    wait_state(worker, "session-1", "idle")
    worker.handle(command("input", number=3, payload={"text": "tool"}))
    session = wait_state(worker, "session-1", "tool_running")
    assert session.get("backgroundCleanup") is None
    assert session.get("providerBackgroundCleanup") is None
