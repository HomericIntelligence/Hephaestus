"""Least-privilege Claude tool scopes for pipeline agent jobs (#2160).

Maps each session-agent constant to the explicit ``--allowedTools`` /
``--permission-mode`` pair the worker pool passes to every Claude
invocation. Unknown agents fail closed to the read-only default.
A stage can set ``AgentJob.allowed_tools`` to supply an explicit tool grant.
That grant has priority over this default map.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from hephaestus.automation.session_naming import (
    AGENT_IMPLEMENTER,
    AGENT_PLAN_REVIEWER,
    AGENT_PLANNER,
    AGENT_PR_REVIEWER,
)

logger = logging.getLogger(__name__)

_READ_ONLY = "Read,Glob,Grep"
_READ_EXPLORE = "Read,Glob,Grep,Bash"
_WRITE = "Read,Write,Edit,Glob,Grep,Bash"


@dataclass(frozen=True)
class ToolScope:
    """Explicit least-privilege tool grant for one agent role.

    Attributes:
        allowed_tools: Comma-separated ``--allowedTools`` value passed to the
            Claude CLI.
        permission_mode: ``--permission-mode`` value; ``"dontAsk"`` runs the
            granted tools non-interactively, as everywhere else in
            ``hephaestus.automation``.

    """

    allowed_tools: str
    permission_mode: str = "dontAsk"


#: Fail-closed scope for any agent absent from :data:`AGENT_TOOL_SCOPES`.
DEFAULT_TOOL_SCOPE = ToolScope(_READ_ONLY)

#: Reviewers can read source. Only implementation receives ``Write,Edit``.
AGENT_TOOL_SCOPES: Mapping[str, ToolScope] = MappingProxyType(
    {
        AGENT_PLANNER: ToolScope(_READ_EXPLORE),
        AGENT_PLAN_REVIEWER: ToolScope(_READ_ONLY),
        AGENT_IMPLEMENTER: ToolScope(_WRITE),
        AGENT_PR_REVIEWER: ToolScope(_READ_ONLY),
    }
)


def tool_scope_for(agent: str) -> ToolScope:
    """Resolve the least-privilege scope for ``agent``.

    Args:
        agent: The session-agent role constant (e.g. ``AGENT_IMPLEMENTER``).

    Returns:
        The mapped :class:`ToolScope`, or :data:`DEFAULT_TOOL_SCOPE`
        (read-only) when ``agent`` is unmapped — a security control degrades
        to the most restrictive scope, not the most permissive. Dynamic
        reviewer tokens (e.g. ``strict-review-<sha>-a<n>``) intentionally miss
        the exact-match map and fall through to read-only.

    """
    scope = AGENT_TOOL_SCOPES.get(agent)
    if scope is None:
        logger.warning("no tool scope for agent %r; using read-only default", agent)
        return DEFAULT_TOOL_SCOPE
    return scope
