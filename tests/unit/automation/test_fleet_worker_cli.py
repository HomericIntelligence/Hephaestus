"""Route private evidence reads without dispatching an execution command."""

from __future__ import annotations

import inspect
import json
import socket
import sys
import tempfile
import threading
import time
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


def test_private_exchange_uses_the_callers_finite_deadline():
    """A connected but silent private worker cannot outlive the caller budget."""
    from hephaestus.automation.fleet_worker_cli import exchange

    assert "timeout" in inspect.signature(exchange).parameters, (
        "the existing private attachment must accept the caller's remaining timeout"
    )
    connected = threading.Event()
    finish = threading.Event()
    failures = []
    with tempfile.TemporaryDirectory(
        prefix="fleet-socket-", dir=Path("/tmp").resolve()
    ) as temporary:
        directory = Path(temporary)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(directory / "worker.sock"))
            listener.listen(1)
            listener.settimeout(2)

            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connected.set()
                        finish.wait(2)
                except Exception as error:
                    failures.append(error)

            server = threading.Thread(target=serve)
            server.start()
            started = time.monotonic()
            try:
                with pytest.raises(TimeoutError):
                    exchange(directory, {"operation": "inventory"}, timeout=0.05)
                assert connected.is_set()
                assert time.monotonic() - started < 1
            finally:
                finish.set()
                server.join(timeout=3)
            assert not server.is_alive()
            assert failures == []


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_private_exchange_rejects_unbounded_timeout_before_connect(tmp_path, timeout):
    """Invalid deadlines cannot become an unbounded attachment operation."""
    from hephaestus.automation.fleet_worker_cli import exchange

    assert "timeout" in inspect.signature(exchange).parameters, (
        "the existing private attachment must accept the caller's remaining timeout"
    )
    with pytest.raises(ValueError, match="timeout"):
        exchange(tmp_path, {"operation": "inventory"}, timeout=timeout)
