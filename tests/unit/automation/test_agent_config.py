"""#1441: agent_config consolidates models+timeouts+naming; shims re-export it."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from hephaestus.automation import agent_config


def test_agent_config_exposes_all_three_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merged module answers model, timeout, and naming queries."""
    monkeypatch.delenv("HEPH_PLANNER_MODEL", raising=False)
    assert agent_config.planner_model() == agent_config.OPUS  # models
    assert agent_config.implementer_claude_timeout() == agent_config.AGENT_IMPL_TIMEOUT  # timeouts
    assert agent_config.session_name("R", 1, agent_config.AGENT_PLANNER)  # naming


def test_canonical_jsonl_path_is_dot_safe() -> None:
    """Dot-prefixed cwd segments are encoded (guards #822)."""
    p = agent_config.session_jsonl_path("u", Path("/a/.worktrees/b"))
    assert "-worktrees-" in str(p)


# Parity over EVERY public symbol of each shim — a missing re-export is only an
# AttributeError at the call site, so assert the full surface here.
@pytest.mark.parametrize("shim", ["claude_models", "claude_timeouts", "session_naming"])
def test_shim_reexports_every_public_symbol_identically(shim: str) -> None:
    """Each shim re-exports the exact same object agent_config defines."""
    mod = importlib.import_module(f"hephaestus.automation.{shim}")
    public = [
        n
        for n in dir(mod)
        if not n.startswith("_") and not isinstance(getattr(mod, n), type(importlib))
    ]
    for sym in public:
        assert getattr(mod, sym) is getattr(agent_config, sym), f"{shim}.{sym} drifted"
