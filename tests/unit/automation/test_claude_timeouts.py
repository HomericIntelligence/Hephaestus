"""Tests for automation agent timeout configuration."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from hephaestus.automation import claude_timeouts

TWO_HOURS_S = 7200
DEFAULT_THROUGHPUT_TIMEOUT_S = 1200


def _clear_planner_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEPH_AGENT_PLAN_TIMEOUT", raising=False)
    monkeypatch.delenv("HEPH_PLANNER_CLAUDE_TIMEOUT", raising=False)


def test_planner_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planner timeout uses the documented default when unset."""
    _clear_planner_timeout_env(monkeypatch)

    assert claude_timeouts.planner_claude_timeout() == DEFAULT_THROUGHPUT_TIMEOUT_S


def test_agent_default_timeout_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared invoke fallback is generic (7200s), not the planner budget (#1415)."""
    monkeypatch.delenv("HEPH_AGENT_DEFAULT_TIMEOUT", raising=False)

    result = claude_timeouts.agent_default_timeout()

    assert result == TWO_HOURS_S
    # Must NOT collapse onto any phase-specific budget.
    assert result != DEFAULT_THROUGHPUT_TIMEOUT_S


def test_agent_default_timeout_ignores_removed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removed process configuration cannot tune the shared invoke fallback."""
    monkeypatch.setenv("HEPH_AGENT_DEFAULT_TIMEOUT", "4321")

    assert claude_timeouts.agent_default_timeout() == TWO_HOURS_S


@pytest.mark.parametrize(
    ("primary_env", "claude_env", "timeout_fn", "default"),
    [
        (
            "HEPH_AGENT_PLAN_TIMEOUT",
            "HEPH_PLANNER_CLAUDE_TIMEOUT",
            claude_timeouts.planner_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
        (
            "HEPH_AGENT_REVIEW_TIMEOUT",
            "HEPH_PLAN_REVIEWER_CLAUDE_TIMEOUT",
            claude_timeouts.plan_reviewer_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
        (
            "HEPH_AGENT_IMPL_TIMEOUT",
            "HEPH_IMPLEMENTER_CLAUDE_TIMEOUT",
            claude_timeouts.implementer_claude_timeout,
            1800,
        ),
        (
            "HEPH_ADVISE_AGENT_TIMEOUT",
            "HEPH_ADVISE_CLAUDE_TIMEOUT",
            claude_timeouts.advise_claude_timeout,
            TWO_HOURS_S,
        ),
        (
            "HEPH_AGENT_REVIEW_TIMEOUT",
            "HEPH_PR_REVIEWER_CLAUDE_TIMEOUT",
            claude_timeouts.pr_reviewer_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
        (
            "HEPH_AGENT_LEARN_TIMEOUT",
            "HEPH_LEARN_CLAUDE_TIMEOUT",
            claude_timeouts.learn_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
        (
            "HEPH_GIT_MESSAGE_AGENT_TIMEOUT",
            "HEPH_GIT_MESSAGE_CLAUDE_TIMEOUT",
            claude_timeouts.git_message_agent_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
    ],
)
def test_legacy_claude_timeout_envs_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    primary_env: str,
    claude_env: str,
    timeout_fn: Callable[[], int],
    default: int,
) -> None:
    """Legacy Claude-named timeout env vars are no longer supported."""
    monkeypatch.delenv(primary_env, raising=False)
    monkeypatch.setenv(claude_env, "444")

    assert timeout_fn() == default


@pytest.mark.parametrize(
    ("primary_env", "timeout_fn"),
    [
        ("HEPH_AGENT_PLAN_TIMEOUT", claude_timeouts.planner_claude_timeout),
        ("HEPH_AGENT_REVIEW_TIMEOUT", claude_timeouts.plan_reviewer_claude_timeout),
        ("HEPH_AGENT_IMPL_TIMEOUT", claude_timeouts.implementer_claude_timeout),
        ("HEPH_AGENT_REVIEW_TIMEOUT", claude_timeouts.pr_reviewer_claude_timeout),
        ("HEPH_AGENT_LEARN_TIMEOUT", claude_timeouts.learn_claude_timeout),
    ],
)
def test_agent_timeout_envs_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    primary_env: str,
    timeout_fn: Callable[[], int],
) -> None:
    """New generic agent timeout env vars are read on every function call."""
    monkeypatch.setenv(primary_env, "333")
    expected = 1800 if primary_env == "HEPH_AGENT_IMPL_TIMEOUT" else 1200
    assert timeout_fn() == expected

    monkeypatch.setenv(primary_env, "444")
    assert timeout_fn() == expected


@pytest.mark.parametrize(
    ("canonical_env", "deprecated_env", "timeout_fn", "default"),
    [
        (
            "HEPH_AGENT_PLAN_TIMEOUT",
            "HEPH_PLANNER_AGENT_TIMEOUT",
            claude_timeouts.planner_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
        (
            "HEPH_AGENT_REVIEW_TIMEOUT",
            "HEPH_PLAN_REVIEWER_AGENT_TIMEOUT",
            claude_timeouts.plan_reviewer_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
        (
            "HEPH_AGENT_IMPL_TIMEOUT",
            "HEPH_IMPLEMENTER_AGENT_TIMEOUT",
            claude_timeouts.implementer_claude_timeout,
            1800,
        ),
        (
            "HEPH_AGENT_REVIEW_TIMEOUT",
            "HEPH_PR_REVIEWER_AGENT_TIMEOUT",
            claude_timeouts.pr_reviewer_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
        (
            "HEPH_AGENT_LEARN_TIMEOUT",
            "HEPH_LEARN_AGENT_TIMEOUT",
            claude_timeouts.learn_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
    ],
)
def test_deprecated_agent_timeout_aliases_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    canonical_env: str,
    deprecated_env: str,
    timeout_fn: Callable[[], int],
    default: int,
) -> None:
    """Deprecated phase-specific timeout aliases do not affect configuration."""
    monkeypatch.delenv(canonical_env, raising=False)
    monkeypatch.setenv(deprecated_env, "555")

    assert timeout_fn() == default


def test_planner_timeout_invalid_agent_env_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed timeout values warn and fall back to the default."""
    _clear_planner_timeout_env(monkeypatch)
    monkeypatch.setenv("HEPH_AGENT_PLAN_TIMEOUT", "slow")

    with caplog.at_level(logging.WARNING, logger="hephaestus.constants"):
        assert claude_timeouts.planner_claude_timeout() == DEFAULT_THROUGHPUT_TIMEOUT_S

    assert not caplog.records


def test_advise_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advise timeout uses the same 2h default as other agent calls."""
    monkeypatch.delenv("HEPH_ADVISE_AGENT_TIMEOUT", raising=False)

    assert claude_timeouts.advise_claude_timeout() == TWO_HOURS_S


def test_advise_timeout_ignores_removed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removed timeout environment variables cannot affect defaults."""
    monkeypatch.setenv("HEPH_ADVISE_AGENT_TIMEOUT", "600")

    assert claude_timeouts.advise_claude_timeout() == TWO_HOURS_S


def test_git_message_timeout_ignores_removed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lightweight git-message agent uses its own short tunable timeout."""
    monkeypatch.setenv("HEPH_GIT_MESSAGE_AGENT_TIMEOUT", "90")

    assert claude_timeouts.git_message_agent_timeout() == DEFAULT_THROUGHPUT_TIMEOUT_S
