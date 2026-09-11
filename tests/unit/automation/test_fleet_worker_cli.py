"""Route private evidence reads without dispatching an execution command."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.fleet_worker_cli import _dispatch
from tests.unit.automation.test_fleet_request_evidence import Worker

pytestmark = pytest.mark.precommit


class AttachedWorker(Worker):
    """Retain a visible fallback for an unsupported private operation."""

    def __init__(self) -> None:
        super().__init__()
        self.control_messages: list[dict[str, Any]] = []

    def handle(self, message):
        self.control_messages.append(message)
        return {"error": "unsupported_operation"}


def test_private_request_evidence_dispatch_reads_the_matching_patch():
    """Use the evidence helper for a private JSON request."""
    worker = AttachedWorker()
    result = _dispatch(
        worker,
        {"operation": "request-evidence", "targetId": "session-1", "requestId": "approval-1"},
    )
    assert result.get("evidence") == {"changes": [worker.change]}
    assert result["requestId"] == "approval-1"
    assert result["sessionId"] == "session-1"
    assert worker.calls == [("thread/read", {"threadId": "thread-1", "includeTurns": True})]
    assert worker.control_messages == []


@pytest.mark.parametrize("mode", ["wrong-session", "stale-request"])
def test_private_request_evidence_refuses_inactive_bindings(mode):
    """Read no history when the private request does not belong to the current turn."""
    worker = AttachedWorker()
    session_id = "session-1"
    if mode == "wrong-session":
        session_id = "other-session"
    else:
        worker.pending["approval-1"]["params"]["turnId"] = "finished-turn"
    with pytest.raises(ValueError, match="request_not_active"):
        _dispatch(
            worker,
            {"operation": "request-evidence", "targetId": session_id, "requestId": "approval-1"},
        )
    assert worker.calls == []
    assert worker.control_messages == []
