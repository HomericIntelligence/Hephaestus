"""Tests for scripts/pi_smoke.py."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.agents.runtime import AgentRunResult

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "pi_smoke.py"
_spec = importlib.util.spec_from_file_location("pi_smoke", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_provider_and_model_aliases_are_required_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The smoke harness must require aliases from operator-local env vars."""
    monkeypatch.delenv("HEPH_PI_PROVIDER", raising=False)
    monkeypatch.delenv("HEPH_PI_MODEL", raising=False)

    assert _mod.main([]) == 2


def test_runs_pi_with_env_aliases_model_via_kwarg_not_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The smoke harness passes model kwarg to propagate via environment."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    run_pi = Mock(return_value=AgentRunResult(stdout="OK", stderr="", session_id="pi-smoke"))
    monkeypatch.setattr(_mod, "run_pi_session", run_pi)

    assert (
        _mod.main(
            [
                "--cwd",
                str(tmp_path),
                "--prompt",
                "Say OK",
                "--log-dir",
                str(tmp_path),
            ]
        )
        == 0
    )

    kwargs = run_pi.call_args.kwargs
    assert kwargs["cwd"] == tmp_path
    assert kwargs["sandbox"] == "read-only"
    assert kwargs["model"] == "private-model-alias"
    assert "provider" not in kwargs
    assert run_pi.call_args.args == ("Say OK",)
    captured = capsys.readouterr()
    assert captured.out.strip() == "OK"
    assert "LOG_FILE=" in captured.err
    log_file_line = next(l for l in captured.err.splitlines() if l.startswith("LOG_FILE="))
    log_path = Path(log_file_line.split("LOG_FILE=", 1)[1])
    log_text = log_path.read_text(encoding="utf-8")
    assert "stdout: OK" in log_text
    assert "private-provider-alias" not in log_text
    assert "private-model-alias" not in log_text


def test_failure_output_redacts_private_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Smoke failures should redact the local alias and denylist tokens."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-test-alias")
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\n",
        encoding="utf-8",
    )
    err = subprocess.CalledProcessError(
        9,
        ["pi"],
        output="PRIVATE_ENDPOINT_TOKEN",
        stderr="private-test-alias PRIVATE_ENDPOINT_TOKEN",
    )
    monkeypatch.setattr(_mod, "run_pi_session", Mock(side_effect=err))

    assert _mod.main(["--cwd", str(tmp_path)]) == 9

    output = capsys.readouterr().err
    assert "private-test-alias" not in output
    assert "PRIVATE_ENDPOINT_TOKEN" not in output
    assert "<redacted-pi-private-value>" in output


def test_success_output_redacts_private_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Smoke success output should also be safe for publication."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-test-alias")
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\n",
        encoding="utf-8",
    )
    run_pi = Mock(
        return_value=AgentRunResult(
            stdout="private-test-alias PRIVATE_ENDPOINT_TOKEN",
            stderr="",
            session_id=None,
        )
    )
    monkeypatch.setattr(_mod, "run_pi_session", run_pi)

    assert _mod.main(["--cwd", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "private-test-alias" not in output
    assert "PRIVATE_ENDPOINT_TOKEN" not in output
    assert "<redacted-pi-private-value>" in output


def test_reports_pi_runtime_contract_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pi JSON contract failures should produce an actionable smoke error."""
    monkeypatch.setenv("HEPH_PI_MODEL", "private-test-alias")
    run_pi = Mock(side_effect=RuntimeError("missing session id"))
    monkeypatch.setattr(_mod, "run_pi_session", run_pi)

    assert _mod.main(["--cwd", str(tmp_path)]) == 1
    assert "missing session id" in capsys.readouterr().err
