"""Check private job results with synthetic provider and containment boundaries."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hephaestus.automation.fleet_worker_cli import _dispatch
from tests.unit.automation.test_fleet_worker import command, start, worker as worker
from tests.unit.automation.test_fleet_worker_containment import contained_worker as contained_worker

pytestmark = pytest.mark.precommit


def association():
    """Bind the next admitted input without changing its public command."""
    return {
        "operation": "associate-job",
        "schema": "hi/fleet/job/v1",
        "jobId": "job-1",
        "targetId": "session-1",
        "generation": 1,
        "bindingDigest": "a" * 64,
        "inputCommandId": "cmd-2",
        "inputIdempotencyKey": "key-2",
        "inputSha256": hashlib.sha256(b"tool").hexdigest(),
    }


def read_result(worker, **overrides):
    """Read one exact private association without dispatching work."""
    return _dispatch(
        worker,
        {
            key: value
            for key, value in {**association(), "operation": "job-result", **overrides}.items()
            if key not in {"inputCommandId", "inputIdempotencyKey", "inputSha256"}
        },
    )


def notify(worker, method, params):
    """Deliver a synthetic message through the existing provider fixture."""
    worker.provider.request("fixture/message", {"message": {"method": method, "params": params}})
    worker.poll()


def begin_job(worker):
    """Associate an existing session before its ordinary admitted input."""
    assert start(worker)["status"] == "completed"
    assert _dispatch(worker, association())["status"] == "associated"
    assert worker.handle(command("input", number=2, payload={"text": "tool"}))["status"] == (
        "completed"
    )
    return worker.journal.sessions["session-1"]


def test_input_ack_does_not_supply_a_terminal_job_result(contained_worker):
    """The accepted turn still owns its container and has no terminal result."""
    worker, owner, lease = contained_worker
    begin_job(worker)
    observed = read_result(worker)
    assert observed["status"] == "pending"
    assert observed["result"] is None
    assert "remove" not in owner.engine.calls
    assert owner.inspect(lease["leaseId"])["phase"] != "disposed"
    assert worker.inventory()["activeReservations"] == 1


def test_completed_job_returns_private_answer_after_causal_disposal(contained_worker):
    """A matching final answer and disposed boundary produce the real turn result."""
    worker, owner, lease = contained_worker
    session = begin_job(worker)
    thread_id, turn_id = session["providerThreadId"], session["providerTurnId"]
    notify(
        worker,
        "item/completed",
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "completedAtMs": 1000,
            "item": {
                "type": "agentMessage",
                "id": "final-1",
                "text": "Private final answer.",
                "phase": "final_answer",
                "memoryCitation": None,
                "delivery": None,
                "questions": None,
            },
        },
    )
    assert read_result(worker)["result"] is None
    worker.provider.request("fixture/status", {"threadId": thread_id, "status": "idle"})
    notify(
        worker,
        "turn/completed",
        {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "error": None}},
    )
    observed = read_result(worker)
    assert observed["status"] == "completed"
    result = observed["result"]
    assert result["schema"] == "hi/fleet/job-result/v1"
    assert result["bindingDigest"] == association()["bindingDigest"]
    assert result["providerTurnId"] == turn_id
    assert result["outcome"] == "completed"
    assert result["answer"]["text"] == "Private final answer."
    assert result["answer"]["itemId"] == "final-1"
    disposed = owner.inspect(lease["leaseId"])
    assert disposed["phase"] == "disposed"
    assert result["disposal"] == {
        "leaseId": lease["leaseId"],
        "digest": disposed["disposal"]["digest"],
    }
    assert owner.engine.calls.count("remove") == 1
    assert worker.inventory()["activeReservations"] == 1
    retained = worker.journal.sessions["session-1"]
    assert retained["released"] is False
    assert retained["outcome"] == "completed"
    assert retained.get("stopCommandId") is None
    assert "Private final answer." not in json.dumps(worker.events(0))


def complete_turn(worker, *, answer="Private answer.", status="completed", error=None):
    """Emit pinned provider fields through the synthetic transport."""
    session = worker.journal.sessions["session-1"]
    thread_id, turn_id = session["providerThreadId"], session["providerTurnId"]
    if answer is not None:
        notify(
            worker,
            "item/completed",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "completedAtMs": 1000,
                "item": {
                    "type": "agentMessage",
                    "id": "final-1",
                    "text": answer,
                    "phase": "final_answer",
                    "memoryCitation": None,
                    "delivery": None,
                    "questions": None,
                },
            },
        )
    worker.provider.request("fixture/status", {"threadId": thread_id, "status": "idle"})
    terminal = {"threadId": thread_id, "turn": {"id": turn_id, "status": status, "error": error}}
    notify(worker, "turn/completed", terminal)
    return terminal


def test_failed_provider_retains_native_error_without_inventing_an_answer(contained_worker):
    """A failed provider result remains failed after confirmed cleanup."""
    worker, owner, lease = contained_worker
    begin_job(worker)
    error = {
        "message": "Synthetic provider rejected this turn.",
        "codexErrorInfo": None,
        "additionalDetails": "Synthetic failure detail.",
        "misalignment": None,
    }
    complete_turn(worker, answer=None, status="failed", error=error)
    observed = read_result(worker)
    assert observed["status"] == "failed"
    assert observed["result"]["outcome"] == "failed"
    assert observed["result"]["answer"] is None
    assert observed["result"]["error"] == error
    assert owner.inspect(lease["leaseId"])["phase"] == "disposed"
    assert worker.inventory()["activeReservations"] == 1
    assert error["message"] not in json.dumps(worker.events(0))


def test_missing_success_answer_still_disposes_but_retains_the_assignment(contained_worker):
    """A terminated container need not keep running when its answer is missing."""
    worker, owner, lease = contained_worker
    begin_job(worker)
    complete_turn(worker, answer=None)
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None
    assert owner.inspect(lease["leaseId"])["phase"] == "disposed"
    assert worker.inventory()["activeReservations"] == 1


def test_pipeline_association_preserves_explicit_cancel_cleanup(contained_worker):
    """A pending job must not intercept the existing cancellation operation."""
    from tests.unit.automation.test_fleet_worker import wait_state

    worker, owner, lease = contained_worker
    begin_job(worker)
    assert worker.handle(command("cancel", number=3))["status"] == "accepted"
    session = wait_state(worker, "session-1", "idle")
    assert session["outcome"] == "cancelled"
    assert session["released"] is True
    assert owner.inspect(lease["leaseId"])["phase"] == "disposed"
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None


def test_provider_disconnect_makes_a_running_job_unknown(contained_worker):
    """Connection loss cannot continue to report a confirmed pending turn."""
    worker, owner, _lease = contained_worker
    begin_job(worker)
    worker.provider.close()
    worker.poll()
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None
    assert "remove" not in owner.engine.calls
    assert worker.inventory()["activeReservations"] == 1


@pytest.mark.parametrize("completed", [False, True], ids=["uncertain-turn", "terminal-result"])
def test_reopened_journal_preserves_results_and_marks_unfinished_work_unknown(
    contained_worker, completed
):
    """A process restart supplies evidence without granting a new turn."""
    from hephaestus.automation.fleet_job_results import FleetJobResults
    from hephaestus.automation.fleet_journal import WorkerJournal

    worker, owner, _lease = contained_worker
    begin_job(worker)
    if completed:
        complete_turn(worker)
    before = read_result(worker)
    directory = worker.journal.directory
    worker.close()
    journal = WorkerJournal(directory)
    try:
        reopened = SimpleNamespace(jobs=FleetJobResults(journal))
        observed = read_result(reopened)
        if completed:
            assert observed == before
        else:
            assert observed["status"] == "unknown"
            assert observed["result"] is None
            assert "remove" not in owner.engine.calls
    finally:
        journal.close()


def test_result_binds_the_existing_stage_and_checks_its_own_digest(contained_worker):
    """The downstream consumer receives the actual assignment role and bytes."""
    worker, _owner, _lease = contained_worker
    begin_job(worker)
    complete_turn(worker)
    result = read_result(worker)["result"]
    assert result["owner"]["stage"] == "implementation"
    expected = result.pop("sha256")
    assert (
        hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == expected
    )
    assert read_result(worker)["result"]["sha256"] == expected


def test_identical_terminal_replay_is_inert_and_conflicting_replay_is_withheld(contained_worker):
    """A later conflicting observation cannot replace the original result."""
    worker, owner, _lease = contained_worker
    begin_job(worker)
    terminal = complete_turn(worker)
    original = read_result(worker)
    before = (worker.journal.directory / "receipts.jsonl").read_bytes()
    notify(worker, "turn/completed", terminal)
    assert read_result(worker) == original
    assert (worker.journal.directory / "receipts.jsonl").read_bytes() == before
    changed = {**terminal, "turn": {**terminal["turn"], "status": "failed"}}
    notify(worker, "turn/completed", changed)
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None
    assert worker.journal.jobs["job-1"]["result"] == original["result"]
    assert owner.engine.calls.count("remove") == 1


@pytest.mark.parametrize(
    "field,value", [("targetId", []), ("generation", True), ("bindingDigest", "bad")]
)
def test_malformed_association_is_rejected_without_a_type_error(contained_worker, field, value):
    """Malformed private requests do not damage the serial worker server."""
    worker, _owner, _lease = contained_worker
    assert start(worker)["status"] == "completed"
    with pytest.raises(ValueError):
        _dispatch(worker, {**association(), field: value})
    assert worker.journal.jobs == {}


@pytest.mark.parametrize("missing", ["environment_registry", "containment_supervisor"])
def test_association_requires_the_owned_containment_boundary(contained_worker, missing):
    """The interactive no-registry disposal path cannot qualify a job."""
    worker, owner, _lease = contained_worker
    assert start(worker)["status"] == "completed"
    setattr(worker, missing, None)
    with pytest.raises(ValueError, match="pipeline_containment_required"):
        _dispatch(worker, association())
    assert worker.journal.jobs == {}
    assert "remove" not in owner.engine.calls


@pytest.mark.parametrize(
    "field,value",
    [("bindingDigest", "b" * 64), ("generation", 2), ("targetId", "session-2"), ("jobId", "other")],
)
def test_private_result_read_requires_the_exact_association(contained_worker, field, value):
    """An incorrect selector cannot read the final private answer."""
    worker, _owner, _lease = contained_worker
    begin_job(worker)
    complete_turn(worker)
    with pytest.raises(ValueError, match="job_not_found"):
        read_result(worker, **{field: value})


@pytest.mark.parametrize("failure", ["owner", "idle", "remove"])
def test_completion_keeps_the_specific_unconfirmed_boundary_cause(contained_worker, failure):
    """An ownership or cleanup failure stays distinct and cannot publish success."""
    worker, owner, lease = contained_worker
    begin_job(worker)
    if failure == "owner":
        worker.journal.sessions["session-1"]["taskId"] = "other-task"
        expected = "job_owner_mismatch"
    elif failure == "remove":
        owner.engine.fail_remove = True
        expected = "container_disposal_unconfirmed"
    else:
        worker.provider.request(
            "fixture/next-result",
            {
                "method": "thread/read",
                "result": {"thread": {"id": "thread-1", "status": {"type": "active"}}},
            },
        )
        expected = "provider_not_confirmed_idle"
    complete_turn(worker)
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None
    assert worker.journal.sessions["session-1"]["waitingReason"] == expected
    assert owner.inspect(lease["leaseId"])["phase"] != "disposed"
    assert worker.inventory()["activeReservations"] == 1


def test_terminal_job_clears_only_its_pending_provider_requests(contained_worker):
    """Job completion uses the same pending-request cleanup as an interactive turn."""
    worker, _owner, _lease = contained_worker
    session = begin_job(worker)
    worker.provider.request(
        "fixture/message",
        {
            "message": {
                "id": "approval-job",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": session["providerThreadId"],
                    "turnId": session["providerTurnId"],
                    "itemId": "command-1",
                    "command": "synthetic command",
                },
            }
        },
    )
    worker.poll()
    assert "approval-job" in worker.pending
    complete_turn(worker)
    assert "approval-job" not in worker.pending
    assert worker._pending_bytes == 0
    assert worker._pending_sizes == {}


def final_item(worker, text):
    """Return a foreground final item for the retained provider turn."""
    session = worker.journal.sessions["session-1"]
    return {
        "threadId": session["providerThreadId"],
        "turnId": session["providerTurnId"],
        "completedAtMs": 1000,
        "item": {
            "type": "agentMessage",
            "id": "final-1",
            "text": text,
            "phase": "final_answer",
            "memoryCitation": None,
            "delivery": None,
            "questions": None,
        },
    }


def test_late_final_item_replay_keeps_original_evidence_and_withholds_conflicts(contained_worker):
    """Late output cannot replace a retained final answer or silently conflict."""
    worker, owner, _lease = contained_worker
    begin_job(worker)
    complete_turn(worker)
    original = read_result(worker)
    path = worker.journal.directory / "receipts.jsonl"
    before = path.read_bytes()
    notify(worker, "item/completed", final_item(worker, "Private answer."))
    assert read_result(worker) == original
    assert path.read_bytes() == before
    notify(worker, "item/completed", final_item(worker, "Different private answer."))
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None
    assert worker.journal.jobs["job-1"]["result"] == original["result"]
    assert worker.journal.sessions["session-1"]["activity"] == "unknown"
    assert owner.engine.calls.count("remove") == 1


@pytest.mark.parametrize("character", ["\0", "é"], ids=["json-escaped", "utf8"])
@pytest.mark.parametrize("extra", [0, 1], ids=["at-limit", "over-limit"])
def test_final_answer_bounds_use_bytes_and_fit_private_frames(contained_worker, character, extra):
    """The final answer bound includes UTF-8 and each encoded private frame."""
    worker, owner, lease = contained_worker
    begin_job(worker)
    answer = character * (65536 // len(character.encode())) + "x" * extra
    complete_turn(worker, answer=answer)
    observed = read_result(worker)
    assert len((json.dumps(observed) + "\n").encode()) < 1024 * 1024
    if extra:
        assert observed["status"] == "unknown"
        assert observed["result"] is None
    else:
        assert observed["status"] == "completed"
        assert observed["result"]["answer"]["text"] == answer
    assert all(
        len(line) + 1 <= 1024 * 1024
        for line in (worker.journal.directory / "receipts.jsonl").read_bytes().splitlines()
    )
    assert owner.inspect(lease["leaseId"])["phase"] == "disposed"
    assert worker.inventory()["activeReservations"] == 1


@pytest.mark.parametrize(
    "field,value", [("phase", None), ("phase", "commentary"), ("delivery", "async")]
)
def test_nonfinal_or_async_output_is_not_a_completed_answer(contained_worker, field, value):
    """Only a foreground item with an explicit final phase supplies the answer."""
    worker, _owner, _lease = contained_worker
    begin_job(worker)
    item = final_item(worker, "Not a final answer.")
    item["item"][field] = value
    notify(worker, "item/completed", item)
    complete_turn(worker, answer=None)
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None


@pytest.mark.parametrize("changed", ["text", "commandId", "idempotencyKey"])
def test_input_must_match_the_private_association(contained_worker, changed):
    """A mismatched admitted input cannot use the reserved job turn."""
    worker, _owner, _lease = contained_worker
    assert start(worker)["status"] == "completed"
    assert _dispatch(worker, association())["status"] == "associated"
    request = command("input", number=2, payload={"text": "tool"})
    if changed == "text":
        request["payload"]["text"] = "other"
    else:
        request[changed] = "other"
    assert worker.handle(request)["receipt"]["error"] == "job_input_mismatch"
    assert worker.provider.request("fixture/last-request", {"method": "turn/start"}) == {}
    assert read_result(worker)["result"] is None


@contextmanager
def replacement_boundary(worker):
    """Install a distinct valid synthetic lease for the same assignment."""
    from hephaestus.automation.fleet_attachment import AttachmentEndpoint
    from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry
    from tests.unit.automation.test_fleet_containment import specification, supervisor

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
        old_registry, old_owner = worker.environment_registry, worker.containment_supervisor
        original_config = old_registry.path.read_bytes()
        try:
            registry = EnvironmentRegistry(
                worker.codex_home,
                [EnvironmentLease.from_endpoint(endpoint, "session-1-env", Path(sys.executable))],
            )
            registry.path.unlink()
            registry.write_configuration()
            worker.environment_registry, worker.containment_supervisor = registry, owner
            yield owner, lease
        finally:
            old_registry.path.write_bytes(original_config)
            worker.environment_registry, worker.containment_supervisor = old_registry, old_owner
            endpoint.close()
            owner.close()


@pytest.mark.parametrize("operation", ["associate-job", "input", "terminal"])
def test_associated_job_cannot_rebind_to_replacement_containment(contained_worker, operation):
    """A new valid lease cannot replace the container bound to an existing job."""
    worker, owner, _lease = contained_worker
    if operation == "terminal":
        begin_job(worker)
    else:
        assert start(worker)["status"] == "completed"
        assert _dispatch(worker, association())["status"] == "associated"
    original = json.loads(json.dumps(worker.journal.jobs["job-1"]))
    with replacement_boundary(worker) as (replacement, _replacement_lease):
        if operation == "associate-job":
            with pytest.raises(ValueError, match="job_association_conflict"):
                _dispatch(worker, association())
        elif operation == "input":
            result = worker.handle(command("input", number=2, payload={"text": "tool"}))
            assert result["status"] == "failed"
            assert result["receipt"]["error"] == "job_lease_mismatch"
            assert worker.provider.request("fixture/last-request", {"method": "turn/start"}) == {}
        else:
            complete_turn(worker)
            assert read_result(worker)["status"] == "unknown"
        assert worker.journal.jobs["job-1"]["lease"] == original["lease"]
        assert read_result(worker)["result"] is None
        assert "remove" not in replacement.engine.calls
    assert "remove" not in owner.engine.calls


@pytest.mark.parametrize("boundary", ["dispatch", "result"])
def test_journal_failure_never_authorizes_a_turn_or_terminal_result(
    contained_worker, monkeypatch, boundary
):
    """A failed durable write retains uncertain work for restart reconciliation."""
    from hephaestus.automation.fleet_job_results import FleetJobResults
    from hephaestus.automation.fleet_journal import WorkerJournal

    worker, owner, lease = contained_worker
    if boundary == "result":
        begin_job(worker)
    else:
        assert start(worker)["status"] == "completed"
        assert _dispatch(worker, association())["status"] == "associated"
    append = worker.journal.append

    def fail_write(kind, value):
        if kind == "job" and (
            value["phase"] == "dispatching"
            if boundary == "dispatch"
            else value["result"] is not None
        ):
            raise OSError("synthetic journal write failure")
        return append(kind, value)

    monkeypatch.setattr(worker.journal, "append", fail_write)
    with pytest.raises(OSError, match="synthetic journal write failure"):
        if boundary == "dispatch":
            worker.handle(command("input", number=2, payload={"text": "tool"}))
        else:
            complete_turn(worker)
    assert worker.journal.jobs["job-1"]["result"] is None
    if boundary == "dispatch":
        assert worker.provider.request("fixture/last-request", {"method": "turn/start"}) == {}
        assert "remove" not in owner.engine.calls
    else:
        assert owner.inspect(lease["leaseId"])["phase"] == "disposed"
    directory = worker.journal.directory
    worker.close()
    reopened = WorkerJournal(directory)
    try:
        observed = read_result(SimpleNamespace(jobs=FleetJobResults(reopened)))
        assert observed["status"] == "unknown"
        assert observed["result"] is None
        assert reopened.sessions["session-1"]["admissionReserved"] is True
    finally:
        reopened.close()


def test_missing_input_receipt_cannot_later_supply_a_successful_job(contained_worker, monkeypatch):
    """A provider effect without a confirmed input receipt remains uncertain."""
    worker, owner, lease = contained_worker
    assert start(worker)["status"] == "completed"
    assert _dispatch(worker, association())["status"] == "associated"
    append = worker.journal.append

    def fail_receipt(kind, value):
        if kind == "receipt" and value["key"] == "key-2":
            raise OSError("synthetic input receipt failure")
        return append(kind, value)

    monkeypatch.setattr(worker.journal, "append", fail_receipt)
    with pytest.raises(OSError, match="synthetic input receipt failure"):
        worker.handle(command("input", number=2, payload={"text": "tool"}))
    assert worker.provider.request("fixture/last-request", {"method": "turn/start"})
    complete_turn(worker)
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None
    assert owner.inspect(lease["leaseId"])["phase"] == "disposed"
    assert worker.inventory()["activeReservations"] == 1


def test_rejected_terminal_journal_change_does_not_poison_retained_records(contained_worker):
    """Reject a changed result before its bytes enter the replay journal."""
    worker, _owner, _lease = contained_worker
    begin_job(worker)
    complete_turn(worker)
    original = read_result(worker)
    path = worker.journal.directory / "receipts.jsonl"
    before = path.read_bytes()
    retained = worker.journal.jobs["job-1"]
    with pytest.raises(RuntimeError, match="terminal job result changed"):
        worker.journal.append(
            "job", {**retained, "result": {**retained["result"], "outcome": "failed"}}
        )
    assert path.read_bytes() == before
    assert read_result(worker) == original


def test_idle_cancel_before_job_input_does_not_leave_a_pending_result(contained_worker):
    """Cancellation resolves the local reservation without implying an executed job."""
    worker, owner, lease = contained_worker
    assert start(worker)["status"] == "completed"
    assert _dispatch(worker, association())["status"] == "associated"
    assert worker.handle(command("cancel", number=3))["status"] == "completed"
    session = worker.journal.sessions["session-1"]
    assert session["released"] is True
    assert session["outcome"] == "cancelled"
    assert owner.inspect(lease["leaseId"])["phase"] == "disposed"
    assert read_result(worker)["status"] == "unknown"
    assert read_result(worker)["result"] is None


def test_prior_interactive_terminal_replay_keeps_next_associated_input_usable(contained_worker):
    """A prior turn cannot become the terminal result of a future associated turn."""
    from tests.unit.automation.test_fleet_worker import wait_state

    worker, owner, _lease = contained_worker
    assert start(worker)["status"] == "completed"
    assert worker.handle(command("input", number=2, payload={"text": "finish"}))["status"] == (
        "completed"
    )
    session = wait_state(worker, "session-1", "idle")
    prior_turn = session["providerTurnId"]
    assert (
        _dispatch(
            worker,
            {**association(), "inputCommandId": "cmd-3", "inputIdempotencyKey": "key-3"},
        )["status"]
        == "associated"
    )
    notify(
        worker,
        "turn/completed",
        {
            "threadId": session["providerThreadId"],
            "turn": {"id": prior_turn, "status": "completed", "error": None},
        },
    )
    assert read_result(worker)["status"] == "pending"
    assert worker.journal.sessions["session-1"]["activity"] == "idle"
    assert worker.handle(command("input", number=3, payload={"text": "tool"}))["status"] == (
        "completed"
    )
    assert worker.journal.sessions["session-1"]["providerTurnId"] != prior_turn
    assert read_result(worker)["result"] is None
    assert "remove" not in owner.engine.calls
