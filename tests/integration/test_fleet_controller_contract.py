"""Consume commands exported by the real Agamemnon FleetService fixture."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from hephaestus.automation.fleet_worker import FleetWorker

pytestmark = pytest.mark.integration


def test_exported_controller_commands_complete_worker_stop_resume_cycle(tmp_path):
    """Use actual controller serialization and a real provider fixture process."""
    contract_path = os.environ.get("FLEET_CONTROLLER_CONTRACT")
    if not contract_path:
        pytest.skip("Set FLEET_CONTROLLER_CONTRACT to an actual just fleet-export artifact")
    contract = json.loads(Path(contract_path).read_text())
    assert contract["schema"] == "hi/fleet/contracts/v1"
    commands = [row["command"] for row in contract["commands"]]
    assert [item["operation"] for item in commands] == [
        "start",
        "input",
        "interrupt",
        "resume",
        "cancel",
    ]
    root = tmp_path / "work"
    workspace = root / "session-1"
    workspace.mkdir(parents=True)
    codex_home = tmp_path / "codex"
    codex_home.mkdir(mode=0o700)
    provider_fixture = Path(__file__).parents[1] / "fixtures" / "fleet_provider.py"
    worker = FleetWorker(
        state_dir=tmp_path / "state",
        workspace_root=root,
        codex_home=codex_home,
        worker_id=commands[0]["workerId"],
        pool_id="fixture-pool",
        host_id="fixture-host",
        generation=commands[0]["generation"],
        capacity=2,
        provider_command=[sys.executable, "-u", str(provider_fixture)],
    )
    # The subprocess is a protocol fixture, not a native execution boundary.
    worker.storage_guard = lambda: None
    worker.execution_guard = lambda: None
    try:
        worker.start()
        provider_thread = None
        for original in commands:
            command = json.loads(json.dumps(original))
            # The physical fixture mount is the only assignment mapping.
            assert command["workspace"] == "/work/session-1"
            command["workspace"] = str(workspace)
            if command["operation"] == "input":
                assert command["payload"]["inputRef"] == "a" * 32 + ".json"
                command["payload"]["text"] = "tool"
            result = worker.handle(command)
            expected = "accepted" if command["operation"] == "interrupt" else "completed"
            assert result["status"] == expected, result
            if command["operation"] == "input":
                _wait_activity(worker, "tool_running")
            else:
                _wait_activity(worker, "idle")
            session = worker.inventory()["sessions"][0]
            assert session["executionId"] == command["executionId"]
            if provider_thread is None:
                provider_thread = session["providerThreadId"]
            assert session["providerThreadId"] == provider_thread
            if command["operation"] in {"interrupt", "cancel"}:
                fact = worker.events(0)["events"][-1]["event"]
                assert fact["commandId"] == command["commandId"]
                assert worker.inventory()["activeReservations"] == 0
        assert worker.inventory()["sessions"][0]["released"] is True
    finally:
        worker.close()


def _wait_activity(worker, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        session = worker.inventory()["sessions"][0]
        if session["activity"] == expected:
            return
        time.sleep(0.01)
    pytest.fail(f"provider activity did not reach {expected}: {session['activity']}")
