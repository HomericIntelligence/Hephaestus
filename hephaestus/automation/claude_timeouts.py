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

from hephaestus.constants import (
    AGENT_IMPL_TIMEOUT,
    AGENT_LEARN_TIMEOUT,
    AGENT_PLAN_TIMEOUT,
    AGENT_REVIEW_TIMEOUT,
    read_timeout_env,
)

logger = logging.getLogger(__name__)

PLAN_STAGE_TIMEOUT = 7200

# Defaults for the explicit CLI timeout options (#1657). The non-phase-
# differentiated agent phases (advise, address-review, ci-driver, follow-up)
# and the options-object fallbacks default to DEFAULT_AGENT_TIMEOUT; per-phase
# timeouts keep their #1642 values via the AGENT_* constants in
# ``hephaestus.constants``.
DEFAULT_AGENT_TIMEOUT: int = 7200
DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT: int = 300
DEFAULT_CI_POLL_MAX_WAIT: int = 600


def _read_int_env(name: str, default: int) -> int:
    """Return ``int(os.environ[name])`` or ``default`` if unset/invalid.

    Thin delegate to :func:`hephaestus.constants.read_timeout_env`, kept for the
    in-module callers; that helper logs and falls back on a non-integer value.
    """
    return read_timeout_env(name, default)


def planner_claude_timeout() -> int:
    """Timeout for planner agent calls (default 300s)."""
    return read_timeout_env(
        "HEPH_AGENT_PLAN_TIMEOUT",
        AGENT_PLAN_TIMEOUT,
        legacy_names=("HEPH_PLANNER_AGENT_TIMEOUT",),
    )


def plan_stage_timeout() -> int:
    """Timeout for the outer ``hephaestus-plan-issues`` stage (default 7200s)."""
    return read_timeout_env(
        "HEPH_PLAN_STAGE_TIMEOUT",
        PLAN_STAGE_TIMEOUT,
        legacy_names=("HEPH_PLANNER_AGENT_TIMEOUT",),
    )


def plan_reviewer_claude_timeout() -> int:
    """Timeout for agent calls inside the plan reviewer (default 600s)."""
    return read_timeout_env(
        "HEPH_AGENT_REVIEW_TIMEOUT",
        AGENT_REVIEW_TIMEOUT,
        legacy_names=("HEPH_PLAN_REVIEWER_AGENT_TIMEOUT",),
    )


def implementer_claude_timeout() -> int:
    """Timeout for the implementer's agent invocation (default 1800s)."""
    return read_timeout_env(
        "HEPH_AGENT_IMPL_TIMEOUT",
        AGENT_IMPL_TIMEOUT,
        legacy_names=("HEPH_IMPLEMENTER_AGENT_TIMEOUT",),
    )


def advise_claude_timeout() -> int:
    """Timeout for advise agent calls (default 7200s)."""
    return _read_int_env("HEPH_ADVISE_AGENT_TIMEOUT", 7200)


def pr_reviewer_claude_timeout() -> int:
    """Timeout for the PR reviewer's agent analysis (default 600s)."""
    return read_timeout_env(
        "HEPH_AGENT_REVIEW_TIMEOUT",
        AGENT_REVIEW_TIMEOUT,
        legacy_names=("HEPH_PR_REVIEWER_AGENT_TIMEOUT",),
    )


def address_review_claude_timeout() -> int:
    """Timeout for the address-review fix session (default 7200s)."""
    return _read_int_env("HEPH_ADDRESS_REVIEW_AGENT_TIMEOUT", 7200)


def ci_driver_claude_timeout() -> int:
    """Timeout for the CI-driver fix session (default 7200s)."""
    return _read_int_env("HEPH_CI_DRIVER_AGENT_TIMEOUT", 7200)


def learn_claude_timeout() -> int:
    """Timeout for ``/learn`` agent calls (default 300s)."""
    return read_timeout_env(
        "HEPH_AGENT_LEARN_TIMEOUT",
        AGENT_LEARN_TIMEOUT,
        legacy_names=("HEPH_LEARN_AGENT_TIMEOUT",),
    )


def follow_up_claude_timeout() -> int:
    """Timeout for the follow-up-issue agent session (default 7200s)."""
    return _read_int_env("HEPH_FOLLOW_UP_AGENT_TIMEOUT", 7200)


def git_message_agent_timeout() -> int:
    """Timeout for the lightweight commit/PR message writer (default 300s)."""
    return _read_int_env("HEPH_GIT_MESSAGE_AGENT_TIMEOUT", 300)


# Re-exported from hephaestus.github.client so the gh-adapter timeout lives
# with the gh adapter; this alias preserves the legacy import path.
from hephaestus.github.client import gh_cli_timeout  # noqa: E402

__all__ = [
    "AGENT_IMPL_TIMEOUT",
    "AGENT_LEARN_TIMEOUT",
    "AGENT_PLAN_TIMEOUT",
    "AGENT_REVIEW_TIMEOUT",
    "DEFAULT_AGENT_TIMEOUT",
    "DEFAULT_CI_POLL_MAX_WAIT",
    "DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT",
    "PLAN_STAGE_TIMEOUT",
    "address_review_claude_timeout",
    "advise_claude_timeout",
    "ci_driver_claude_timeout",
    "ci_poll_max_wait",
    "follow_up_claude_timeout",
    "gh_cli_timeout",
    "git_message_agent_timeout",
    "implementer_claude_timeout",
    "learn_claude_timeout",
    "plan_reviewer_claude_timeout",
    "plan_stage_timeout",
    "planner_claude_timeout",
    "pr_reviewer_claude_timeout",
    "read_timeout_env",
]


def ci_poll_max_wait() -> int:
    """Wall-clock seconds for the CI-driver poll loops (default 600s).

    Bounds the exponential-backoff wait in :mod:`ci_driver` while CI checks
    are still pending. Re-read on each invocation so tests and operators can
    tune it at runtime via ``HEPH_CI_POLL_MAX_WAIT``.
    """
    return _read_int_env("HEPH_CI_POLL_MAX_WAIT", 600)
