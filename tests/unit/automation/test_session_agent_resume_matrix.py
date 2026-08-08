"""Regression matrix for persisted session provider metadata."""

from __future__ import annotations

import pytest

from hephaestus.agents.runtime import session_agent_matches


@pytest.mark.parametrize("selected_agent", ["claude", "codex", "pi"])
@pytest.mark.parametrize("saved_agent", [None, "claude", "codex", "pi"])
def test_session_agent_matches_matrix(
    saved_agent: str | None,
    selected_agent: str,
) -> None:
    """Missing metadata remains Claude-only; explicit metadata must match exactly."""
    expected = (saved_agent or "claude") == selected_agent
    assert session_agent_matches(saved_agent, selected_agent) is expected
