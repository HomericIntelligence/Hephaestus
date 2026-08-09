"""Pi session bindings preserve provider, role, model, and worktree identity."""

from __future__ import annotations

import pytest

from hephaestus.agents.execution_policy import AgentRole
from hephaestus.agents.pi_session import (
    PiSessionBindingError,
    create_pi_binding,
    parse_pi_session_event,
    validate_pi_binding,
)


def test_binding_round_trip_and_identity_validation(tmp_path) -> None:
    """A binding remains opaque while preserving its permitted resume identity."""
    binding = create_pi_binding(
        session_id="session-123",
        cwd=tmp_path,
        role=AgentRole.IMPLEMENTER,
        model="private-model-alias",
        state_reference="bindings/implementer.json",
    )

    restored = type(binding).from_json(binding.to_json())

    assert restored == binding
    assert "private-model-alias" not in binding.to_json()
    validate_pi_binding(restored, cwd=tmp_path, role=AgentRole.IMPLEMENTER, model="private-model-alias")


def test_binding_rejects_cross_worktree_and_role(tmp_path) -> None:
    """A raw Pi session identifier cannot be replayed under another identity."""
    binding = create_pi_binding(
        session_id="session-123",
        cwd=tmp_path,
        role=AgentRole.PLANNER,
        model="model",
    )

    with pytest.raises(PiSessionBindingError, match="worktree"):
        validate_pi_binding(binding, cwd=tmp_path / "other", role=AgentRole.PLANNER, model="model")
    with pytest.raises(PiSessionBindingError, match="role"):
        validate_pi_binding(binding, cwd=tmp_path, role=AgentRole.IMPLEMENTER, model="model")


def test_strict_session_event_rejects_malformed_payload() -> None:
    """Malformed or ambiguous JSON events fail before a session can be persisted."""
    assert parse_pi_session_event('{"type":"session","id":"abc"}') == "abc"
    with pytest.raises(PiSessionBindingError, match="session"):
        parse_pi_session_event('{"type":"session","id":""}')
    with pytest.raises(PiSessionBindingError, match="JSON"):
        parse_pi_session_event("not-json")
