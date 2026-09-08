"""Check independent tool and model selection for pipeline roles."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from hephaestus.automation.pipeline.stages.base import StageContext, agent_provider, stage_model


def test_role_tool_overrides_global_tool_without_changing_model() -> None:
    """A role tool keeps the explicit global model."""
    context = cast(
        StageContext,
        SimpleNamespace(
            config=SimpleNamespace(
                agent="codex",
                reviewer_agent="claude",
                model="gpt-6-astra:max",
                reviewer_model="",
            )
        ),
    )
    assert agent_provider(context, "reviewer") == "claude"
    assert agent_provider(context, "planner") == "codex"
    assert stage_model(context, "reviewer", lambda: "legacy") == "gpt-6-astra:max"


def test_role_admission_receives_only_that_roles_model() -> None:
    """Tool admission does not receive a different role's model."""
    from argparse import Namespace
    from unittest.mock import Mock

    from hephaestus.automation.role_selection import resolve_role_agents

    args = Namespace(
        agent="codex",
        model="Global:max",
        reviewer_agent="claude",
        reviewer_model="Review",
        planner_model="Plan",
        fallback_model="",
    )
    resolver = Mock(side_effect=lambda agent, **kwargs: agent)
    agent, roles = resolve_role_agents(args, ("planner", "reviewer"), resolver=resolver)
    assert agent == "codex"
    assert roles == {"planner": "codex", "reviewer": "claude"}
    assert resolver.call_args_list[0].kwargs["model_references"] == ("Plan",)
    assert resolver.call_args_list[1].kwargs["model_references"] == ("Review",)


def test_session_selection_rejects_changed_tool_and_model(tmp_path: Path) -> None:
    """An opaque session cannot cross a tool or model boundary."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.coordinator_sessions import (
        session_selection_error,
        store_agent_session_result,
    )
    from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

    item = WorkItem(repo="test", kind=ItemKind.ISSUE, issue=1)
    job = AgentJob(
        repo="test",
        issue=1,
        agent="codex",
        model="gpt-6-astra:max",
        prompt_builder=lambda: "",
        cwd=tmp_path,
        timeout_s=1,
        session_agent="reviewer",
    )
    assert store_agent_session_result(item, job, JobResult(ok=True, session_id="opaque")) is None
    assert session_selection_error(item, replace(job, agent="opencode")) is None
    job = replace(job, resume_session_id="opaque")
    assert session_selection_error(item, job) is None
    assert session_selection_error(item, replace(job, agent="claude")) is not None
    assert session_selection_error(item, replace(job, model="gpt-6-astra:low")) is not None


def test_legacy_session_requires_selection_identity(tmp_path: Path) -> None:
    """An old opaque ID must not select a different tool by accident."""
    from hephaestus.automation.pipeline.coordinator_sessions import session_selection_error
    from hephaestus.automation.pipeline.jobs import AgentJob
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

    item = WorkItem(repo="test", kind=ItemKind.ISSUE, issue=1)
    job = AgentJob(
        repo="test",
        issue=1,
        agent="opencode",
        model="New",
        prompt_builder=lambda: "",
        cwd=tmp_path,
        timeout_s=1,
        session_agent="reviewer",
        resume_session_id="old-codex-session",
    )
    assert session_selection_error(item, job) == "session selection is missing; start a new session"


def test_all_role_overrides_do_not_detect_an_unused_global_tool() -> None:
    """Explicit role tools do not require another installed tool."""
    from argparse import Namespace
    from unittest.mock import Mock

    from hephaestus.automation.role_selection import resolve_role_agents

    args = Namespace(
        agent=None,
        model="Shared",
        planner_agent="codex",
        reviewer_agent="opencode",
        implementer_agent="pi",
    )
    resolver = Mock(side_effect=lambda agent, **kwargs: agent)
    global_agent, roles = resolve_role_agents(
        args, ("planner", "implementer", "reviewer"), resolver=resolver
    )
    assert global_agent == ""
    assert roles == {"planner": "codex", "implementer": "pi", "reviewer": "opencode"}
    assert all(call.args[0] is not None for call in resolver.call_args_list)
