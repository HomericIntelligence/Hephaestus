"""Validated, private session bindings for resumable Pi automation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hephaestus.agents.execution_policy import AgentRole

_SCHEMA_VERSION = 1


class PiSessionBindingError(ValueError):
    """A Pi session binding or JSON event is malformed or mismatched."""


def _canonical_cwd(cwd: Path) -> str:
    """Return the canonical worktree identity used in persisted bindings."""
    return str(cwd.resolve())


def model_fingerprint(model: str) -> str:
    """Return a non-reversible model identity suitable for private state."""
    if not model:
        return ""
    return hashlib.sha256(model.encode("utf-8")).hexdigest()


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PiSessionBindingError(f"Pi session binding {field} must be a non-empty string")
    return value


@dataclass(frozen=True)
class AgentSessionBinding:
    """Versioned identity required to resume or compact a Pi session.

    The binding intentionally contains no provider/model alias or credentials.
    ``model_fingerprint`` lets the runtime reject a changed model without
    disclosing its private operator-local selection.
    """

    session_id: str
    canonical_cwd: str
    role: AgentRole
    model_fingerprint: str
    state_reference: str = ""
    provider: str = "pi"
    schema_version: int = _SCHEMA_VERSION

    def to_json(self) -> str:
        """Serialize this binding in a strict, versioned private-state form."""
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "provider": self.provider,
                "session_id": self.session_id,
                "canonical_cwd": self.canonical_cwd,
                "role": self.role.value,
                "model_fingerprint": self.model_fingerprint,
                "state_reference": self.state_reference,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> AgentSessionBinding:
        """Load and validate a persisted binding without accepting extra state."""
        try:
            payload: Any = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PiSessionBindingError("Pi session binding is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise PiSessionBindingError("Pi session binding must be a JSON object")
        expected = {
            "schema_version",
            "provider",
            "session_id",
            "canonical_cwd",
            "role",
            "model_fingerprint",
            "state_reference",
        }
        if set(payload) != expected:
            raise PiSessionBindingError("Pi session binding has an unsupported schema")
        if payload["schema_version"] != _SCHEMA_VERSION:
            raise PiSessionBindingError("Pi session binding schema version is unsupported")
        if payload["provider"] != "pi":
            raise PiSessionBindingError("Pi session binding provider must be 'pi'")
        try:
            role = AgentRole(_require_text(payload["role"], "role"))
        except ValueError as exc:
            raise PiSessionBindingError("Pi session binding role is invalid") from exc
        state_reference = payload["state_reference"]
        if not isinstance(state_reference, str):
            raise PiSessionBindingError("Pi session binding state_reference must be a string")
        return cls(
            session_id=_require_text(payload["session_id"], "session_id"),
            canonical_cwd=_require_text(payload["canonical_cwd"], "canonical_cwd"),
            role=role,
            model_fingerprint=_require_text(payload["model_fingerprint"], "model_fingerprint"),
            state_reference=state_reference,
        )


def create_pi_binding(
    *,
    session_id: str,
    cwd: Path,
    role: AgentRole,
    model: str,
    state_reference: str = "",
) -> AgentSessionBinding:
    """Create a binding only after a non-one-shot Pi session has succeeded."""
    return AgentSessionBinding(
        session_id=_require_text(session_id, "session_id"),
        canonical_cwd=_canonical_cwd(cwd),
        role=role,
        model_fingerprint=model_fingerprint(model),
        state_reference=state_reference,
    )


def validate_pi_binding(
    binding: AgentSessionBinding,
    *,
    cwd: Path,
    role: AgentRole,
    model: str,
) -> None:
    """Fail closed unless a resume has the original provider identity.

    Args:
        binding: Persisted binding returned by an earlier Pi invocation.
        cwd: Requested worktree path.
        role: Requested execution role.
        model: Operator-selected model, compared only by fingerprint.
    """
    if binding.provider != "pi" or binding.schema_version != _SCHEMA_VERSION:
        raise PiSessionBindingError("Pi session binding provider or schema is unsupported")
    if binding.canonical_cwd != _canonical_cwd(cwd):
        raise PiSessionBindingError("Pi session binding worktree does not match cwd")
    if binding.role is not role:
        raise PiSessionBindingError("Pi session binding role does not match request")
    if binding.model_fingerprint != model_fingerprint(model):
        raise PiSessionBindingError("Pi session binding model does not match request")


def parse_pi_session_event(line: str) -> str:
    """Extract one opaque session id from a strict Pi JSON event line."""
    try:
        event: Any = json.loads(line)
    except json.JSONDecodeError as exc:
        raise PiSessionBindingError("Pi session event is not valid JSON") from exc
    if not isinstance(event, dict) or set(event) != {"type", "id"}:
        raise PiSessionBindingError("Pi session event must contain only type and id")
    if event["type"] != "session":
        raise PiSessionBindingError("Pi event is not a session event")
    return _require_text(event["id"], "session event id")
