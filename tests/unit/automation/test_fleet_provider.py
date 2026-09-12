"""Check the Codex provider process boundary."""

from __future__ import annotations

import base64
import json
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
