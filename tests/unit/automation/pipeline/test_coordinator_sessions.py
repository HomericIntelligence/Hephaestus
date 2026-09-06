"""Tests for coordinator-owned agent session persistence."""

from __future__ import annotations

from pathlib import Path

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.pi_session import create_pi_binding
from hephaestus.automation.pipeline.coordinator_sessions import store_agent_session_result
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem


def test_pi_default_binding_is_validated_against_the_resolved_selection(tmp_path: Path) -> None:
    """The coordinator persists a binding for the trusted Pi default."""
    pi_dir = tmp_path / "pi-agent"
    pi_dir.mkdir()
    settings_path = pi_dir / "settings.json"
    settings_path.write_text(
        '{"defaultProvider":"IFM","defaultModel":"K2-Horizon-0.9B","defaultThinkingLevel":"high"}',
        encoding="utf-8",
    )
    settings_path.chmod(0o600)
    job = AgentJob(
        repo="repo",
        issue=1,
        agent="pi",
        model="",
        prompt_builder=lambda: "plan",
        cwd=tmp_path,
        timeout_s=30,
        pi_dir=pi_dir,
        execution_request=ExecutionRequest(
            AgentRole.PLANNER,
            AgentOperation.PLAN,
            SessionLifecycle.START_NEW,
        ),
    )
    binding = create_pi_binding(
        session_id="pi-session",
        cwd=tmp_path,
        role=AgentRole.PLANNER,
        model="IFM/K2-Horizon-0.9B:high",
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1)

    error = store_agent_session_result(
        item,
        job,
        JobResult(ok=True, session_id=binding.session_id, session_binding=binding),
    )

    assert error is None
    assert item.session_bindings["pi"] == binding
