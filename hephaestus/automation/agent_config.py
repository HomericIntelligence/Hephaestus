"""Agent configuration for the automation pipeline.

Timeout defaults and role-model wrappers share one configuration owner.
Model resolution and deterministic session identities live in
``hephaestus.agents.invocation_config``; Claude subprocess execution stays in
``hephaestus/agents/claude_invoke.py``.

Model selection
---------------
The caller selects a tool separately from its model string. An omitted model
uses that tool's configured default. Explicit names pass through without
catalog checks or alias translation. Role selections inherit the global model.
A quota fallback requires an explicit fallback model.

Reasoning effort
----------------
Each model option accepts ``MODEL[:EFFORT]``. The final nonempty colon segment
is a free-form provider effort. Codex receives ``model_reasoning_effort``.
OpenCode receives ``--variant``. Pi receives ``--thinking``. The value
``default`` selects the applicable provider default. Claude uses the base model
and its default effort.

Timeouts
--------
Timeouts are typed CLI/configuration values. The default accessors return
fixed values and do not read process environment variables.
"""

from __future__ import annotations

from hephaestus.agents.invocation_config import _resolve_model
from hephaestus.agents.model_selection import (
    AgentModelSelection,
    normalize_model_reference,
    parse_model_selection,
)
from hephaestus.constants import (
    AGENT_IMPL_TIMEOUT,
    AGENT_LEARN_TIMEOUT,
    AGENT_PLAN_TIMEOUT,
    AGENT_REVIEW_TIMEOUT,
    IMPLEMENTER_CHECKPOINT_INTERVAL_S,
)

# ── Model selection ──────────────────────────────────────────────────────────


def _normalize_configured_model(model: str) -> AgentModelSelection:
    """Return a literal model name and optional provider effort."""
    return parse_model_selection(model)


def normalize_claude_model(model: str) -> str:
    """Return the literal Claude model without the effort suffix."""
    return parse_model_selection(model).model


def planner_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Model used to generate implementation plans from issue text."""
    return _resolve_model(value, agent=agent)


def implementer_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Return the model for implementation and its resumed repair sessions."""
    return _resolve_model(value, agent=agent)


def reviewer_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Model used by plan/PR reviewers and the review-fix loop."""
    return _resolve_model(value, agent=agent)


def advise_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Return an explicit host advice model or the tool default."""
    return _resolve_model(value, agent=agent)


def learn_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Return an explicit host learning model or the tool default."""
    return _resolve_model(value, agent=agent)


# ── Subprocess timeouts ──────────────────────────────────────────────────────


DEFAULT_THROUGHPUT_TIMEOUT: int = 1200
DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT: int = DEFAULT_THROUGHPUT_TIMEOUT


def planner_claude_timeout() -> int:
    """Timeout for planner agent calls (default 1200s)."""
    return AGENT_PLAN_TIMEOUT


def plan_reviewer_claude_timeout() -> int:
    """Timeout for agent calls inside the plan reviewer (default 3600s)."""
    return AGENT_REVIEW_TIMEOUT


def implementer_claude_timeout() -> int:
    """Timeout for the implementer's agent invocation (default 1800s)."""
    return AGENT_IMPL_TIMEOUT


def implementer_checkpoint_interval_s() -> int:
    """Periodic implementer progress/checkpoint interval in seconds.

    The default is zero, which disables the checkpoint feature. An operator
    selects an interval to enable it. This accessor returns the fixed default
    only; see
    :func:`hephaestus.automation.pipeline.implementer_checkpoint.interval_from_config`
    for the config-backed value.
    """
    return IMPLEMENTER_CHECKPOINT_INTERVAL_S


def advise_claude_timeout() -> int:
    """Timeout for advise agent calls (default 7200s)."""
    return 7200


def pr_reviewer_claude_timeout() -> int:
    """Timeout for the PR reviewer's agent analysis (default 3600s)."""
    return AGENT_REVIEW_TIMEOUT


def learn_claude_timeout() -> int:
    """Timeout for ``/learn`` agent calls (default 1200s)."""
    return AGENT_LEARN_TIMEOUT


def git_message_agent_timeout() -> int:
    """Timeout for the lightweight commit/PR message writer (default 1200s)."""
    return DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT


# Re-exported from hephaestus.git.gh.client so the gh-adapter timeout lives
# with the gh adapter; this alias preserves the legacy import path.
from hephaestus.git.gh.client import gh_cli_timeout  # noqa: E402

_GIT_REPO_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

_AUTO_IMPL_BRANCH_SUFFIX = "-auto-impl"


def issue_auto_impl_branch_name(issue_number: int | str) -> str:
    """Return the canonical branch name for an issue implementation PR."""
    return f"{issue_number}{_AUTO_IMPL_BRANCH_SUFFIX}"


__all__ = [
    # Timeouts
    "AGENT_IMPL_TIMEOUT",
    "AGENT_LEARN_TIMEOUT",
    "AGENT_PLAN_TIMEOUT",
    "AGENT_REVIEW_TIMEOUT",
    # Model selection
    "DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT",
    "DEFAULT_THROUGHPUT_TIMEOUT",
    "IMPLEMENTER_CHECKPOINT_INTERVAL_S",
    "advise_claude_timeout",
    "advise_model",
    "gh_cli_timeout",
    "git_message_agent_timeout",
    "implementer_checkpoint_interval_s",
    "implementer_claude_timeout",
    "implementer_model",
    "issue_auto_impl_branch_name",
    "learn_claude_timeout",
    "learn_model",
    "normalize_claude_model",
    "normalize_model_reference",
    "parse_model_selection",
    "plan_reviewer_claude_timeout",
    "planner_claude_timeout",
    "planner_model",
    "pr_reviewer_claude_timeout",
    "reviewer_model",
]
