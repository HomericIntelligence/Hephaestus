"""Claude agent end-to-end contract tests (opt-in; spends tokens).

Exercises the production ``invoke_claude_with_session`` chokepoint against the
real ``claude`` CLI to prove the create-then-resume session lineage documented
at ``claude_invoke.py`` — the exact seam that broke in #1166/#1168, which no
mocked test can cover.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from hephaestus.agents.runtime import is_agent_authenticated
from hephaestus.automation.claude_invoke import invoke_claude_with_session

pytestmark = [pytest.mark.integration, pytest.mark.contract]

CONTRACT_MODEL = os.environ.get("HEPHAESTUS_CONTRACT_MODEL", "haiku")


def test_invoke_and_resume_session(agent_lane_enabled: None, tmp_path: Path) -> None:
    """First call creates a session (--session-id); second resumes it (--resume).

    Uses the REAL ``is_agent_authenticated`` — the suite-wide stub in
    ``tests/conftest.py`` exempts contract-marked tests — so an unauthenticated
    environment produces a clean skip instead of a failure.
    """
    if not is_agent_authenticated("claude"):
        pytest.skip("claude CLI not installed/authenticated")
    issue = f"contract-{uuid.uuid4().hex[:8]}"
    stdout1, session1 = invoke_claude_with_session(
        repo="hephaestus-contract",
        issue=issue,
        agent="contract-probe",
        prompt="Reply with exactly the word OK and nothing else.",
        model=CONTRACT_MODEL,
        cwd=tmp_path,
        timeout=300,
    )
    assert "OK" in stdout1
    stdout2, session2 = invoke_claude_with_session(
        repo="hephaestus-contract",
        issue=issue,
        agent="contract-probe",
        prompt="Reply with exactly the word RESUMED and nothing else.",
        model=CONTRACT_MODEL,
        cwd=tmp_path,
        timeout=300,
    )
    # The session id is uuid5 of (repo, issue, agent, model) — deterministic —
    # so the second call resumes the transcript the first call created.
    assert session2 == session1
    assert "RESUMED" in stdout2
