"""Behavioral contracts for Pi's immutable execution-policy matrix."""

from __future__ import annotations

from typing import cast

import pytest

from hephaestus.agents import runtime as agent_runtime
from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionPolicy,
    ExecutionPolicyError,
    ExecutionRequest,
    FilesystemMode,
    NetworkMode,
    SessionLifecycle,
    intersect_child_policy,
    resolve_policy,
)
from hephaestus.agents.pi_session import PiSessionBindingError, create_pi_binding


def test_pr_review_one_shot_uses_the_read_only_review_policy() -> None:
    """Direct review resolves the same least-privilege matrix entry as queue review."""
    policy = resolve_policy(
        ExecutionRequest(
            AgentRole.PR_REVIEWER,
            AgentOperation.PR_REVIEW,
            SessionLifecycle.ONE_SHOT,
        )
    )

    assert policy.filesystem is FilesystemMode.CHECKOUT_RO
    assert policy.builtins == frozenset({"read", "grep", "find", "ls", "bash"})
    assert policy.skills == frozenset({"athena:pr-review"})
    assert policy.subagent is False
    assert policy.network is NetworkMode.CONSTRAINED_WEB_RELAY


def test_unknown_lifecycle_fails_closed() -> None:
    """A permitted role/operation cannot be started with an invented lifecycle."""
    with pytest.raises(ExecutionPolicyError, match="not permitted"):
        resolve_policy(
            ExecutionRequest(
                AgentRole.PLANNER,
                AgentOperation.PLAN,
                SessionLifecycle.RESUME_REQUIRED,
            )
        )


def test_delegation_is_na_until_a_child_policy_broker_is_available() -> None:
    """Pi never exposes a child launch that bypasses policy intersection."""
    parent = resolve_policy(
        ExecutionRequest(
            AgentRole.IMPLEMENTER,
            AgentOperation.ADDRESS_REVIEW,
            SessionLifecycle.START_NEW,
        )
    )
    requested = resolve_policy(
        ExecutionRequest(
            AgentRole.PR_REVIEWER,
            AgentOperation.PR_REVIEW,
            SessionLifecycle.START_NEW,
        )
    )

    assert parent.subagent is False
    with pytest.raises(ExecutionPolicyError, match="does not permit subagent"):
        intersect_child_policy(parent, requested)


def test_child_policy_intersection_can_only_reduce_parent_capabilities() -> None:
    """A brokered child keeps only capabilities granted by both policies."""
    parent = ExecutionPolicy(
        role=AgentRole.IMPLEMENTER,
        operation=AgentOperation.ADDRESS_REVIEW,
        permitted_lifecycles=frozenset({SessionLifecycle.START_NEW}),
        filesystem=FilesystemMode.WORKTREE_RW,
        builtins=frozenset({"read", "write"}),
        skills=frozenset({"athena:pr-review", "shared"}),
        subagent=True,
        network=NetworkMode.CONSTRAINED_WEB_RELAY,
    )
    requested = ExecutionPolicy(
        role=AgentRole.PR_REVIEWER,
        operation=AgentOperation.PR_REVIEW,
        permitted_lifecycles=frozenset({SessionLifecycle.ONE_SHOT}),
        filesystem=FilesystemMode.CHECKOUT_RO,
        builtins=frozenset({"read", "bash"}),
        skills=frozenset({"athena:pr-review", "ungranted"}),
        subagent=True,
        network=NetworkMode.CONSTRAINED_WEB_RELAY,
    )

    child = intersect_child_policy(parent, requested)

    assert child.filesystem is FilesystemMode.CHECKOUT_RO
    assert child.builtins == frozenset({"read"})
    assert child.skills == frozenset({"athena:pr-review"})
    assert child.subagent is False
    assert child.network is NetworkMode.CONSTRAINED_WEB_RELAY


def test_child_policy_intersection_rejects_filesystem_widening() -> None:
    """A child cannot exchange a writable worktree for another writable root."""
    parent = ExecutionPolicy(
        role=AgentRole.IMPLEMENTER,
        operation=AgentOperation.IMPLEMENT,
        permitted_lifecycles=frozenset({SessionLifecycle.START_NEW}),
        filesystem=FilesystemMode.WORKTREE_RW,
        builtins=frozenset({"read", "write"}),
        skills=frozenset(),
        subagent=True,
        network=NetworkMode.PROVIDER_RELAY,
    )
    requested = ExecutionPolicy(
        role=AgentRole.LEARNER,
        operation=AgentOperation.LEARN,
        permitted_lifecycles=frozenset({SessionLifecycle.START_NEW}),
        filesystem=FilesystemMode.MNEMOSYNE_RW,
        builtins=frozenset({"read", "write"}),
        skills=frozenset(),
        subagent=False,
        network=NetworkMode.PROVIDER_RELAY,
    )

    with pytest.raises(ExecutionPolicyError, match="would widen parent"):
        intersect_child_policy(parent, requested)


def test_pi_policy_args_never_advertise_an_unbrokered_subagent_tool() -> None:
    """Provider-visible flags cannot create a child execution path."""
    policy = resolve_policy(
        ExecutionRequest(
            AgentRole.PR_REVIEWER,
            AgentOperation.PR_REVIEW,
            SessionLifecycle.ONE_SHOT,
        )
    )

    assert "subagent" not in agent_runtime._pi_policy_args(policy)[1].split(",")


def test_ready_pi_is_explicitly_na_without_a_registered_isolation_adapter(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stock installations reject Pi before a queue or wrapper can start it."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    monkeypatch.setattr(
        agent_runtime, "preflight_pi_environment", lambda _cwd: PiPreflightResult.ready_result()
    )
    monkeypatch.setattr(agent_runtime, "is_agent_authenticated", lambda _agent: True)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)

    with pytest.raises(agent_runtime.PiIsolationUnavailableError, match="Pi automation is N/A"):
        agent_runtime.resolve_agent("pi", cwd=tmp_path)


def test_registered_host_adapter_admits_pi_selection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host integration has one explicit registration path before Pi is selected."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    class Adapter:
        def invoke(self, **_kwargs: object) -> agent_runtime.AgentRunResult:
            raise AssertionError("selection must not invoke the adapter")

    monkeypatch.setattr(
        agent_runtime, "preflight_pi_environment", lambda _cwd: PiPreflightResult.ready_result()
    )
    monkeypatch.setattr(agent_runtime, "is_agent_authenticated", lambda _agent: True)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)

    agent_runtime.register_pi_isolation_adapter(Adapter())

    assert agent_runtime.resolve_agent("pi", cwd=tmp_path) == "pi"


def test_pi_policy_dispatch_fails_before_provider_without_os_adapter(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi cannot treat model-visible tool flags as filesystem or network isolation."""
    monkeypatch.setattr(agent_runtime, "_require_pi_automation_admission", lambda _cwd: None)
    request = ExecutionRequest(
        AgentRole.PR_REVIEWER, AgentOperation.PR_REVIEW, SessionLifecycle.ONE_SHOT
    )

    with pytest.raises(
        agent_runtime.PiIsolationUnavailableError,
        match=r"checkout_ro.*constrained_web",
    ):
        agent_runtime.run_agent_text(
            "pi", "review", cwd=tmp_path, timeout=30, execution_request=request
        )


def test_pi_policy_dispatch_hands_read_only_and_network_policy_to_adapter(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only Pi dispatch seam gives the external adapter the full policy."""
    received: dict[str, object] = {}

    class Adapter:
        def invoke(self, **kwargs: object) -> agent_runtime.AgentRunResult:
            received.update(kwargs)
            return agent_runtime.AgentRunResult(stdout="review", stderr="")

    monkeypatch.setattr(agent_runtime, "_require_pi_automation_admission", lambda _cwd: None)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", Adapter())
    request = ExecutionRequest(
        AgentRole.PR_REVIEWER, AgentOperation.PR_REVIEW, SessionLifecycle.ONE_SHOT
    )

    result = agent_runtime.run_agent_text(
        "pi", "review", cwd=tmp_path, timeout=30, execution_request=request
    )

    assert result.stdout == "review"
    policy = cast(ExecutionPolicy, received["policy"])
    assert policy.filesystem is FilesystemMode.CHECKOUT_RO
    assert policy.network is NetworkMode.CONSTRAINED_WEB_RELAY
    assert received["session_id"] is None


def test_pi_session_start_rejects_a_binding_but_resume_requires_one(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A START_NEW request never silently turns into a session resume."""
    binding = create_pi_binding(
        session_id="pi-session-123", cwd=tmp_path, role=AgentRole.PLANNER, model="model"
    )
    monkeypatch.setattr(agent_runtime, "_require_pi_automation_admission", lambda _cwd: None)
    start_request = ExecutionRequest(
        AgentRole.PLANNER, AgentOperation.PLAN, SessionLifecycle.START_NEW
    )

    with pytest.raises(PiSessionBindingError, match="start-new or one-shot"):
        agent_runtime.run_agent_session(
            "pi",
            "plan",
            cwd=tmp_path,
            timeout=30,
            model="model",
            execution_request=start_request,
            resume_binding=binding,
        )

    one_shot_request = ExecutionRequest(
        AgentRole.PR_REVIEWER,
        AgentOperation.AUDIT_REVIEW,
        SessionLifecycle.ONE_SHOT,
    )
    with pytest.raises(PiSessionBindingError, match="start-new or one-shot"):
        agent_runtime.run_agent_session(
            "pi",
            "audit",
            cwd=tmp_path,
            timeout=30,
            model="model",
            execution_request=one_shot_request,
            resume_binding=binding,
        )

    received: dict[str, object] = {}

    class Adapter:
        def invoke(self, **kwargs: object) -> agent_runtime.AgentRunResult:
            received.update(kwargs)
            return agent_runtime.AgentRunResult(stdout="amended", stderr="")

    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", Adapter())
    resume_request = ExecutionRequest(
        AgentRole.PLANNER, AgentOperation.AMEND, SessionLifecycle.RESUME_REQUIRED
    )
    result = agent_runtime.resume_agent_session(
        "pi",
        binding.session_id,
        "amend",
        cwd=tmp_path,
        timeout=30,
        model="model",
        execution_request=resume_request,
        resume_binding=binding,
    )

    assert received["session_id"] == binding.session_id
    assert result.session_id == binding.session_id


def test_pi_session_start_dispatches_without_resume_binding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A START_NEW request reaches the adapter without an inherited session id."""
    received: dict[str, object] = {}

    class Adapter:
        def invoke(self, **kwargs: object) -> agent_runtime.AgentRunResult:
            received.update(kwargs)
            return agent_runtime.AgentRunResult(
                stdout="planned", stderr="", session_id="pi-session-new"
            )

    monkeypatch.setattr(agent_runtime, "_require_pi_automation_admission", lambda _cwd: None)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", Adapter())
    request = ExecutionRequest(AgentRole.PLANNER, AgentOperation.PLAN, SessionLifecycle.START_NEW)

    result = agent_runtime.run_agent_session(
        "pi", "plan", cwd=tmp_path, timeout=30, model="model", execution_request=request
    )

    assert received["session_id"] is None
    assert result.session_id == "pi-session-new"
    assert result.session_binding is not None
    assert result.session_binding.session_id == "pi-session-new"
