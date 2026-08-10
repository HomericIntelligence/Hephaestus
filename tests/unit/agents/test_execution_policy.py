"""Behavioral contracts for Pi's immutable execution-policy matrix."""

from __future__ import annotations

import os
import subprocess
import sys
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
    monkeypatch.delenv("HEPH_PI_ISOLATION_ADAPTER", raising=False)
    monkeypatch.setattr(agent_runtime, "entry_points", pytest.fail, raising=False)

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
    monkeypatch.setenv("HEPH_PI_ISOLATION_ADAPTER", "must-not-be-loaded")
    monkeypatch.setattr(agent_runtime, "entry_points", pytest.fail, raising=False)

    agent_runtime.register_pi_isolation_adapter(Adapter())

    assert agent_runtime.resolve_agent("pi", cwd=tmp_path) == "pi"


def test_named_host_adapter_entry_point_admits_fresh_cli_process(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicitly selected installed adapter bootstraps a fresh process."""
    """An explicit installed adapter can bootstrap a new console process."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    class Adapter:
        def invoke(self, **_kwargs: object) -> agent_runtime.AgentRunResult:
            raise AssertionError("selection must not invoke the adapter")

    class EntryPoint:
        def load(self) -> object:
            def factory() -> Adapter:
                return Adapter()

            return factory
            return Adapter

    observed: list[dict[str, str]] = []

    def entry_points(**kwargs: str) -> tuple[EntryPoint, ...]:
        observed.append(kwargs)
        return (EntryPoint(),)

    monkeypatch.setattr(
        agent_runtime, "preflight_pi_environment", lambda _cwd: PiPreflightResult.ready_result()
    )
    monkeypatch.setattr(agent_runtime, "is_agent_authenticated", lambda _agent: True)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)
    monkeypatch.setattr(agent_runtime, "entry_points", entry_points, raising=False)
    monkeypatch.setenv("HEPH_PI_ISOLATION_ADAPTER", "operator-broker")

    assert agent_runtime.resolve_agent("pi", cwd=tmp_path) == "pi"
    assert observed == [
        {
            "group": "hephaestus.pi_isolation_adapters",
            "name": "operator-broker",
        }
    ]
    assert agent_runtime._PI_ISOLATION_ADAPTER is not None


def test_installed_host_adapter_bootstraps_in_a_fresh_python_process(tmp_path) -> None:
    """Installed entry-point metadata is sufficient without in-process registration."""
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    (fixture_root / "fixture_adapter.py").write_text(
        "class Adapter:\n"
        "    def invoke(self, **kwargs):\n"
        "        raise AssertionError('selection must not invoke the adapter')\n"
        "\n"
        "def create_adapter():\n"
        "    return Adapter()\n",
        encoding="utf-8",
    )
    metadata = fixture_root / "fixture_adapter-1.0.dist-info"
    metadata.mkdir()
    (metadata / "entry_points.txt").write_text(
        "[hephaestus.pi_isolation_adapters]\nprocess-fixture = fixture_adapter:create_adapter\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HEPH_PI_ISOLATION_ADAPTER"] = "process-fixture"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(fixture_root), env.get("PYTHONPATH", "")) if part
    )
    process_code = (
        "from hephaestus.agents import runtime; "
        + "runtime._require_pi_isolation_adapter(); print('loaded')"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            process_code,
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "loaded"


def test_named_host_adapter_bootstraps_before_direct_policy_dispatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct library callers load the selected adapter before provider dispatch."""
    received: dict[str, object] = {}

    class Adapter:
        def invoke(self, **kwargs: object) -> agent_runtime.AgentRunResult:
            received.update(kwargs)
            return agent_runtime.AgentRunResult(stdout="reviewed", stderr="")

    class EntryPoint:
        def load(self) -> object:
            return Adapter

    monkeypatch.setattr(agent_runtime, "_require_pi_automation_admission", lambda _cwd: None)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)
    monkeypatch.setattr(
        agent_runtime, "entry_points", lambda **_kwargs: (EntryPoint(),), raising=False
    )
    monkeypatch.setenv("HEPH_PI_ISOLATION_ADAPTER", "operator-broker")
    request = ExecutionRequest(
        AgentRole.PR_REVIEWER,
        AgentOperation.PR_REVIEW,
        SessionLifecycle.ONE_SHOT,
    )

    result = agent_runtime.run_agent_text(
        "pi", "review", cwd=tmp_path, timeout=30, execution_request=request
    )

    assert result.stdout == "reviewed"
    assert received["prompt"] == "review"


@pytest.mark.parametrize("match_count", [0, 2])
def test_named_host_adapter_requires_one_exact_entry_point(
    tmp_path, monkeypatch: pytest.MonkeyPatch, match_count: int
) -> None:
    """A missing or ambiguous broker selection fails before authentication."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    class EntryPoint:
        pass

    monkeypatch.setattr(
        agent_runtime, "preflight_pi_environment", lambda _cwd: PiPreflightResult.ready_result()
    )
    monkeypatch.setattr(agent_runtime, "is_agent_authenticated", pytest.fail)
    authenticated = pytest.fail
    monkeypatch.setattr(agent_runtime, "is_agent_authenticated", authenticated)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)
    monkeypatch.setattr(
        agent_runtime,
        "entry_points",
        lambda **_kwargs: tuple(EntryPoint() for _index in range(match_count)),
        raising=False,
    )
    monkeypatch.setenv("HEPH_PI_ISOLATION_ADAPTER", "operator-broker")

    with pytest.raises(
        agent_runtime.PiIsolationUnavailableError,
        match="not installed exactly once",
    ):
        agent_runtime.resolve_agent("pi", cwd=tmp_path)


@pytest.mark.parametrize("failure", ["discover", "load", "initialize"])
def test_named_host_adapter_sanitizes_external_factory_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Private diagnostics never escape discovery, loading, or initialization."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    class EntryPoint:
        def load(self) -> object:
            if failure == "load":
                raise RuntimeError("private load diagnostic")

            def factory() -> object:
                raise RuntimeError("private initialization diagnostic")

            return factory

    monkeypatch.setattr(
        agent_runtime, "preflight_pi_environment", lambda _cwd: PiPreflightResult.ready_result()
    )
    monkeypatch.setattr(agent_runtime, "is_agent_authenticated", pytest.fail)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)

    def entry_points(**_kwargs: str) -> tuple[EntryPoint, ...]:
        if failure == "discover":
            raise RuntimeError("private discovery diagnostic")
        return (EntryPoint(),)

    monkeypatch.setattr(agent_runtime, "entry_points", entry_points, raising=False)
    monkeypatch.setenv("HEPH_PI_ISOLATION_ADAPTER", "operator-broker")

    with pytest.raises(
        agent_runtime.PiIsolationUnavailableError,
        match="could not be discovered" if failure == "discover" else "could not be initialized",
    ) as exc_info:
        agent_runtime.resolve_agent("pi", cwd=tmp_path)

    assert "private" not in str(exc_info.value)


def test_named_host_adapter_rejects_an_invalid_protocol(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A factory result without a callable invoke boundary remains unadmitted."""
def test_named_host_adapter_sanitizes_protocol_attribute_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External attribute diagnostics never escape protocol validation."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    class Adapter:
        @property
        def invoke(self) -> object:
            raise RuntimeError("private validation diagnostic")

    class EntryPoint:
        def load(self) -> object:
            return Adapter

    monkeypatch.setattr(
        agent_runtime, "preflight_pi_environment", lambda _cwd: PiPreflightResult.ready_result()
    )
    monkeypatch.setattr(agent_runtime, "is_agent_authenticated", pytest.fail)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)
    monkeypatch.setattr(
        agent_runtime, "entry_points", lambda **_kwargs: (EntryPoint(),), raising=False
    )
    monkeypatch.setenv("HEPH_PI_ISOLATION_ADAPTER", "operator-broker")

    with pytest.raises(
        agent_runtime.PiIsolationUnavailableError,
        match="could not be initialized",
    ) as exc_info:
        agent_runtime.resolve_agent("pi", cwd=tmp_path)

    assert "private" not in str(exc_info.value)


def test_named_host_adapter_rejects_incompatible_invoke_signature(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected broker must accept the complete keyword invocation contract."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    class Adapter:
        def invoke(
            self,
            *,
            policy: ExecutionPolicy,
            command: list[str],
        ) -> agent_runtime.AgentRunResult:
            raise AssertionError("selection must not invoke the adapter")

    class EntryPoint:
        def load(self) -> object:
            return Adapter

    monkeypatch.setattr(
        agent_runtime, "preflight_pi_environment", lambda _cwd: PiPreflightResult.ready_result()
    )
    monkeypatch.setattr(agent_runtime, "is_agent_authenticated", lambda _agent: True)
    monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)
    monkeypatch.setattr(agent_runtime, "entry_points", lambda **_kwargs: (EntryPoint(),))
    monkeypatch.setenv("HEPH_PI_ISOLATION_ADAPTER", "operator-broker")

    with pytest.raises(
        agent_runtime.PiIsolationUnavailableError,
        match=r"does not implement invoke\(\) with the required keyword contract",
    ):
        agent_runtime.resolve_agent("pi", cwd=tmp_path)

    assert agent_runtime._PI_ISOLATION_ADAPTER is None


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
