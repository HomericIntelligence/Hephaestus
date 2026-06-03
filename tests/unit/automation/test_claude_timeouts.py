"""Tests for automation agent timeout configuration."""

from __future__ import annotations

import logging

import pytest

from hephaestus.automation import claude_timeouts


def _clear_planner_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEPH_PLANNER_AGENT_TIMEOUT", raising=False)
    monkeypatch.delenv("HEPH_PLANNER_CLAUDE_TIMEOUT", raising=False)


def test_planner_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planner timeout uses the documented default when unset."""
    _clear_planner_timeout_env(monkeypatch)

    assert claude_timeouts.planner_claude_timeout() == 600


def test_planner_timeout_legacy_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy Claude-named timeout env vars remain supported."""
    _clear_planner_timeout_env(monkeypatch)
    monkeypatch.setenv("HEPH_PLANNER_CLAUDE_TIMEOUT", "444")

    assert claude_timeouts.planner_claude_timeout() == 444


def test_planner_timeout_agent_env_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic agent timeout env vars override legacy names."""
    _clear_planner_timeout_env(monkeypatch)
    monkeypatch.setenv("HEPH_PLANNER_CLAUDE_TIMEOUT", "444")
    monkeypatch.setenv("HEPH_PLANNER_AGENT_TIMEOUT", "555")

    assert claude_timeouts.planner_claude_timeout() == 555


def test_planner_timeout_invalid_agent_env_logs_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed timeout values warn and fall back to the default."""
    _clear_planner_timeout_env(monkeypatch)
    monkeypatch.setenv("HEPH_PLANNER_AGENT_TIMEOUT", "slow")

    with caplog.at_level(logging.WARNING, logger="hephaestus.automation.claude_timeouts"):
        assert claude_timeouts.planner_claude_timeout() == 600

    assert any("HEPH_PLANNER_AGENT_TIMEOUT" in record.message for record in caplog.records)


def test_advise_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advise timeout keeps its short default unless explicitly tuned."""
    monkeypatch.delenv("HEPH_ADVISE_AGENT_TIMEOUT", raising=False)
    monkeypatch.delenv("HEPH_ADVISE_CLAUDE_TIMEOUT", raising=False)

    assert claude_timeouts.advise_claude_timeout() == 360


def test_advise_timeout_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex-friendly advise runs can be tuned with a generic env var."""
    monkeypatch.setenv("HEPH_ADVISE_AGENT_TIMEOUT", "600")

    assert claude_timeouts.advise_claude_timeout() == 600
