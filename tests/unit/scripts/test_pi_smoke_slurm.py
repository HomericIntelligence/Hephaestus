"""Tests for scripts/pi_smoke_slurm.py."""

from __future__ import annotations

import importlib.util
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
    run = Mock(
        return_value=subprocess.CompletedProcess(
            ["sbatch"],
            0,
            stdout="Submitted batch job 123\n",
            stderr="",
        )
    )
    monkeypatch.setattr(_mod.subprocess, "run", run)

    assert _mod.main(["--log-dir", str(tmp_path), "--sbatch", "sbatch"]) == 0

    cmd = run.call_args.args[0]
    cmd_text = "\0".join(cmd)
    assert "--export=ALL,HEPH_PI_PROVIDER,HEPH_PI_MODEL,HEPH_PI_SMOKE_LOG_DIR" in cmd
    assert f"--output={tmp_path / 'pi-smoke-%j.out'}" in cmd
    assert f"--error={tmp_path / 'pi-smoke-%j.err'}" in cmd
    assert "private-provider-alias" not in cmd_text
    assert "private-model-alias" not in cmd_text
    assert run.call_args.kwargs["env"]["HEPH_PI_SMOKE_LOG_DIR"] == str(tmp_path)
    assert tmp_path.is_dir()


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
