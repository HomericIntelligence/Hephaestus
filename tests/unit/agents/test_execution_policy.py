"""Behavioral contracts for Pi's immutable execution-policy matrix."""

from __future__ import annotations

import pytest

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionPolicyError,
    ExecutionRequest,
    FilesystemMode,
    NetworkMode,
    SessionLifecycle,
    intersect_child_policy,
    resolve_policy,
)


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
    assert policy.subagent is True
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


def test_child_policy_is_a_strict_intersection() -> None:
    """Delegation cannot restore write or web capabilities absent from its parent."""
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

    child = intersect_child_policy(parent, requested)

    assert child.filesystem is FilesystemMode.CHECKOUT_RO
    assert child.builtins == frozenset({"read", "grep", "find", "ls", "bash"})
    assert child.skills == frozenset()
    assert child.network is NetworkMode.PROVIDER_RELAY
    assert child.subagent is False
