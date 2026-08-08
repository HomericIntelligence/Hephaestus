"""Tests for scripts/pi_smoke_slurm.py."""

from __future__ import annotations

import importlib.util
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "pi_smoke_slurm.py"
_spec = importlib.util.spec_from_file_location("pi_smoke_slurm", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_submit_uses_export_names_without_alias_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Slurm submission must export alias env var names, never alias values."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o755)
    log_dir.chmod(0o755)
    run = Mock(
        return_value=subprocess.CompletedProcess(
            ["sbatch"],
            0,
            stdout="Submitted batch job 123\n",
            stderr="",
        )
    )
    monkeypatch.setattr(_mod.subprocess, "run", run)

    assert _mod.main(["--log-dir", str(log_dir), "--sbatch", "sbatch"]) == 0

    cmd = run.call_args.args[0]
    cmd_text = "\0".join(cmd)
    assert f"--export={','.join(_mod.EXPORT_NAMES)}" in cmd
    assert "ALL" not in _mod.EXPORT_NAMES
    output_arg = next(argument for argument in cmd if argument.startswith("--output="))
    private_run_dir = Path(output_arg.removeprefix("--output=")).parent
    error_arg = next(argument for argument in cmd if argument.startswith("--error="))
    assert Path(output_arg.removeprefix("--output=")).name == "pi-smoke-%j.out"
    assert Path(error_arg.removeprefix("--error=")).parent == private_run_dir
    assert Path(error_arg.removeprefix("--error=")).name == "pi-smoke-%j.err"
    assert private_run_dir.parent == log_dir
    assert private_run_dir.name.startswith("pi-smoke-")
    assert "private-provider-alias" not in cmd_text
    assert "private-model-alias" not in cmd_text
    assert "github-secret" not in cmd_text
    assert "aws-secret" not in cmd_text
    submission_env = run.call_args.kwargs["env"]
    assert submission_env["HEPH_PI_SMOKE_LOG_DIR"] == str(private_run_dir)
    assert "GH_TOKEN" not in submission_env
    assert "AWS_SECRET_ACCESS_KEY" not in submission_env
    assert "github-secret" not in submission_env.values()
    assert "aws-secret" not in submission_env.values()
    assert set(submission_env).issubset(_mod.EXPORT_NAMES)
    assert stat.S_IMODE(private_run_dir.stat().st_mode) & 0o077 == 0


def test_missing_alias_env_blocks_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Submission should fail before sbatch when required aliases are absent."""
    monkeypatch.delenv("HEPH_PI_PROVIDER", raising=False)
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    run = Mock()
    monkeypatch.setattr(_mod.subprocess, "run", run)

    assert _mod.main(["--log-dir", str(tmp_path)]) == 2

    run.assert_not_called()


def test_submit_fails_closed_without_user_only_log_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Slurm submission must not run when its scheduler logs cannot be protected."""
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    monkeypatch.setattr(_mod, "_private_smoke_log_permissions_supported", lambda: False)
    run = Mock()
    monkeypatch.setattr(_mod.subprocess, "run", run)

    assert _mod.main(["--log-dir", str(tmp_path / "logs")]) == 1

    run.assert_not_called()


def test_submit_redacts_sbatch_failure_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scheduler failures must use the same private denylist redaction boundary."""
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    (repository_root / ".heph-private-denylist").write_text(
        "PRIVATE_SCHEDULER_TOKEN\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_mod, "REPOSITORY_ROOT", repository_root, raising=False)
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    private_run_dir = tmp_path / "private-run"
    private_run_dir.mkdir(mode=0o700)
    monkeypatch.setattr(_mod, "_prepare_private_log_dir", lambda _log_dir: private_run_dir)
    monkeypatch.setattr(
        _mod.subprocess,
        "run",
        Mock(
            side_effect=subprocess.CalledProcessError(
                9,
                ["sbatch"],
                output="PRIVATE_SCHEDULER_TOKEN",
                stderr="private-provider-alias private-model-alias PRIVATE_SCHEDULER_TOKEN",
            )
        ),
    )

    assert _mod.main(["--log-dir", str(tmp_path / "logs")]) == 9

    diagnostics = capsys.readouterr().err
    assert "private-provider-alias" not in diagnostics
    assert "private-model-alias" not in diagnostics
    assert "PRIVATE_SCHEDULER_TOKEN" not in diagnostics


def test_default_template_has_a_minimal_export_and_no_scheduler_artifact() -> None:
    """Direct sbatch use must not inherit ambient credentials or shared logs."""
    template = _SCRIPT.parent / "slurm" / "pi_smoke.sbatch"
    text = template.read_text(encoding="utf-8")

    assert f"#SBATCH --export={','.join(_mod.EXPORT_NAMES)}" in text
    assert "#SBATCH --output=/dev/null" in text
    assert "#SBATCH --error=/dev/null" in text
