"""Tests for scripts/pi_smoke.py."""

from __future__ import annotations

import importlib.util
import stat
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


def test_runs_pi_with_env_aliases_model_for_redaction_not_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The smoke harness passes the alias for redaction, never as process argv."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    run_pi = Mock(return_value=AgentRunResult(stdout="OK", stderr="", session_id="pi-smoke"))
    assert hasattr(_mod, "run_pi_smoke_session")
    monkeypatch.setattr(_mod, "run_pi_smoke_session", run_pi)

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
    assert kwargs["model"] == "private-model-alias"
    assert "provider" not in kwargs
    assert run_pi.call_args.args == ("Say OK",)
    captured = capsys.readouterr()
    assert captured.out.strip() == "OK"
    assert "SESSION_ID=" not in captured.err
    assert "LOG_FILE=" in captured.err
    log_file_line = next(line for line in captured.err.splitlines() if line.startswith("LOG_FILE="))
    log_path = Path(log_file_line.split("LOG_FILE=", 1)[1])
    log_text = log_path.read_text(encoding="utf-8")
    assert "stdout: OK" in log_text
    assert "pi-smoke" not in log_text
    assert "private-provider-alias" not in log_text
    assert "private-model-alias" not in log_text


def test_failure_output_redacts_private_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Smoke failures should redact the local alias and denylist tokens."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-test-alias")
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\nPRIVATE_SESSION_TOKEN\n",
        encoding="utf-8",
    )
    err = subprocess.CalledProcessError(
        9,
        ["pi"],
        output="PRIVATE_ENDPOINT_TOKEN",
        stderr="private-provider-alias private-test-alias PRIVATE_ENDPOINT_TOKEN",
    )
    monkeypatch.setattr(_mod, "run_pi_smoke_session", Mock(side_effect=err))

    assert _mod.main(["--cwd", str(tmp_path)]) == 9

    output = capsys.readouterr().err
    assert "private-provider-alias" not in output
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
        "PRIVATE_ENDPOINT_TOKEN\nPRIVATE_SESSION_TOKEN\n",
        encoding="utf-8",
    )
    run_pi = Mock(
        return_value=AgentRunResult(
            stdout="private-test-alias PRIVATE_ENDPOINT_TOKEN",
            stderr="",
            session_id="generated-opaque-session",
        )
    )
    monkeypatch.setattr(_mod, "run_pi_smoke_session", run_pi)

    assert _mod.main(["--cwd", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    output = captured.out
    assert "private-test-alias" not in output
    assert "PRIVATE_ENDPOINT_TOKEN" not in output
    assert "generated-opaque-session" not in captured.err
    assert "<redacted-pi-private-value>" in output
    assert "SESSION_ID=" not in captured.err
    log_file_line = next(line for line in captured.err.splitlines() if line.startswith("LOG_FILE="))
    log_path = Path(log_file_line.split("LOG_FILE=", 1)[1])
    log_text = log_path.read_text(encoding="utf-8")
    assert "private-provider-alias" not in log_text
    assert "private-test-alias" not in log_text
    assert "PRIVATE_ENDPOINT_TOKEN" not in log_text
    assert "generated-opaque-session" not in log_text
    assert "session_id:" not in log_text
    assert "<redacted-pi-private-value>" in log_text
    assert stat.S_IMODE(log_path.stat().st_mode) & 0o077 == 0


def test_sessionless_smoke_success_does_not_print_a_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``--no-session`` smoke path must remain successful without an ID."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    monkeypatch.setattr(
        _mod,
        "run_pi_smoke_session",
        Mock(return_value=AgentRunResult(stdout="OK", stderr="", session_id=None)),
    )

    assert _mod.main(["--cwd", str(tmp_path), "--log-dir", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "OK"
    assert "SESSION_ID=" not in captured.err


def test_repository_denylist_redacts_values_when_cwd_is_outside_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The checkout denylist protects smoke diagnostics for arbitrary working dirs."""
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    (repository_root / ".heph-private-denylist").write_text(
        "ROOT_PRIVATE_TOKEN\nprivate-log-directory\n",
        encoding="utf-8",
    )
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    log_dir = outside_cwd / "private-log-directory"
    monkeypatch.setattr(_mod, "REPOSITORY_ROOT", repository_root, raising=False)
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    monkeypatch.setattr(
        _mod,
        "run_pi_smoke_session",
        Mock(
            return_value=AgentRunResult(
                stdout="ROOT_PRIVATE_TOKEN",
                stderr="ROOT_PRIVATE_TOKEN",
                session_id="generated-opaque-session",
            )
        ),
    )

    assert _mod.main(["--cwd", str(outside_cwd), "--log-dir", str(log_dir)]) == 0

    captured = capsys.readouterr()
    diagnostics = f"{captured.out}\n{captured.err}"
    assert "ROOT_PRIVATE_TOKEN" not in diagnostics
    assert "private-log-directory" not in diagnostics
    log_paths = list(log_dir.glob("pi-smoke-local-*.log"))
    assert len(log_paths) == 1
    log_text = log_paths[0].read_text(encoding="utf-8")
    assert "ROOT_PRIVATE_TOKEN" not in log_text


def test_unreadable_repository_denylist_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Smoke must not run when its checkout privacy configuration cannot be read."""
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    denylist = repository_root / ".heph-private-denylist"
    denylist.write_text("ROOT_PRIVATE_TOKEN\n", encoding="utf-8")
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    monkeypatch.setattr(_mod, "REPOSITORY_ROOT", repository_root, raising=False)
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    original_read_text = Path.read_text

    def fail_denylist_read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == denylist:
            raise OSError("denylist unavailable")
        return original_read_text(
            path,
            encoding=encoding,
            errors=errors,
        )

    monkeypatch.setattr(Path, "read_text", fail_denylist_read)
    run_pi = Mock(return_value=AgentRunResult(stdout="OK", stderr=""))
    monkeypatch.setattr(_mod, "run_pi_smoke_session", run_pi)

    assert _mod.main(["--cwd", str(outside_cwd)]) == 1

    run_pi.assert_not_called()
    assert "unable to load Pi private denylist safely" in capsys.readouterr().err


def test_rejects_smoke_when_user_only_log_permissions_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The smoke seam must fail before execution if it cannot protect its log artifact."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-test-alias")
    run_pi = Mock(return_value=AgentRunResult(stdout="OK", stderr=""))
    monkeypatch.setattr(_mod, "run_pi_smoke_session", run_pi)
    monkeypatch.setattr(_mod, "_private_smoke_log_permissions_supported", lambda: False)

    assert _mod.main(["--cwd", str(tmp_path)]) == 1

    run_pi.assert_not_called()
    assert "user-only log permissions" in capsys.readouterr().err


def test_missing_pi_binary_is_a_sanitized_smoke_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing Pi executable must be a deterministic redacted CLI failure."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-test-alias")
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\n",
        encoding="utf-8",
    )
    missing_pi = FileNotFoundError(
        2,
        "PRIVATE_ENDPOINT_TOKEN private-provider-alias",
        "private-test-alias",
    )
    monkeypatch.setattr(_mod, "run_pi_smoke_session", Mock(side_effect=missing_pi))

    try:
        result = _mod.main(["--cwd", str(tmp_path)])
    except OSError:
        pytest.fail("Pi startup errors must be converted to a smoke CLI failure")

    assert result == 1
    output = capsys.readouterr().err
    assert "ERROR: Pi smoke could not start" in output
    assert "private-provider-alias" not in output
    assert "private-test-alias" not in output
    assert "PRIVATE_ENDPOINT_TOKEN" not in output
    assert "<redacted-pi-private-value>" in output


def test_reports_pi_runtime_contract_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pi JSON contract failures should produce an actionable smoke error."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-test-alias")
    run_pi = Mock(side_effect=RuntimeError("missing session id"))
    monkeypatch.setattr(_mod, "run_pi_smoke_session", run_pi)

    assert _mod.main(["--cwd", str(tmp_path)]) == 1
    assert "missing session id" in capsys.readouterr().err
