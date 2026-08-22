"""Claude agent end-to-end contract tests (opt-in; spends tokens)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from hephaestus.agents.runtime import is_agent_authenticated
from hephaestus.automation.claude_invoke import invoke_claude_with_session
from hephaestus.automation.session_naming import AGENT_ADVISE

pytestmark = [pytest.mark.integration, pytest.mark.contract]


def test_invoke_and_resume_session(
    agent_lane_enabled: None, contract_model: str, tmp_path: Path
) -> None:
    """Create a real session and then resume its deterministic lineage."""
    if not is_agent_authenticated("claude"):
        pytest.skip("claude CLI not installed/authenticated")

    issue = f"contract-{uuid.uuid4().hex[:8]}"
    stdout1, session1 = invoke_claude_with_session(
        repo="hephaestus-contract",
        issue=issue,
        agent=AGENT_ADVISE,
        prompt="Reply with exactly the word OK and nothing else.",
        model=contract_model,
        cwd=tmp_path,
        timeout=300,
        allowed_tools="",
        permission_mode="dontAsk",
    )
    assert "OK" in stdout1

    stdout2, session2 = invoke_claude_with_session(
        repo="hephaestus-contract",
        issue=issue,
        agent=AGENT_ADVISE,
        prompt="Reply with exactly the word RESUMED and nothing else.",
        model=contract_model,
        cwd=tmp_path,
        timeout=300,
        allowed_tools="",
        permission_mode="dontAsk",
    )
    assert session2 == session1
    assert "RESUMED" in stdout2
