"""Strict provider-neutral receipts for resumable agent sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from hephaestus.agents.workspace import SourceLane, WorkspaceKind


class AgentSessionReceiptError(RuntimeError):
    """Raised when a session receipt is malformed or incompatible."""


_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "session_id",
        "role",
        "model_fingerprint",
        "repository_identity",
        "item_number",
        "lane",
        "canonical_path",
        "revision",
        "generation",
        "workspace_kind",
    }
)


@dataclass(frozen=True, slots=True)
class AgentSessionReceipt:
    """Bind a provider session to one workspace generation and revision."""

    provider: str
    session_id: str
    role: str
    model_fingerprint: str
    repository_identity: str
    item_number: int
    lane: SourceLane
    canonical_path: Path
    revision: str
    generation: int
    workspace_kind: WorkspaceKind
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-compatible receipt."""
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "session_id": self.session_id,
            "role": self.role,
            "model_fingerprint": self.model_fingerprint,
            "repository_identity": self.repository_identity,
            "item_number": self.item_number,
            "lane": self.lane.value,
            "canonical_path": str(self.canonical_path),
            "revision": self.revision,
            "generation": self.generation,
            "workspace_kind": self.workspace_kind.value,
        }

    def to_json(self) -> str:
        """Serialize the receipt deterministically."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> Self:
        """Parse a JSON receipt."""
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AgentSessionReceiptError(f"invalid session receipt JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AgentSessionReceiptError("session receipt must be a JSON object")
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """Parse a receipt, rejecting schema drift and unknown fields."""
        unknown = set(payload) - _FIELDS
        missing = _FIELDS - set(payload)
        if unknown:
            raise AgentSessionReceiptError(f"session receipt has unknown fields: {sorted(unknown)}")
        if missing:
            raise AgentSessionReceiptError(f"session receipt is missing fields: {sorted(missing)}")
        try:
            receipt = cls(
                schema_version=int(payload["schema_version"]),
                provider=str(payload["provider"]),
                session_id=str(payload["session_id"]),
                role=str(payload["role"]),
                model_fingerprint=str(payload["model_fingerprint"]),
                repository_identity=str(payload["repository_identity"]),
                item_number=int(payload["item_number"]),
                lane=SourceLane(payload["lane"]),
                canonical_path=Path(str(payload["canonical_path"])),
                revision=str(payload["revision"]),
                generation=int(payload["generation"]),
                workspace_kind=WorkspaceKind(payload["workspace_kind"]),
            )
        except (TypeError, ValueError) as exc:
            raise AgentSessionReceiptError(f"invalid session receipt: {exc}") from exc
        if receipt.schema_version != 1 or receipt.generation < 0:
            raise AgentSessionReceiptError("unsupported session receipt schema or generation")
        return receipt
