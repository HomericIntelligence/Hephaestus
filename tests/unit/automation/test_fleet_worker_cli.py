"""Route private evidence reads without dispatching an execution command."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_worker_cli import _dispatch
from tests.unit.automation.test_fleet_request_evidence import Worker

pytestmark = pytest.mark.precommit


class AttachedWorker(Worker):
    """Retain a visible fallback for an unsupported private operation."""

    def __init__(self) -> None:
        super().__init__()
        self.control_messages: list[dict[str, Any]] = []

    def handle(self, message):
        self.control_messages.append(message)
        return {"error": "unsupported_operation"}


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_worker_cli_supports_the_shared_version_contract(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report the installed package version without requiring an operation."""
    from hephaestus.automation.fleet_worker_cli import main

    monkeypatch.setattr(sys, "argv", ["hephaestus-fleet-worker"])
    with pytest.raises(SystemExit) as exited:
        main([flag])

    assert exited.value.code == 0
    assert capsys.readouterr().out.startswith("hephaestus-fleet-worker ")


def test_worker_cli_accepts_the_shared_json_flag_for_a_data_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Accept the common JSON flag and emit the requested structured payload."""
    from hephaestus.automation import fleet_worker_cli

    expected = {"sessions": [], "activeReservations": 0}
    observed: list[tuple[Path, dict[str, Any]]] = []

    def fake_exchange(state_dir: Path, message: dict[str, Any]) -> dict[str, Any]:
        observed.append((state_dir, message))
        return expected

    monkeypatch.setattr(fleet_worker_cli, "exchange", fake_exchange)

    assert fleet_worker_cli.main(["--json", "inventory", "--state-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert observed == [(tmp_path, {"operation": "inventory", "after": 0})]


def test_private_request_evidence_dispatch_reads_the_matching_patch():
    """Use the evidence helper for a private JSON request."""
    worker = AttachedWorker()
    result = _dispatch(
        worker,
        {"operation": "request-evidence", "targetId": "session-1", "requestId": "approval-1"},
    )
    assert result.get("evidence") == {"changes": [worker.change]}
    assert result["requestId"] == "approval-1"
    assert result["sessionId"] == "session-1"
    assert worker.calls == [("thread/read", {"threadId": "thread-1", "includeTurns": True})]
    assert worker.control_messages == []


@pytest.mark.parametrize("mode", ["wrong-session", "stale-request"])
def test_private_request_evidence_refuses_inactive_bindings(mode):
    """Read no history when the private request does not belong to the current turn."""
    worker = AttachedWorker()
    session_id = "session-1"
    if mode == "wrong-session":
        session_id = "other-session"
    else:
        worker.pending["approval-1"]["params"]["turnId"] = "finished-turn"
    with pytest.raises(ValueError, match="request_not_active"):
        _dispatch(
            worker,
            {"operation": "request-evidence", "targetId": session_id, "requestId": "approval-1"},
        )
    assert worker.calls == []
    assert worker.control_messages == []
