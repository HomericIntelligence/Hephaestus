"""Check private evidence against the current request and execution owner."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
from typing import Any

import pytest

pytestmark = pytest.mark.precommit


def read(worker, request_id="approval-1"):
    """Call the public helper after its implementation is available."""
    name = "hephaestus.automation.fleet_request_evidence"
    assert importlib.util.find_spec(name) is not None, "private request evidence is not implemented"
    return importlib.import_module(name).read_request_evidence(worker, "session-1", request_id)


class Worker:
    """Supply controlled inventory, pending requests, and provider history."""

    def __init__(self) -> None:
        self.session = {
            "workerId": "worker-1",
            "generation": 7,
            "sessionId": "session-1",
            "providerThreadId": "thread-1",
            "providerTurnId": "turn-1",
            "activity": "waiting_approval",
            "admissionReserved": True,
            "released": False,
            "outcome": None,
        }
        self.pending = {
            "approval-1": {
                "id": "approval-1",
                "method": "item/fileChange/requestApproval",
                "sessionId": "session-1",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "patch-1",
                    "startedAtMs": 123,
                    "reason": "private approval reason",
                },
            }
        }
        self.change = {
            "path": "/workspace/café.py",
            "kind": {"type": "update", "move_path": None},
            "diff": "-old\n+new\n",
        }
        self.item = {
            "id": "patch-1",
            "type": "fileChange",
            "status": "inProgress",
            "changes": [self.change],
        }
        self.turn = {"id": "turn-1", "status": "inProgress", "items": [self.item]}
        self.history = {
            "thread": {
                "id": "thread-1",
                "turns": [self.turn],
                "privateMetadata": "DO-NOT-RETURN-HISTORY",
            }
        }
        self.provider = self
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.after_read = lambda: None
        self.extra_sessions: list[dict[str, Any]] = []

    def inventory(self):
        return {
            "workerId": "worker-1",
            "generation": 7,
            "sessions": copy.deepcopy([self.session, *self.extra_sessions]),
        }

    def request(self, method, params):
        self.calls.append((method, params))
        self.after_read()
        return copy.deepcopy(self.history)


def test_current_file_change_returns_only_bounded_item_evidence():
    """Return the current patch and omit unrelated private fields."""
    worker = Worker()
    worker.item["privateOutput"] = "DO-NOT-RETURN-ITEM"
    worker.turn["items"].append({"id": "other", "type": "agentMessage", "text": "PRIVATE MESSAGE"})
    worker.change["extra"] = "DO-NOT-RETURN-CHANGE"
    expected_request = worker.pending["approval-1"]
    result = read(worker)
    assert worker.calls == [("thread/read", {"threadId": "thread-1", "includeTurns": True})]
    assert result == {
        "workerId": "worker-1",
        "generation": 7,
        "requestId": "approval-1",
        "sessionId": "session-1",
        "threadId": "thread-1",
        "turnId": "turn-1",
        "itemId": "patch-1",
        "requestFingerprint": hashlib.sha256(
            json.dumps(
                expected_request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest(),
        "evidence": {"changes": [{key: worker.change[key] for key in ("path", "kind", "diff")}]},
    }
    assert "PRIVATE" not in json.dumps(result)
    assert "private approval reason" not in json.dumps(result)


@pytest.mark.parametrize("request_id", [1, "1"])
def test_request_ids_preserve_their_json_type(request_id):
    """Keep integer and string request identifiers distinct."""
    worker = Worker()
    pending = worker.pending.pop("approval-1")
    pending["id"] = request_id
    worker.pending[request_id] = pending
    assert type(read(worker, request_id)["requestId"]) is type(request_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("providerTurnId", "old-turn"),
        ("providerThreadId", "other-thread"),
        ("activity", "idle"),
        ("outcome", "completed"),
        ("released", True),
        ("admissionReserved", False),
        ("stopCommandId", "stop-1"),
        ("workerId", "other-worker"),
        ("generation", 6),
    ],
)
def test_inactive_or_wrong_owner_does_not_read_history(field, value):
    """Reject a request before a provider read if its owner is not current."""
    worker = Worker()
    worker.session[field] = value
    with pytest.raises(ValueError, match="request_not_active"):
        read(worker)
    assert worker.calls == []


@pytest.mark.parametrize(
    "mode", ["missing", "wrong-session", "wrong-type", "bool-id", "duplicate-session"]
)
def test_invalid_pending_request_does_not_read_history(mode):
    """Read no history for a missing or invalid pending request."""
    worker = Worker()
    if mode == "missing":
        worker.pending.clear()
    elif mode == "wrong-session":
        worker.pending["approval-1"]["sessionId"] = "session-other"
    elif mode == "wrong-type":
        worker.pending["approval-1"]["method"] = "item/commandExecution/requestApproval"
    elif mode == "bool-id":
        worker.pending[True] = worker.pending.pop("approval-1")
    else:
        worker.extra_sessions.append(copy.deepcopy(worker.session))
    with pytest.raises(ValueError, match="request_not_active"):
        read(worker, True if mode == "bool-id" else "approval-1")
    assert worker.calls == []


@pytest.mark.parametrize("mode", ["request-gone", "request-changed", "turn-changed", "stopped"])
def test_provider_read_cannot_return_evidence_after_ownership_changes(mode):
    """Discard evidence if the request changes during the provider read."""
    worker = Worker()

    def mutate():
        if mode == "request-gone":
            worker.pending.clear()
        elif mode == "request-changed":
            worker.pending["approval-1"]["params"]["reason"] = "changed request"
        elif mode == "turn-changed":
            worker.session["providerTurnId"] = "turn-2"
        else:
            worker.session["stopCommandId"] = "stop-1"

    worker.after_read = mutate
    with pytest.raises(ValueError, match="request_not_active"):
        read(worker)


@pytest.mark.parametrize(
    "mode",
    [
        "thread",
        "turn",
        "duplicate-turn",
        "item",
        "duplicate-item",
        "type",
        "finished",
        "empty",
        "malformed",
    ],
)
def test_absent_or_ambiguous_history_never_invents_evidence(mode):
    """Require one current file-change item in the returned history."""
    worker = Worker()
    if mode == "thread":
        worker.history["thread"]["id"] = "thread-other"
    elif mode == "turn":
        worker.turn["id"] = "turn-other"
    elif mode == "duplicate-turn":
        worker.history["thread"]["turns"].append(copy.deepcopy(worker.turn))
    elif mode == "item":
        worker.item["id"] = "item-other"
    elif mode == "duplicate-item":
        worker.turn["items"].append(copy.deepcopy(worker.item))
    elif mode == "type":
        worker.item["type"] = "agentMessage"
    elif mode == "finished":
        worker.item["status"] = "completed"
    elif mode == "empty":
        worker.item["changes"] = []
    else:
        worker.history = {"thread": []}
    result = read(worker)
    assert result["error"] == "evidence_unavailable"
    assert "evidence" not in result
    assert "DO-NOT-RETURN" not in json.dumps(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("diff", ""),
        ("diff", "x" * (64 * 1024 + 1)),
        ("path", "x" * 4097),
        ("kind", {"type": "unknown"}),
    ],
)
def test_invalid_change_fields_fail_without_partial_evidence(field, value):
    """Reject incomplete or excessive changes without returning a partial patch."""
    worker = Worker()
    worker.change[field] = value
    assert read(worker)["error"] == "evidence_unavailable"


def test_total_evidence_limit_does_not_truncate_a_patch():
    """Return no evidence when the complete patch exceeds the response limit."""
    worker = Worker()
    worker.item["changes"] = [
        {"path": f"/workspace/{index}", "kind": {"type": "add"}, "diff": "x" * 65536}
        for index in range(3)
    ]
    result = read(worker)
    assert result["error"] == "evidence_unavailable"
    assert "evidence" not in result


def test_provider_error_does_not_disclose_private_error_text():
    """Convert provider errors to a fixed unavailable response."""
    worker = Worker()

    def fail():
        raise RuntimeError("DO-NOT-RETURN-PROVIDER-ERROR")

    worker.after_read = fail
    result = read(worker)
    assert result["error"] == "evidence_unavailable"
    assert "DO-NOT-RETURN" not in json.dumps(result)
