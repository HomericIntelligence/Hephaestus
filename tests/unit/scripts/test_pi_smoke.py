"""Tests for scripts/pi_smoke.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.agents.runtime import AgentRunResult

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "pi_smoke.py"
_spec = importlib.util.spec_from_file_location("pi_smoke", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_model_alias_is_required_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The smoke harness must not bake model aliases into source."""
    monkeypatch.delenv("HEPH_PI_MODEL", raising=False)

    assert _mod.main([]) == 2


def test_runs_pi_with_env_model_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The smoke harness forwards only the operator-provided model alias."""
    monkeypatch.setenv("HEPH_PI_MODEL", "private-test-alias")
    run_pi = Mock(return_value=AgentRunResult(stdout="OK", stderr="", session_id="pi-smoke"))
    monkeypatch.setattr(_mod, "run_pi_session", run_pi)

    assert _mod.main(["--cwd", str(tmp_path), "--prompt", "Say OK"]) == 0

    kwargs = run_pi.call_args.kwargs
    assert kwargs["cwd"] == tmp_path
    assert kwargs["model"] == "private-test-alias"
    assert run_pi.call_args.args == ("Say OK",)
