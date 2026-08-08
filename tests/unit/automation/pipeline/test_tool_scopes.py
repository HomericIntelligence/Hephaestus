"""Tests for pipeline agent tool scopes (#2160)."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.tool_scopes import (
    AGENT_TOOL_SCOPES,
    DEFAULT_TOOL_SCOPE,
    ToolScope,
    tool_scope_for,
)
from hephaestus.automation.session_naming import (
    AGENT_ADDRESS_REVIEW,
    AGENT_ADVISE,
    AGENT_CI_DRIVER,
    AGENT_COMMENT_CLASSIFIER,
    AGENT_IMPLEMENTER,
    AGENT_LEARNINGS,
    AGENT_PLAN_REVIEWER,
    AGENT_PLANNER,
    AGENT_PR_REVIEWER,
)

# Every distinct session_agent= constant used at the pipeline stage call sites.
_STAGE_AGENTS = [
    AGENT_ADVISE,
    AGENT_PLANNER,
    AGENT_PLAN_REVIEWER,
    AGENT_IMPLEMENTER,
    AGENT_PR_REVIEWER,
    AGENT_COMMENT_CLASSIFIER,
    AGENT_ADDRESS_REVIEW,
    AGENT_CI_DRIVER,
    AGENT_LEARNINGS,
]
_REVIEWERS = [AGENT_PLAN_REVIEWER, AGENT_PR_REVIEWER, AGENT_COMMENT_CLASSIFIER]
_WRITERS = [AGENT_IMPLEMENTER, AGENT_ADDRESS_REVIEW, AGENT_CI_DRIVER, AGENT_LEARNINGS]


@pytest.mark.parametrize("agent", _STAGE_AGENTS)
def test_every_stage_agent_has_explicit_scope(agent: str) -> None:
    """No pipeline stage agent may fall through to the read-only default."""
    assert agent in AGENT_TOOL_SCOPES


@pytest.mark.parametrize("agent", _REVIEWERS)
def test_reviewer_scopes_grant_no_write_or_exec(agent: str) -> None:
    """Reviewers and classifiers are strictly read-only (no Write/Edit/Bash)."""
    tools = set(AGENT_TOOL_SCOPES[agent].allowed_tools.split(","))
    assert tools == {"Read", "Glob", "Grep"}


@pytest.mark.parametrize("agent", _WRITERS)
def test_writer_scopes_grant_write_and_exec(agent: str) -> None:
    """Implementer-class roles keep their legacy Write/Edit/Bash grant."""
    tools = set(AGENT_TOOL_SCOPES[agent].allowed_tools.split(","))
    assert {"Read", "Write", "Edit", "Bash"} <= tools


def test_unknown_agent_fails_closed_to_read_only() -> None:
    """An unmapped agent resolves to the read-only default scope."""
    assert tool_scope_for("no-such-agent") is DEFAULT_TOOL_SCOPE
    assert DEFAULT_TOOL_SCOPE.allowed_tools == "Read,Glob,Grep"


def test_unknown_agent_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Failing closed is logged so a missing mapping is visible."""
    with caplog.at_level("WARNING", logger="hephaestus.automation.pipeline.tool_scopes"):
        tool_scope_for("mystery-agent")
    assert any("mystery-agent" in rec.message for rec in caplog.records)


def test_mapped_agent_resolves_to_its_scope() -> None:
    """A known agent returns its exact mapped scope object."""
    assert tool_scope_for(AGENT_IMPLEMENTER) is AGENT_TOOL_SCOPES[AGENT_IMPLEMENTER]


@pytest.mark.parametrize(
    "scope", list(AGENT_TOOL_SCOPES.values()), ids=list(AGENT_TOOL_SCOPES.keys())
)
def test_permission_mode_is_dontask_everywhere(scope: ToolScope) -> None:
    """Every scope runs non-interactively, matching legacy automation."""
    assert scope.permission_mode == "dontAsk"


def test_default_permission_mode_is_dontask() -> None:
    """The fail-closed default also runs non-interactively."""
    assert DEFAULT_TOOL_SCOPE.permission_mode == "dontAsk"


def test_scope_map_is_immutable() -> None:
    """The policy map must not be mutable at runtime."""
    with pytest.raises(TypeError):
        AGENT_TOOL_SCOPES["x"] = DEFAULT_TOOL_SCOPE  # type: ignore[index]


def test_toolscope_is_frozen() -> None:
    """A ToolScope may not be mutated after construction."""
    scope = ToolScope("Read")
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        scope.allowed_tools = "Write"  # type: ignore[misc]
