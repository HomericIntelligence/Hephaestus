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


def _clear_plan_stage_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEPH_AGENT_PLAN_TIMEOUT", raising=False)
    monkeypatch.delenv("HEPH_PLAN_STAGE_TIMEOUT", raising=False)


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


def test_plan_stage_timeout_default_stays_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer plan stage keeps its historical long wrapper timeout."""
    _clear_plan_stage_timeout_env(monkeypatch)

    assert claude_timeouts.plan_stage_timeout() == TWO_HOURS_S


def test_plan_stage_timeout_ignores_removed_process_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEPH_AGENT_PLAN_TIMEOUT only controls planner agent calls, not the stage."""
    _clear_plan_stage_timeout_env(monkeypatch)
    monkeypatch.setenv("HEPH_AGENT_PLAN_TIMEOUT", "333")

    assert claude_timeouts.plan_stage_timeout() == TWO_HOURS_S
    assert claude_timeouts.planner_claude_timeout() == DEFAULT_THROUGHPUT_TIMEOUT_S


def test_plan_stage_timeout_ignores_removed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEPH_PLAN_STAGE_TIMEOUT tunes the outer plan-stage wrapper."""
    _clear_plan_stage_timeout_env(monkeypatch)
    monkeypatch.setenv("HEPH_PLAN_STAGE_TIMEOUT", "9000")

    assert claude_timeouts.plan_stage_timeout() == TWO_HOURS_S


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
            "HEPH_ADDRESS_REVIEW_AGENT_TIMEOUT",
            "HEPH_ADDRESS_REVIEW_CLAUDE_TIMEOUT",
            claude_timeouts.address_review_claude_timeout,
            TWO_HOURS_S,
        ),
        (
            "HEPH_CI_DRIVER_AGENT_TIMEOUT",
            "HEPH_CI_DRIVER_CLAUDE_TIMEOUT",
            claude_timeouts.ci_driver_claude_timeout,
            TWO_HOURS_S,
        ),
        (
            "HEPH_AGENT_LEARN_TIMEOUT",
            "HEPH_LEARN_CLAUDE_TIMEOUT",
            claude_timeouts.learn_claude_timeout,
            DEFAULT_THROUGHPUT_TIMEOUT_S,
        ),
        (
            "HEPH_FOLLOW_UP_AGENT_TIMEOUT",
            "HEPH_FOLLOW_UP_CLAUDE_TIMEOUT",
            claude_timeouts.follow_up_claude_timeout,
            TWO_HOURS_S,
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
            "HEPH_PLAN_STAGE_TIMEOUT",
            "HEPH_PLANNER_AGENT_TIMEOUT",
            claude_timeouts.plan_stage_timeout,
            TWO_HOURS_S,
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


def test_ci_poll_max_wait_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI poll max wait uses the documented throughput-friendly default when unset."""
    monkeypatch.delenv("HEPH_CI_POLL_MAX_WAIT", raising=False)

    assert claude_timeouts.ci_poll_max_wait() == DEFAULT_THROUGHPUT_TIMEOUT_S


def test_ci_poll_max_wait_ignores_removed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEPH_CI_POLL_MAX_WAIT overrides the default per call (re-read each time)."""
    monkeypatch.setenv("HEPH_CI_POLL_MAX_WAIT", "1800")

    assert claude_timeouts.ci_poll_max_wait() == DEFAULT_THROUGHPUT_TIMEOUT_S


def test_ci_poll_max_wait_invalid_env_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed HEPH_CI_POLL_MAX_WAIT warns and falls back to the default."""
    monkeypatch.setenv("HEPH_CI_POLL_MAX_WAIT", "soon")

    with caplog.at_level(logging.WARNING, logger="hephaestus.automation.claude_timeouts"):
        assert claude_timeouts.ci_poll_max_wait() == DEFAULT_THROUGHPUT_TIMEOUT_S

    assert not caplog.records
