"""Tests for the ``hephaestus-gh`` wrapper."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

from hephaestus.github.gh_cli import main


def _completed(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


@patch("hephaestus.github.gh_cli.configure_github_throttle_from_args")
@patch("hephaestus.github.gh_cli.gh_call")
def test_json_success_emits_single_envelope(
    mock_gh_call: MagicMock,
    _mock_configure: MagicMock,
    capsys,
) -> None:
    """``--json`` wraps gh stdout once instead of appending a second JSON doc."""
    mock_gh_call.return_value = _completed(stdout='{"data": true}\n')

    assert main(["--json", "api", "rate_limit"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "ok",
        "exit_code": 0,
        "stdout": '{"data": true}\n',
        "stderr": "",
    }


@patch("hephaestus.github.gh_cli.configure_github_throttle_from_args")
@patch("hephaestus.github.gh_cli.gh_call")
def test_json_called_process_error_emits_envelope(
    mock_gh_call: MagicMock, _mock_configure: MagicMock, capsys
) -> None:
    """Nonzero gh exits keep stdout/stderr inside one JSON envelope."""
    mock_gh_call.side_effect = subprocess.CalledProcessError(
        2,
        ["gh"],
        output="partial",
        stderr="denied",
    )

    assert main(["--json", "api", "repos/o/r"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert payload["stdout"] == "partial"
    assert payload["stderr"] == "denied"


@patch("hephaestus.github.gh_cli.configure_github_throttle_from_args")
@patch("hephaestus.github.gh_cli.gh_call")
def test_runtime_error_returns_stable_nonzero(
    mock_gh_call: MagicMock, _mock_configure: MagicMock, capsys
) -> None:
    """Adapter runtime failures do not escape as Python tracebacks."""
    mock_gh_call.side_effect = RuntimeError("circuit breaker open")

    assert main(["--json", "api", "rate_limit"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["message"] == "circuit breaker open"
    assert payload["stderr"] == "circuit breaker open"
