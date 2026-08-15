"""Tests for strict, provider-neutral agent session receipts."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.agents.session_receipt import (
    AgentSessionReceipt,
    AgentSessionReceiptError,
)
from hephaestus.agents.workspace import SourceLane, WorkspaceKind


def test_session_receipt_round_trip(tmp_path: Path) -> None:
    """All source identity survives deterministic serialization."""
    receipt = AgentSessionReceipt(
        provider="codex",
        session_id="session-1",
        role="planner",
        model_fingerprint="gpt:test",
        repository_identity="example/project:common-dir",
        item_number=42,
        lane=SourceLane.IMPLEMENTATION,
        canonical_path=tmp_path.resolve(),
        revision="a" * 40,
        generation=3,
        workspace_kind=WorkspaceKind.SOURCE,
    )

    assert AgentSessionReceipt.from_json(receipt.to_json()) == receipt


def test_session_receipt_rejects_unknown_fields(tmp_path: Path) -> None:
    """Receipt schema drift is rejected rather than ignored."""
    receipt = AgentSessionReceipt(
        provider="claude",
        session_id="session-1",
        role="reviewer",
        model_fingerprint="sonnet:test",
        repository_identity="example/project:common-dir",
        item_number=7,
        lane=SourceLane.REVIEW,
        canonical_path=tmp_path.resolve(),
        revision="b" * 40,
        generation=1,
        workspace_kind=WorkspaceKind.SOURCE,
    )
    payload = receipt.to_dict()
    payload["extra"] = "not allowed"

    with pytest.raises(AgentSessionReceiptError, match="unknown fields"):
        AgentSessionReceipt.from_dict(payload)
