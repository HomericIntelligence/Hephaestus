"""Subprocess timeouts per automation phase.

Each phase that shells out to an agent CLI or ``gh`` has historically
hard-coded its own timeout. Centralising them here parallels
:mod:`claude_models` and gives operators a way to tune slow repos / network
conditions without code changes via ``HEPH_<PHASE>_AGENT_TIMEOUT`` environment
variables (values in seconds).

If an env var is set but not an integer, the default is used and a warning is
logged on first read; we never crash on a malformed timeout because the cost
of a runtime startup error is higher than the cost of falling back.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _read_int_env(name: str, default: int) -> int:
    """Return ``int(os.environ[name])`` or ``default`` if unset/invalid."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r — using default %ds", name, raw, default)
        return default


def planner_claude_timeout() -> int:
    """Timeout for agent calls inside the planner (default 7200s)."""
    return _read_int_env("HEPH_PLANNER_AGENT_TIMEOUT", 7200)


def plan_reviewer_claude_timeout() -> int:
    """Timeout for agent calls inside the plan reviewer (default 7200s)."""
    return _read_int_env("HEPH_PLAN_REVIEWER_AGENT_TIMEOUT", 7200)


def implementer_claude_timeout() -> int:
    """Timeout for the implementer's agent invocation (default 7200s)."""
    return _read_int_env("HEPH_IMPLEMENTER_AGENT_TIMEOUT", 7200)


def advise_claude_timeout() -> int:
    """Timeout for advise agent calls (default 7200s)."""
    return _read_int_env("HEPH_ADVISE_AGENT_TIMEOUT", 7200)


def pr_reviewer_claude_timeout() -> int:
    """Timeout for the PR reviewer's agent analysis (default 7200s)."""
    return _read_int_env("HEPH_PR_REVIEWER_AGENT_TIMEOUT", 7200)


def address_review_claude_timeout() -> int:
    """Timeout for the address-review fix session (default 7200s)."""
    return _read_int_env("HEPH_ADDRESS_REVIEW_AGENT_TIMEOUT", 7200)


def ci_driver_claude_timeout() -> int:
    """Timeout for the CI-driver fix session (default 7200s)."""
    return _read_int_env("HEPH_CI_DRIVER_AGENT_TIMEOUT", 7200)


def learn_claude_timeout() -> int:
    """Timeout for ``/learn`` agent calls (default 7200s)."""
    return _read_int_env("HEPH_LEARN_AGENT_TIMEOUT", 7200)


def follow_up_claude_timeout() -> int:
    """Timeout for the follow-up-issue agent session (default 7200s)."""
    return _read_int_env("HEPH_FOLLOW_UP_AGENT_TIMEOUT", 7200)


# Re-exported from hephaestus.github.client so the gh-adapter timeout lives
# with the gh adapter; this alias preserves the legacy import path.
from hephaestus.github.client import gh_cli_timeout  # noqa: E402

__all__ = [
    "address_review_claude_timeout",
    "advise_claude_timeout",
    "ci_driver_claude_timeout",
    "ci_poll_max_wait",
    "follow_up_claude_timeout",
    "gh_cli_timeout",
    "implementer_claude_timeout",
    "learn_claude_timeout",
    "plan_reviewer_claude_timeout",
    "planner_claude_timeout",
    "pr_reviewer_claude_timeout",
]


def ci_poll_max_wait() -> int:
    """Wall-clock seconds for the CI-driver poll loops (default 600s).

    Bounds the exponential-backoff wait in :mod:`ci_driver` while CI checks
    are still pending. Re-read on each invocation so tests and operators can
    tune it at runtime via ``HEPH_CI_POLL_MAX_WAIT``.
    """
    return _read_int_env("HEPH_CI_POLL_MAX_WAIT", 600)
