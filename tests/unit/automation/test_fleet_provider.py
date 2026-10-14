"""Check the Codex provider process boundary."""

from __future__ import annotations

import base64
import json
import queue
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_provider import CodexAppServer, ProviderError

pytestmark = pytest.mark.precommit


def _overflow_executable(tmp_path: Path, descriptor: int = 1) -> tuple[Path, Path]:
    """Create a version command that continues only if its output is not bounded."""
    executable = tmp_path / "codex"
    sentinel = tmp_path / "continued-after-overflow"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, time\n"
        f"os.write({descriptor}, b'x' * (1024 * 1024 + 1))\n"
        "time.sleep(3)\n"
        f"pathlib.Path({str(sentinel)!r}).write_text('continued')\n"
    )
    executable.chmod(0o700)
    return executable, sentinel


@pytest.mark.parametrize("descriptor", [1, 2], ids=["stdout", "stderr"])
def test_provider_start_stops_excess_version_output(tmp_path: Path, descriptor: int) -> None:
    """Stop version discovery before excess output can consume host memory."""
    executable, sentinel = _overflow_executable(tmp_path, descriptor)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    provider = CodexAppServer([str(executable)], codex_home)

    started = time.monotonic()
    try:
        with pytest.raises(ProviderError, match="provider_version_response_limit"):
            provider.start()
    finally:
        provider.close()

    assert time.monotonic() - started < 2
    assert not sentinel.exists()


def test_lifecycle_queue_has_an_aggregate_byte_limit_and_recovers_after_drain(
    tmp_path: Path,
) -> None:
    """Several valid frames cannot exceed the lifecycle memory budget."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    provider = CodexAppServer(["codex"], codex_home)
    payload = "x" * (1024 * 1024 - 4096)

    for index in range(4):
        provider._enqueue(
            {
                "method": "turn/started",
                "params": {"threadId": "thread-1", "index": index, "payload": payload},
            }
        )
    with pytest.raises(queue.Full):
        provider._enqueue(
            {
                "method": "turn/started",
                "params": {"threadId": "thread-1", "index": 4, "payload": payload},
            }
        )

    assert len(provider.drain_notifications()) == 4
    provider._enqueue({"method": "turn/started", "params": {"threadId": "thread-1", "index": 5}})
    assert len(provider.drain_notifications()) == 1


def test_live_observations_have_an_aggregate_byte_limit_and_recover_after_drain(
    tmp_path: Path,
) -> None:
    """Coalesced observations cannot retain several gigabytes of identities."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    provider = CodexAppServer(["codex"], codex_home)
    accepted = 0

    for index in range(4096):
        try:
            provider._enqueue(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": f"thread-{index}-" + ("x" * 1000),
                        "turnId": "turn-" + ("y" * 1000),
                    },
                }
            )
        except queue.Full:
            break
        accepted += 1

    assert 0 < accepted < 4096
    assert len(provider.drain_notifications()) == accepted
    provider._enqueue(
        {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "thread-recovered", "turnId": "turn-recovered"},
        }
    )
    assert len(provider.drain_notifications()) == 1


def test_live_observation_identity_has_a_utf8_byte_limit(tmp_path: Path) -> None:
    """A multibyte identity cannot bypass the observation field limit."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    provider = CodexAppServer(["codex"], codex_home)

    with pytest.raises(ValueError, match="invalid provider identity"):
        provider._enqueue(
            {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": "é" * 513, "turnId": "turn-1"},
            }
        )


@pytest.mark.parametrize(
    ("method", "result"),
    [
        ("thread/start", {}),
        ("thread/start", {"thread": None}),
        ("thread/start", {"thread": {"id": ""}}),
        ("thread/resume", {"thread": {"id": 1}}),
        ("thread/resume", {"thread": {"id": "x" * 1025}}),
        ("turn/start", {"turn": {}}),
        ("turn/start", {"turn": {"id": None}}),
    ],
)
def test_request_rejects_invalid_method_specific_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    result: dict[str, Any],
) -> None:
    """Reject a malformed result before a worker can use its identity."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    provider = CodexAppServer(["codex"], codex_home)

    def complete(message: dict[str, Any], *, deadline: float | None = None) -> None:
        del deadline
        provider._pending[message["id"]].set_result({"id": message["id"], "result": result})

    monkeypatch.setattr(provider, "_send", complete)

    with pytest.raises(ProviderError, match="provider_invalid_result"):
        provider.request(method, {})


class _ProbeInput:
    """Accept the probe's initialized notification."""

    def write(self, data: bytes) -> int:
        return len(data)


class _SuccessfulProbeConnection:
    """Reach version discovery without starting a real exec server."""

    def __init__(self, _executable: Path, _home: Path) -> None:
        self.stdin = _ProbeInput()
        self.diagnostics = ""

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"sessionId": "synthetic-session"}
        if method == "fs/readFile":
            path = Path(params["path"].removeprefix("file://"))
            if path.name != "synthetic-marker":
                raise ValueError("forbidden")
            return {"dataBase64": base64.b64encode(path.read_bytes()).decode()}
        if method == "process/start":
            return {"sandboxType": "synthetic"}
        if method == "process/read":
            return {"exited": True, "exitCode": 0, "nextSeq": None}
        if method == "process/terminate":
            return {}
        raise AssertionError(f"unexpected probe method: {method}")

    def close(self) -> bool:
        return True


def test_exec_server_probe_stops_excess_version_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep the opt-in probe bounded when its selected executable is malformed."""
    from tests.integration import fleet_exec_server_probe

    executable, sentinel = _overflow_executable(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    forbidden = tmp_path / "forbidden"
    monkeypatch.setattr(fleet_exec_server_probe, "ProbeConnection", _SuccessfulProbeConnection)
    monkeypatch.setattr(fleet_exec_server_probe, "owned_processes", lambda _nonce: [(123, "456")])
    monkeypatch.setattr(fleet_exec_server_probe, "clean_owned_processes", lambda _nonce: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fleet_exec_server_probe.py",
            "--codex-bin",
            str(executable),
            "--workspace",
            str(workspace),
            "--forbidden",
            str(forbidden),
        ],
    )

    started = time.monotonic()
    assert fleet_exec_server_probe.main() == 1

    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "provider_version_response_limit"
    assert result["authorizesAdmission"] is False
    assert time.monotonic() - started < 2
    assert not sentinel.exists()
