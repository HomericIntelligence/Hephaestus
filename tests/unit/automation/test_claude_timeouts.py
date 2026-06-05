"""Tests for automation agent timeout configuration."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from hephaestus.automation import claude_timeouts


def _clear_planner_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEPH_PLANNER_AGENT_TIMEOUT", raising=False)
    monkeypatch.delenv("HEPH_PLANNER_CLAUDE_TIMEOUT", raising=False)


def test_planner_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planner timeout uses the documented default when unset."""
    _clear_planner_timeout_env(monkeypatch)

    assert claude_timeouts.planner_claude_timeout() == 600


@pytest.mark.parametrize(
    ("generic_env", "legacy_env", "timeout_fn", "default"),
    [
        (
            "HEPH_PLANNER_AGENT_TIMEOUT",
            "HEPH_PLANNER_CLAUDE_TIMEOUT",
            claude_timeouts.planner_claude_timeout,
            600,
        ),
        (
            "HEPH_PLAN_REVIEWER_AGENT_TIMEOUT",
            "HEPH_PLAN_REVIEWER_CLAUDE_TIMEOUT",
            claude_timeouts.plan_reviewer_claude_timeout,
            300,
        ),
        (
            "HEPH_IMPLEMENTER_AGENT_TIMEOUT",
            "HEPH_IMPLEMENTER_CLAUDE_TIMEOUT",
            claude_timeouts.implementer_claude_timeout,
            1800,
        ),
        (
            "HEPH_ADVISE_AGENT_TIMEOUT",
            "HEPH_ADVISE_CLAUDE_TIMEOUT",
            claude_timeouts.advise_claude_timeout,
            360,
        ),
        (
            "HEPH_PR_REVIEWER_AGENT_TIMEOUT",
            "HEPH_PR_REVIEWER_CLAUDE_TIMEOUT",
            claude_timeouts.pr_reviewer_claude_timeout,
            1200,
        ),
        (
            "HEPH_ADDRESS_REVIEW_AGENT_TIMEOUT",
            "HEPH_ADDRESS_REVIEW_CLAUDE_TIMEOUT",
            claude_timeouts.address_review_claude_timeout,
            1800,
        ),
        (
            "HEPH_CI_DRIVER_AGENT_TIMEOUT",
            "HEPH_CI_DRIVER_CLAUDE_TIMEOUT",
            claude_timeouts.ci_driver_claude_timeout,
            1800,
        ),
        (
            "HEPH_LEARN_AGENT_TIMEOUT",
            "HEPH_LEARN_CLAUDE_TIMEOUT",
            claude_timeouts.learn_claude_timeout,
            600,
        ),
        (
            "HEPH_FOLLOW_UP_AGENT_TIMEOUT",
            "HEPH_FOLLOW_UP_CLAUDE_TIMEOUT",
            claude_timeouts.follow_up_claude_timeout,
            600,
        ),
    ],
)
def test_legacy_claude_timeout_envs_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    generic_env: str,
    legacy_env: str,
    timeout_fn: Callable[[], int],
    default: int,
) -> None:
    """Legacy Claude-named timeout env vars are no longer supported."""
    monkeypatch.delenv(generic_env, raising=False)
    monkeypatch.setenv(legacy_env, "444")

    assert timeout_fn() == default


def test_planner_timeout_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic agent timeout env vars tune planner calls."""
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

    assert claude_timeouts.advise_claude_timeout() == 360


def test_advise_timeout_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex-friendly advise runs can be tuned with a generic env var."""
    monkeypatch.setenv("HEPH_ADVISE_AGENT_TIMEOUT", "600")

    assert claude_timeouts.advise_claude_timeout() == 600


def test_ci_poll_max_wait_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI poll max wait uses the documented 600s default when unset."""
    monkeypatch.delenv("HEPH_CI_POLL_MAX_WAIT", raising=False)

    assert claude_timeouts.ci_poll_max_wait() == 600


def test_ci_poll_max_wait_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEPH_CI_POLL_MAX_WAIT overrides the default per call (re-read each time)."""
    monkeypatch.setenv("HEPH_CI_POLL_MAX_WAIT", "1800")

    assert claude_timeouts.ci_poll_max_wait() == 1800


def test_ci_poll_max_wait_invalid_env_logs_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed HEPH_CI_POLL_MAX_WAIT warns and falls back to 600s."""
    monkeypatch.setenv("HEPH_CI_POLL_MAX_WAIT", "soon")

    with caplog.at_level(logging.WARNING, logger="hephaestus.automation.claude_timeouts"):
        assert claude_timeouts.ci_poll_max_wait() == 600

    assert any("HEPH_CI_POLL_MAX_WAIT" in record.message for record in caplog.records)
