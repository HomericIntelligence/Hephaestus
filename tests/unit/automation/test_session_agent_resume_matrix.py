"""Regression matrix for persisted session provider metadata."""

from __future__ import annotations

import pytest

from hephaestus.agents.runtime import session_agent_matches


@pytest.mark.parametrize(
    ("saved_agent", "selected_agent", "expected"),
    [
        ("claude", "claude", True),
        ("codex", "codex", True),
        ("pi", "pi", True),
        ("claude", "codex", False),
        ("codex", "claude", False),
        (None, "claude", False),
        ("", "claude", False),
        ("unknown", "claude", False),
        ("unknown", "unknown", False),
        (42, "claude", False),
        ({"provider": "claude"}, "claude", False),
    ],
)
def test_session_agent_matches_matrix(
    saved_agent: object,
    selected_agent: str,
    expected: bool,
) -> None:
    """Only explicit, supported provider metadata can authorize a resume."""
    assert session_agent_matches(saved_agent, selected_agent) is expected
