"""Pi session bindings preserve provider, role, model, and worktree identity."""

from __future__ import annotations

import pytest

from hephaestus.agents.execution_policy import AgentRole
from hephaestus.agents.pi_session import (
    AgentSessionBinding,
    PiSessionBindingError,
    create_pi_binding,
    model_fingerprint,
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
    validate_pi_binding(
        restored,
        cwd=tmp_path,
        role=AgentRole.IMPLEMENTER,
        model="private-model-alias",
    )


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


def test_binding_rejects_provider_schema_and_model_mismatches(tmp_path) -> None:
    """Resume validation binds provider schema and private model identity."""
    binding = create_pi_binding(
        session_id="session-123",
        cwd=tmp_path,
        role=AgentRole.PLANNER,
        model="model",
    )

    with pytest.raises(PiSessionBindingError, match="provider or schema"):
        validate_pi_binding(
            AgentSessionBinding(**{**binding.__dict__, "provider": "other"}),
            cwd=tmp_path,
            role=AgentRole.PLANNER,
            model="model",
        )
    with pytest.raises(PiSessionBindingError, match="model"):
        validate_pi_binding(binding, cwd=tmp_path, role=AgentRole.PLANNER, model="other-model")


def test_model_fingerprint_is_empty_only_for_an_empty_model() -> None:
    """Private model identity is deterministic without exposing the alias."""
    assert model_fingerprint("") == ""
    assert model_fingerprint("model") == model_fingerprint("model")
    assert model_fingerprint("model") != "model"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("[]", "JSON object"),
        ('{"schema_version": 1}', "unsupported schema"),
        (
            '{"schema_version":2,"provider":"pi","session_id":"s",'
            '"canonical_cwd":"/tmp","role":"planner",'
            '"model_fingerprint":"f","state_reference":""}',
            "schema version",
        ),
        (
            '{"schema_version":1,"provider":"other","session_id":"s",'
            '"canonical_cwd":"/tmp","role":"planner",'
            '"model_fingerprint":"f","state_reference":""}',
            "provider",
        ),
        (
            '{"schema_version":1,"provider":"pi","session_id":"s",'
            '"canonical_cwd":"/tmp","role":"unknown",'
            '"model_fingerprint":"f","state_reference":""}',
            "role is invalid",
        ),
        (
            '{"schema_version":1,"provider":"pi","session_id":"s",'
            '"canonical_cwd":"/tmp","role":"planner",'
            '"model_fingerprint":"f","state_reference":1}',
            "state_reference",
        ),
    ],
)
def test_binding_json_rejects_invalid_schema_fields(payload: str, message: str) -> None:
    """Persisted bindings fail closed on every versioned schema boundary."""
    with pytest.raises(PiSessionBindingError, match=message):
        AgentSessionBinding.from_json(payload)


def test_create_binding_rejects_blank_session_identity(tmp_path) -> None:
    """A blank provider session id cannot be persisted for resume."""
    with pytest.raises(PiSessionBindingError, match="session_id"):
        create_pi_binding(session_id=" ", cwd=tmp_path, role=AgentRole.PLANNER, model="model")


def test_strict_session_event_rejects_malformed_payload() -> None:
    """Malformed or ambiguous JSON events fail before a session can be persisted."""
    assert parse_pi_session_event('{"type":"session","id":"abc"}') == "abc"
    with pytest.raises(PiSessionBindingError, match="session"):
        parse_pi_session_event('{"type":"session","id":""}')
    with pytest.raises(PiSessionBindingError, match="JSON"):
        parse_pi_session_event("not-json")
