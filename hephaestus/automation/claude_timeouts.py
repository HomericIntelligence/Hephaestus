"""Subprocess timeouts per automation phase.

Each phase that shells out to an agent CLI or ``gh`` has historically
hard-coded its own timeout. Centralising them here parallels
:mod:`claude_models` and gives operators a way to tune slow repos / network
conditions without code changes via ``HEPH_<PHASE>_AGENT_TIMEOUT`` environment
variables (values in seconds). Legacy ``HEPH_<PHASE>_CLAUDE_TIMEOUT`` names are
still accepted for compatibility.

If an env var is set but not an integer, the default is used and a warning is
logged on first read; we never crash on a malformed timeout because the cost
of a runtime startup error is higher than the cost of falling back.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _read_int_env(name: str, default: int, *, legacy_name: str | None = None) -> int:
    """Return ``int(os.environ[name])`` or ``default`` if unset/invalid."""
    raw = os.environ.get(name)
    source = name
    if raw is None and legacy_name is not None:
        raw = os.environ.get(legacy_name)
        source = legacy_name
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r — using default %ds", source, raw, default)
        return default


def planner_claude_timeout() -> int:
    """Timeout for agent calls inside the planner (default 600s)."""
    return _read_int_env(
        "HEPH_PLANNER_AGENT_TIMEOUT",
        600,
        legacy_name="HEPH_PLANNER_CLAUDE_TIMEOUT",
    )


def plan_reviewer_claude_timeout() -> int:
    """Timeout for agent calls inside the plan reviewer (default 300s)."""
    return _read_int_env(
        "HEPH_PLAN_REVIEWER_AGENT_TIMEOUT",
        300,
        legacy_name="HEPH_PLAN_REVIEWER_CLAUDE_TIMEOUT",
    )


def implementer_claude_timeout() -> int:
    """Timeout for the implementer's agent invocation (default 1800s)."""
    return _read_int_env(
        "HEPH_IMPLEMENTER_AGENT_TIMEOUT",
        1800,
        legacy_name="HEPH_IMPLEMENTER_CLAUDE_TIMEOUT",
    )


def advise_claude_timeout() -> int:
    """Timeout for lightweight advise agent calls (default 360s)."""
    return _read_int_env(
        "HEPH_ADVISE_AGENT_TIMEOUT",
        360,
        legacy_name="HEPH_ADVISE_CLAUDE_TIMEOUT",
    )


def pr_reviewer_claude_timeout() -> int:
    """Timeout for the PR reviewer's agent analysis (default 1200s)."""
    return _read_int_env(
        "HEPH_PR_REVIEWER_AGENT_TIMEOUT",
        1200,
        legacy_name="HEPH_PR_REVIEWER_CLAUDE_TIMEOUT",
    )


def address_review_claude_timeout() -> int:
    """Timeout for the address-review fix session (default 1800s)."""
    return _read_int_env(
        "HEPH_ADDRESS_REVIEW_AGENT_TIMEOUT",
        1800,
        legacy_name="HEPH_ADDRESS_REVIEW_CLAUDE_TIMEOUT",
    )


def ci_driver_claude_timeout() -> int:
    """Timeout for the CI-driver fix session (default 1800s)."""
    return _read_int_env(
        "HEPH_CI_DRIVER_AGENT_TIMEOUT",
        1800,
        legacy_name="HEPH_CI_DRIVER_CLAUDE_TIMEOUT",
    )


def learn_claude_timeout() -> int:
    """Timeout for the post-impl ``/learn`` agent call (default 600s)."""
    return _read_int_env(
        "HEPH_LEARN_AGENT_TIMEOUT",
        600,
        legacy_name="HEPH_LEARN_CLAUDE_TIMEOUT",
    )


def follow_up_claude_timeout() -> int:
    """Timeout for the follow-up-issue agent session (default 600s)."""
    return _read_int_env(
        "HEPH_FOLLOW_UP_AGENT_TIMEOUT",
        600,
        legacy_name="HEPH_FOLLOW_UP_CLAUDE_TIMEOUT",
    )


def gh_cli_timeout() -> int:
    """Timeout for individual ``gh`` CLI calls in :mod:`github_api` (default 120s)."""
    return _read_int_env("HEPH_GH_TIMEOUT", 120)
