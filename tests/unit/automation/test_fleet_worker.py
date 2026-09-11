"""Check Fleet receipts and lifecycle against a real provider subprocess."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.precommit
FIXTURE = Path(__file__).parents[2] / "fixtures" / "fleet_provider.py"


def modules():
    """Load the feature after collection so RED names the missing capability."""
    from hephaestus.automation import fleet_worker

    return fleet_worker


def command(operation, *, target="session-1", number=1, generation=1, payload=None):
    """Return one controller command without provider configuration overrides."""
    return {
        "schema": "hi/fleet/v1",
        "commandId": f"cmd-{number}",
        "idempotencyKey": f"key-{number}",
        "generation": generation,
        "targetKind": "sessions",
        "targetId": target,
        "operation": operation,
        "workerId": "worker-a",
        "payload": payload or {},
    }


@pytest.fixture
def worker(tmp_path):
    """Use private temporary state and the protocol fixture process."""
    module = modules()
    root = tmp_path / "workspaces"
    root.mkdir()
    (root / "one").mkdir()
    (root / "two").mkdir()
    (root / "three").mkdir()
    codex_home = tmp_path / "codex"
    codex_home.mkdir(mode=0o700)
    instance = module.FleetWorker(
        state_dir=tmp_path / "state",
        workspace_root=root,
        codex_home=codex_home,
        worker_id="worker-a",
        pool_id="local",
        host_id="laptop",
        generation=1,
        capacity=2,
        provider_command=[sys.executable, "-u", str(FIXTURE)],
    )
    # This deterministic provider performs no real model/tool execution.
    instance.storage_guard = lambda: None
    instance.execution_guard = lambda: None
    instance.start()
    yield instance
    instance.close()


def start(worker, *, target="session-1", number=1, workspace="one"):
    """Create one independent logical agent with its assignment identity."""
    return worker.handle(
        command(
            "start",
            target=target,
            number=number,
            payload={
                "workspace": workspace,
                "permissions": "fleet",
                "agentId": target + "-agent",
                "taskId": "task-1",
                "executionId": target + "-exec",
                "stage": "implementation",
                "issueRefs": ["HomericIntelligence/Hephaestus#1"],
            },
        )
    )


def wait_state(worker, target, expected):
    """Wait for the fixture's ordered protocol notifications."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = worker.inventory()
        sessions = {s["sessionId"]: s for s in result["sessions"]}
        if sessions[target]["activity"] == expected:
            return sessions[target]
        time.sleep(0.01)
    pytest.fail(f"activity did not reach {expected}: {result}")


def test_worker_routes_distinct_threads_and_metadata_only_events(worker):
    """Keep workspace ownership and activity identities across shared runtime use."""
    assert start(worker)["status"] == "completed"
    assert start(worker, target="session-2", number=2, workspace="two")["status"] == "completed"
    identities = worker.inventory()["sessions"]
    assert identities[0]["providerThreadId"] != identities[1]["providerThreadId"]
    assert (
        worker.handle(command("input", number=3, payload={"text": "tool"}))["status"] == "completed"
    )
    session = wait_state(worker, "session-1", "tool_running")
    assert session["agentId"] == "session-1-agent"
    assert session["workerId"] == "worker-a"
    assert session["taskId"] == "task-1"
    assert session["executionId"] == "session-1-exec"
    facts = worker.events(0)
    assert facts["events"] and facts["cursor"] >= len(facts["events"])
    encoded = json.dumps(facts)
    assert "private-command" not in encoded
    assert "private-answer" not in encoded
    assert facts["events"][-1]["event"]["observedAt"]


def test_worker_sends_singleton_environment_on_each_provider_start(worker):
    """Bind actual fixture RPCs to the admitted session's sole tool container."""
    from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry

    item = EnvironmentLease(
        worker_id="worker-a",
        session_id="session-1",
        generation=1,
        environment_id="session-1-g1",
        container_id="1" * 64,
        image_digest="sha256:" + "a" * 64,
        workspace=worker.workspace_root / "one",
        engine_program=Path("/usr/bin/podman"),
    )
    registry = EnvironmentRegistry(worker.codex_home, [item])
    registry.write_configuration()
    # The fixture launches no tool environment. A real runtime installs this before start.
    worker.environment_registry = registry
    assert start(worker)["status"] == "completed"
    expected = [
        {
            "environmentId": "session-1-g1",
            "cwd": "/workspace",
            "runtimeWorkspaceRoots": ["/workspace"],
        }
    ]
    parameters = worker.provider.request("fixture/last-request", {"method": "thread/start"})
    assert parameters.get("environments") == expected
    assert (
        worker.handle(command("input", number=2, payload={"text": "tool"}))["status"] == "completed"
    )
    parameters = worker.provider.request("fixture/last-request", {"method": "turn/start"})
    assert parameters.get("environments") == expected
    registry.path.write_text("include_local = true\n")
    result = worker.handle(command("input", number=3, payload={"text": "tool"}))
    assert result["status"] == "failed"
    assert result["receipt"]["error"] == "environment_configuration_changed"


def test_worker_cannot_cold_resume_an_unreconciled_remote_environment(worker):
    """Do not send an unsupported selection field or infer ownership on resume."""
    from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry

    item = EnvironmentLease(
        worker_id="worker-a",
        session_id="session-1",
        generation=1,
        environment_id="session-1-g1",
        container_id="1" * 64,
        image_digest="sha256:" + "a" * 64,
        workspace=worker.workspace_root / "one",
        engine_program=Path("/usr/bin/podman"),
    )
    registry = EnvironmentRegistry(worker.codex_home, [item])
    registry.write_configuration()
    worker.environment_registry = registry
    assert start(worker)["status"] == "completed"
    assert (
        worker.handle(command("input", number=2, payload={"text": "tool"}))["status"] == "completed"
    )
    assert worker.handle(command("interrupt", number=3))["status"] == "accepted"
    wait_state(worker, "session-1", "idle")
    result = worker.handle(command("resume", number=4))
    assert result["status"] == "failed"
    assert result["receipt"]["error"] == "environment_resume_requires_reconciliation"
    assert worker.provider.request("fixture/last-request", {"method": "thread/resume"}) == {}


def test_provider_has_private_environment_without_parent_home_or_xdg(worker):
    """Keep runtime configuration and scratch beneath its private provider home."""
    names = ["HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR", "TMP", "TEMP"]
    environment = worker.provider.request("fixture/environment", {"names": names})
    for name in names:
        path = Path(environment[name])
        assert path.is_relative_to(worker.codex_home)
        assert path.is_dir()
        assert not path.stat().st_mode & 0o077


def test_tool_environment_is_private_to_each_session_and_drops_provider_context(worker):
    """Give each logical agent a separate explicit environment."""
    first = start(worker)
    second = start(worker, target="session-2", number=2, workspace="two")
    policies = []
    for receipt in (first, second):
        config = worker.provider.request(
            "fixture/configuration", {"threadId": receipt["receipt"]["providerThreadId"]}
        )
        policy = config["config"]["shell_environment_policy"]
        assert policy["inherit"] == "none"
        assert policy["ignore_default_excludes"] is False
        assert policy["experimental_use_profile"] is False
        assert "CODEX_HOME" not in policy["set"]
        assert "SSH_AUTH_SOCK" not in policy["set"]
        assert config["config"]["features"]["shell_snapshot"] is False
        for name in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            path = Path(policy["set"][name])
            assert path.is_dir() and path.is_relative_to(Path(config["cwd"]))
            assert not path.stat().st_mode & 0o077
        policies.append(policy)
    assert policies[0]["set"]["HOME"] != policies[1]["set"]["HOME"]


@pytest.mark.parametrize("operation", ["cancel", "interrupt"])
@pytest.mark.parametrize("mode", ["clean", "retained", "error"])
def test_stop_requires_confirmed_empty_background_inventory(worker, operation, mode):
    """Release stop reservations only after provider inventory confirms cleanup."""
    receipt = start(worker)
    thread_id = receipt["receipt"]["providerThreadId"]
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    worker.provider.request("fixture/background", {"threadId": thread_id, "mode": mode})
    assert worker.handle(command(operation, number=3))["status"] == "accepted"
    expected = "idle" if mode == "clean" else "unknown"
    session = wait_state(worker, "session-1", expected)
    if mode == "clean":
        assert session["admissionReserved"] is False
        assert session["released"] is (operation == "cancel")
        assert session["backgroundCleanup"] == "confirmed_empty"
    else:
        assert session["admissionReserved"] is True
        assert session["released"] is False
        assert session["waitingReason"] == "background_cleanup_unconfirmed"
        assert session.get("outcome") not in {"cancelled", "interrupted"}
    count = worker.provider.request("fixture/cleanup-count", {"threadId": thread_id})
    assert count["count"] == 1
    assert "private-background-command" not in json.dumps(worker.events(0))


def test_idle_cancel_keeps_workspace_when_background_cleanup_is_unconfirmed(worker):
    """An idle turn does not prove background processes have stopped."""
    receipt = start(worker)
    worker.provider.request(
        "fixture/background",
        {"threadId": receipt["receipt"]["providerThreadId"], "mode": "retained"},
    )
    result = worker.handle(command("cancel", number=2))
    assert result["status"] == "failed"
    session = wait_state(worker, "session-1", "unknown")
    assert session["released"] is False
    assert session["admissionReserved"] is True


def test_background_cleanup_has_an_overall_deadline(worker):
    """A stalled cleanup cannot hold the control socket beyond its bounded budget."""
    receipt = start(worker)
    worker.provider.request(
        "fixture/background",
        {"threadId": receipt["receipt"]["providerThreadId"], "mode": "timeout"},
    )
    started = time.monotonic()
    result = worker.handle(command("cancel", number=2))
    assert time.monotonic() - started < 1.5
    assert result["status"] == "failed"
    session = worker.journal.sessions["session-1"]
    assert session["activity"] == "unknown"
    assert session["admissionReserved"] is True


def test_stopped_turn_cannot_answer_old_approval_after_cleanup_failure(worker):
    """A retained request cannot revive a turn that stopped with uncertain cleanup."""
    receipt = start(worker)
    worker.handle(command("input", number=2, payload={"text": "approval"}))
    wait_state(worker, "session-1", "waiting_approval")
    worker.provider.request(
        "fixture/background",
        {"threadId": receipt["receipt"]["providerThreadId"], "mode": "retained"},
    )
    worker.handle(command("cancel", number=3))
    wait_state(worker, "session-1", "unknown")
    response = worker.handle(
        command(
            "respond",
            number=4,
            payload={"requestId": "approve-1", "response": {"decision": "accept"}},
        )
    )
    assert response["status"] == "failed"
    assert worker.journal.sessions["session-1"]["activity"] == "unknown"
    assert "approve-1" not in worker.pending


def test_resume_clears_prior_cleanup_confirmation(worker):
    """Reactivation cannot retain an old inactivity receipt."""
    start(worker)
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    worker.handle(command("interrupt", number=3))
    stopped = wait_state(worker, "session-1", "idle")
    assert stopped["backgroundCleanup"] == "confirmed_empty"
    assert worker.handle(command("resume", number=4))["status"] == "completed"
    assert worker.journal.sessions["session-1"].get("backgroundCleanup") is None


@pytest.mark.parametrize("mode", ["retained", "list_error"])
def test_new_turn_never_reuses_cleanup_from_a_previous_turn(worker, mode):
    """Bind inactivity evidence to fresh inventory for the latest provider turn."""
    receipt = start(worker)
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    worker.handle(command("interrupt", number=3))
    stopped = wait_state(worker, "session-1", "idle")
    old_turn = stopped["providerTurnId"]
    assert stopped["backgroundCleanup"] == "confirmed_empty"
    assert worker.handle(command("resume", number=4))["status"] == "completed"
    worker.provider.request(
        "fixture/background", {"threadId": receipt["receipt"]["providerThreadId"], "mode": mode}
    )
    after = len(worker.journal.events)
    assert (
        worker.handle(command("input", number=5, payload={"text": "finish"}))["status"]
        == "completed"
    )
    finished = wait_state(worker, "session-1", "idle")
    assert finished["providerTurnId"] != old_turn
    assert finished["backgroundCleanup"] == "unconfirmed"
    assert finished["waitingReason"] == "background_cleanup_unconfirmed"
    assert finished["admissionReserved"] is True
    for event in worker.journal.events[after:]:
        assert event["event"].get("backgroundCleanup") != "confirmed_empty"
    count = worker.provider.request(
        "fixture/cleanup-count", {"threadId": receipt["receipt"]["providerThreadId"]}
    )
    assert count["count"] == 1, "normal completion must not kill interactive background services"


def test_normal_completion_observes_fresh_empty_inventory(worker):
    """Report inactive execution only after inspecting this turn's provider inventory."""
    start(worker)
    worker.handle(command("input", number=2, payload={"text": "finish"}))
    finished = wait_state(worker, "session-1", "idle")
    assert finished["backgroundCleanup"] == "confirmed_empty"
    assert finished["providerTurnId"] == "turn-1"
    after = len(worker.journal.events)
    worker.handle(command("input", number=3, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    for event in worker.journal.events[after:]:
        assert event["event"].get("backgroundCleanup") is None


def test_duplicate_command_replays_receipt_and_rejects_changed_payload(worker):
    """Do not create a second conversation after an acknowledgment is lost."""
    first = start(worker)
    assert start(worker) == first
    assert len(worker.inventory()["sessions"]) == 1
    changed = command("start", payload={"workspace": "two"})
    assert worker.handle(changed)["receipt"]["error"] == "idempotency_conflict"
    assert (
        worker.handle(command("input", number=2, generation=0))["receipt"]["error"]
        == "stale_generation"
    )


def test_workspace_ownership_capacity_and_escape_fail_closed(worker, tmp_path):
    """Reject another writer, a path escape, and overflow without provider calls."""
    start(worker)
    assert start(worker, target="overlap", number=2)["status"] == "failed"
    assert start(worker, target="escape", number=3, workspace=str(tmp_path))["status"] == "failed"
    assert start(worker, target="session-2", number=4, workspace="two")["status"] == "completed"
    assert (
        start(worker, target="overflow", number=5, workspace="three")["receipt"]["error"]
        == "worker_capacity"
    )


@pytest.mark.parametrize(
    "text,expected,request_id,response",
    [
        ("approval", "waiting_approval", "approve-1", {"decision": "decline"}),
        ("input", "waiting_input", "input-1", {"answers": {"choice": {"answers": ["yes"]}}}),
    ],
)
def test_request_response_is_bound_to_own_session(worker, text, expected, request_id, response):
    """Route requests to their owner and reject a response from another session."""
    start(worker)
    start(worker, target="session-2", number=2, workspace="two")
    worker.handle(command("input", number=3, payload={"text": text}))
    wait_state(worker, "session-1", expected)
    payload = {"requestId": request_id, "response": response}
    assert (
        worker.handle(command("respond", target="session-2", number=4, payload=payload))["status"]
        == "failed"
    )
    assert worker.handle(command("respond", number=5, payload=payload))["status"] == "completed"
    wait_state(worker, "session-1", "idle")


def test_cancel_waits_for_provider_completion_and_drain_blocks_input(worker):
    """Do not equate an interrupt request with a completed cancellation."""
    start(worker)
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    result = worker.handle(command("cancel", number=3))
    assert result["status"] == "accepted"
    wait_state(worker, "session-1", "idle")
    assert any(event["event"].get("outcome") == "cancelled" for event in worker.events(0)["events"])
    worker.handle(command("drain", number=4))
    assert worker.handle(command("input", number=5, payload={"text": "new"}))["status"] == "failed"


def test_provider_exit_marks_unknown_and_never_reports_success(worker):
    """A provider exit cannot prove a turn completed."""
    start(worker)
    result = worker.handle(command("input", number=2, payload={"text": "crash"}))
    assert result["status"] == "failed"
    assert result["receipt"]["error"] == "provider_uncertain"
    wait_state(worker, "session-1", "unknown")


def test_journal_single_writer_replay_and_uncertain_command(tmp_path):
    """Persist intent before dispatch and fence another journal writer."""
    module = modules()
    state = tmp_path / "state"
    journal = module.WorkerJournal(state, max_bytes=4096)
    envelope = command("input", payload={"text": "sensitive-prompt"})
    assert journal.begin(envelope) is None
    with pytest.raises(RuntimeError, match="writer"):
        module.WorkerJournal(state)
    journal.close()
    assert "sensitive-prompt" not in (state / "receipts.jsonl").read_text()
    reopened = module.WorkerJournal(state)
    receipt = reopened.begin(envelope)
    assert receipt["receipt"]["error"] == "outcome_unknown"
    reopened.close()


def test_auth_home_has_one_owner_across_distinct_state_directories(worker, tmp_path):
    """Do not let two provider processes share a refresh-token owner."""
    module = modules()
    second = module.FleetWorker(
        state_dir=tmp_path / "other-state",
        workspace_root=worker.workspace_root,
        codex_home=worker.codex_home,
        worker_id="worker-b",
        pool_id="local",
        host_id="laptop",
        generation=1,
        capacity=1,
        provider_command=[sys.executable, "-u", str(FIXTURE)],
    )
    second.storage_guard = lambda: None
    try:
        with pytest.raises(RuntimeError, match="authentication owner"):
            second.start()
    finally:
        second.close()


def test_resume_rejects_an_active_turn(worker):
    """A resume acknowledgment cannot make active work disappear."""
    start(worker)
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    result = worker.handle(command("resume", number=3))
    assert result["status"] == "failed"
    assert worker.inventory()["sessions"][0]["activity"] == "tool_running"


def test_new_session_does_not_inherit_old_cancellation(worker):
    """Keep cancelled sessions terminal while a new assignment can use its workspace."""
    start(worker)
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    worker.handle(command("cancel", number=3))
    wait_state(worker, "session-1", "idle")
    assert (
        worker.handle(command("input", number=4, payload={"text": "plain"}))["status"] == "failed"
    )
    assert start(worker, target="replacement", number=5)["status"] == "completed"
    worker.handle(command("input", target="replacement", number=6, payload={"text": "plain"}))
    wait_state(worker, "replacement", "idle")
    last = worker.events(0)["events"][-1]
    assert last["event"].get("commandId") != "cmd-3"
    replacement = next(
        item for item in worker.inventory()["sessions"] if item["sessionId"] == "replacement"
    )
    assert "stopOperation" not in replacement


def test_interrupt_activity_then_resume_preserves_provider_thread_without_new_turn(worker):
    """Follow the controller's interrupt, stop confirmation, and resume contract."""
    started = start(worker)
    thread_id = started["receipt"]["providerThreadId"]
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    assert worker.handle(command("interrupt", number=3))["status"] == "accepted"
    wait_state(worker, "session-1", "idle")
    stopped = worker.events(0)["events"][-1]["event"]
    assert stopped["commandId"] == "cmd-3" and stopped["outcome"] == "interrupted"
    assert worker.handle(command("resume", number=4))["status"] == "completed"
    resumed = worker.inventory()["sessions"][0]
    assert resumed["providerThreadId"] == thread_id
    assert resumed["activity"] == "idle"
    assert (
        worker.handle(command("input", number=5, payload={"text": "tool"}))["status"] == "completed"
    )
    wait_state(worker, "session-1", "tool_running")


@pytest.mark.parametrize("finish_turn", [False, True])
def test_idle_cancel_confirms_provider_state_and_releases_workspace(worker, finish_turn):
    """Cancel unused or finished conversations only after an idle provider read."""
    start(worker)
    if finish_turn:
        worker.handle(command("input", number=2, payload={"text": "plain"}))
        wait_state(worker, "session-1", "idle")
    result = worker.handle(command("cancel", number=3))
    assert result["status"] == "completed"
    fact = worker.events(0)["events"][-1]["event"]
    assert fact["outcome"] == "cancelled" and fact["commandId"] == "cmd-3"
    assert start(worker, target="replacement", number=4)["status"] == "completed"
    assert worker.handle(command("resume", number=5))["status"] == "failed"


def test_idle_cancel_does_not_release_when_provider_observes_active_work(worker):
    """A stale local idle observation cannot establish a stopped provider turn."""
    start(worker)
    worker.provider.request("fixture/status", {"threadId": "thread-1", "status": "active"})
    assert worker.handle(command("cancel", number=2))["status"] == "failed"
    assert start(worker, target="replacement", number=3)["status"] == "failed"


@pytest.mark.parametrize("method", ["item/agentMessage/delta", "thread/tokenUsage/updated"])
def test_live_metadata_refresh_coalesces_bursts_and_ignores_stale_turns(worker, method):
    """Refresh active observations from provider facts without publishing their content."""
    clock = [100.0]
    worker.activity_clock = lambda: clock[0]
    start(worker)
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    before = worker.events(0)["cursor"]
    burst = {"threadId": "thread-1", "method": method, "count": 200}
    clock[0] += 6
    worker.provider.request("fixture/notifications", burst)
    observed = worker.events(before)
    assert len(observed["events"]) == 1
    assert observed["events"][0]["event"]["activity"] == "tool_running"
    encoded = json.dumps(observed)
    assert "private-stream-text" not in encoded and "123456789" not in encoded
    first_cursor = observed["cursor"]
    worker.provider.request("fixture/notifications", burst)
    assert worker.events(first_cursor)["events"] == []
    clock[0] += 6
    worker.provider.request("fixture/notifications", {**burst, "turnId": "old-turn"})
    assert worker.events(first_cursor)["events"] == []
    worker.provider.request("fixture/notifications", burst)
    assert len(worker.events(first_cursor)["events"]) == 1


def test_ack_and_activity_have_source_identity(worker):
    """Give every fact a deduplication key and the worker's generation."""
    result = start(worker)
    assert result["eventId"]
    activity = worker.events(0)["events"][-1]
    assert activity["workerId"] == "worker-a"
    assert activity["sourceSequence"] == activity["seq"]
    assert activity["generation"] == 1


def test_invalid_worker_command_cannot_change_existing_session(worker):
    """Reject commands routed to a different worker identity."""
    start(worker)
    wrong = command("input", number=2, payload={"text": "tool"})
    wrong["workerId"] = "another-worker"
    assert worker.handle(wrong)["receipt"]["error"] == "wrong_worker"
    assert worker.inventory()["sessions"][0]["activity"] == "idle"


def test_journal_limit_and_truncation_are_recovery_failures(tmp_path):
    """Stop on full or damaged durable storage without trimming uncertain facts."""
    module = modules()
    state = tmp_path / "state"
    journal = module.WorkerJournal(state, max_bytes=100)
    with pytest.raises(RuntimeError, match="full"):
        journal.append("event", {"value": "x" * 100})
    journal.close()
    (state / "receipts.jsonl").write_bytes(b'{"kind":"event"')
    with pytest.raises(RuntimeError, match="incomplete"):
        module.WorkerJournal(state)


def test_provider_write_deadline_applies_before_waiting_for_response(worker):
    """A provider that stops reading cannot block the worker control loop forever."""
    from hephaestus.automation.fleet_provider import ProviderError

    worker.provider.timeout = 0.1
    worker.provider.request("fixture/freeze", {})
    started = time.monotonic()
    with pytest.raises(ProviderError):
        worker.provider.request("fixture/blob", {"blob": "x" * 200000})
    assert time.monotonic() - started < 1


def test_provider_cleanup_stops_descendants_after_leader_exit(worker):
    """Stop child execution even when PID 1 retains its zombie process record."""
    result = worker.provider.request(
        "turn/start",
        {
            "threadId": "unused",
            "input": [{"type": "text", "text": "orphan"}],
        },
    )
    child_pid = result["childPid"]
    proc_stat = Path(f"/proc/{child_pid}/stat")
    original_start = None
    if sys.platform == "linux":
        original_fields = proc_stat.read_text().rsplit(")", 1)[1].split()
        assert original_fields[0] not in {"Z", "X"}
        original_start = original_fields[19]
    worker.provider.process.wait(timeout=3)

    def execution_stopped():
        if original_start is not None:
            try:
                fields = proc_stat.read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                return True
            # PID reuse and an unreaped zombie cannot run the original child.
            return fields[19] != original_start or fields[0] in {"Z", "X"}
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return True
        return False

    def emergency_cleanup():
        if not execution_stopped():
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)

    timer = threading.Timer(5, emergency_cleanup)
    timer.start()
    try:
        started = time.monotonic()
        worker.provider.close()
        assert time.monotonic() - started < 1.5
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if execution_stopped():
                break
            time.sleep(0.02)
        else:
            pytest.fail("provider descendant can still execute after cleanup")
    finally:
        timer.cancel()
        emergency_cleanup()


def test_controller_assignment_metadata_can_be_top_level(worker):
    """Consume the controller's stored assignment without a second payload copy."""
    envelope = command("start")
    envelope.update(
        {
            "workspace": "one",
            "agentId": "agent-1",
            "taskId": "task-1",
            "sessionId": "session-1",
            "executionId": "exec-1",
            "stage": "implementation",
        }
    )
    assert worker.handle(envelope)["status"] == "completed"
    assert worker.inventory()["sessions"][0]["agentId"] == "agent-1"


def test_duplicate_assignment_fields_must_agree(worker):
    """Reject a payload that changes the durable controller assignment."""
    envelope = command(
        "start",
        payload={
            "workspace": "one",
            "agentId": "agent-1",
            "taskId": "task-1",
            "executionId": "exec-1",
            "stage": "implementation",
        },
    )
    envelope["workspace"] = "two"
    assert worker.handle(envelope)["receipt"]["error"] == "assignment_conflict"


def test_live_stream_burst_cannot_overflow_lifecycle_queue(worker):
    """Coalesce raw stream observations before bounded lifecycle buffering."""
    clock = [100.0]
    worker.activity_clock = lambda: clock[0]
    start(worker)
    worker.handle(command("input", number=2, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    before = worker.events(0)["cursor"]
    clock[0] += 6
    worker.provider.request(
        "fixture/notifications",
        {
            "threadId": "thread-1",
            "method": "item/agentMessage/delta",
            "count": 5000,
        },
    )
    assert not worker.provider.failed
    observed = worker.events(before)
    assert len(observed["events"]) == 1
    assert "private-stream-text" not in json.dumps(observed)


def test_interrupt_frees_execution_capacity_but_retains_workspace_until_resume(worker):
    """Keep unfinished source owned while another workspace can use an execution slot."""
    start(worker)
    start(worker, target="session-2", number=2, workspace="two")
    worker.handle(command("input", number=3, payload={"text": "tool"}))
    wait_state(worker, "session-1", "tool_running")
    worker.handle(command("interrupt", number=4))
    wait_state(worker, "session-1", "idle")
    assert worker.inventory()["activeReservations"] == 1
    assert (
        worker.handle(command("input", number=50, payload={"text": "tool"}))["receipt"]["error"]
        == "session_not_admitted"
    )
    assert start(worker, target="overlap", number=5)["receipt"]["error"] == "workspace_owned"
    assert start(worker, target="session-3", number=6, workspace="three")["status"] == "completed"
    assert worker.handle(command("resume", number=7))["receipt"]["error"] == "worker_capacity"
    worker.handle(command("cancel", target="session-3", number=8))
    assert worker.handle(command("resume", number=9))["status"] == "completed"
    assert worker.inventory()["activeReservations"] == 2


@pytest.mark.parametrize("active_next_turn", [False, True])
@pytest.mark.parametrize(
    "method",
    ["item/commandExecution/requestApproval", "item/started", "item/completed", "turn/started"],
)
def test_late_item_or_approval_cannot_reactivate_a_finished_turn(worker, active_next_turn, method):
    """Reject callbacks from a finished turn before they reach the current activity or UI."""
    start(worker)
    worker.handle(command("input", number=2, payload={"text": "finish"}))
    wait_state(worker, "session-1", "idle")
    if active_next_turn:
        worker.handle(command("input", number=3, payload={"text": "tool"}))
        wait_state(worker, "session-1", "tool_running")
    before = worker.events(0)["cursor"]
    event = {
        "method": method,
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "turn": {"id": "turn-1"},
            "itemId": "late-item",
            "item": {"id": "late-item", "type": "commandExecution"},
            "startedAtMs": 1,
            "command": "private-old-command",
        },
    }
    if method.endswith("requestApproval"):
        event["id"] = "late-approval"
    worker.provider.request("fixture/message", {"message": event})
    assert worker.events(before)["events"] == []
    assert "late-approval" not in worker.pending
    expected = "tool_running" if active_next_turn else "idle"
    assert worker.inventory()["sessions"][0]["activity"] == expected
