"""Read bounded file-change evidence through the private worker attachment."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol, cast

_MAX_EVIDENCE = 128 * 1024
_MAX_HISTORY = 1024 * 1024
_METHOD = "item/fileChange/requestApproval"


class _Provider(Protocol):
    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class RequestEvidenceWorker(Protocol):
    """Supply current inventory and the private provider connection."""

    @property
    def provider(self) -> _Provider:
        """Return the private provider connection."""
        ...

    @property
    def pending(self) -> Mapping[str | int, dict[str, Any]]:
        """Return requests that still wait for a response."""
        ...

    def inventory(self) -> dict[str, Any]:
        """Drain observations and return current execution owners."""
        ...


def _json_bytes(value: Any, limit: int) -> bytes:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("evidence_limit")
    return encoded


def _text(value: Any, limit: int = 1024) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("invalid_evidence_field")
    if len(value.encode("utf-8")) > limit:
        raise ValueError("invalid_evidence_field")
    return value


def _one(values: Any, field: str, expected: Any) -> dict[str, Any]:
    if not isinstance(values, list) or len(values) > 4096:
        raise ValueError("invalid_evidence_collection")
    if any(not isinstance(value, dict) for value in values):
        raise ValueError("invalid_evidence_collection")
    matches = [value for value in values if value.get(field) == expected]
    if len(matches) != 1:
        raise ValueError("ambiguous_evidence")
    return cast(dict[str, Any], matches[0])


def _binding(
    worker: RequestEvidenceWorker, session_id: str, request_id: str | int
) -> dict[str, Any]:
    try:
        _text(session_id)
        if type(request_id) is str:
            _text(request_id, 256)
        elif type(request_id) is not int or abs(request_id) > 2**53 - 1:
            raise ValueError("invalid_request_id")
        # Inventory drains queued notifications before we inspect pending state.
        inventory = worker.inventory()
        worker_id = _text(inventory["workerId"])
        generation = inventory["generation"]
        if type(generation) is not int or not 1 <= generation <= 2**53 - 1:
            raise ValueError("invalid_generation")
        session = _one(inventory.get("sessions"), "sessionId", session_id)
        if (
            session.get("workerId") != worker_id
            or type(session.get("generation")) is not int
            or session["generation"] != generation
            or session.get("activity") != "waiting_approval"
            or session.get("outcome") is not None
            or session.get("released") is not False
            or session.get("admissionReserved") is not True
            or session.get("stopCommandId")
        ):
            raise ValueError("inactive_session")
        thread_id = _text(session.get("providerThreadId"))
        turn_id = _text(session.get("providerTurnId"))
        pending = worker.pending[request_id]
        if (
            type(pending.get("id")) is not type(request_id)
            or pending["id"] != request_id
            or pending.get("sessionId") != session_id
            or pending.get("method") != _METHOD
        ):
            raise ValueError("request_not_owned")
        canonical = _json_bytes(
            {key: pending[key] for key in ("id", "method", "params", "sessionId")},
            _MAX_EVIDENCE,
        )
        params = json.loads(canonical)["params"]
        if not isinstance(params, dict) or (
            params.get("threadId") != thread_id or params.get("turnId") != turn_id
        ):
            raise ValueError("request_not_owned")
        return {
            "workerId": worker_id,
            "generation": generation,
            "requestId": request_id,
            "sessionId": session_id,
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": _text(params.get("itemId")),
            "requestFingerprint": hashlib.sha256(canonical).hexdigest(),
        }
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise ValueError("request_not_active") from error


def _changes(history: dict[str, Any], binding: dict[str, Any]) -> list[dict[str, Any]]:
    # The transport already bounds frames. Also bound substitutes at this seam.
    _json_bytes(history, _MAX_HISTORY)
    thread = history.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != binding["threadId"]:
        raise ValueError("wrong_evidence_thread")
    turn = _one(thread.get("turns"), "id", binding["turnId"])
    item = _one(turn.get("items"), "id", binding["itemId"])
    if (
        turn.get("status") != "inProgress"
        or item.get("type") != "fileChange"
        or item.get("status") != "inProgress"
    ):
        raise ValueError("inactive_evidence_item")
    changes = item.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 64:
        raise ValueError("invalid_evidence_changes")
    selected = []
    paths = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("invalid_evidence_change")
        path = _text(change.get("path"), 4096)
        if path in paths:
            raise ValueError("ambiguous_evidence_path")
        paths.add(path)
        kind = change.get("kind")
        if not isinstance(kind, dict) or kind.get("type") not in {"add", "delete", "update"}:
            raise ValueError("invalid_evidence_kind")
        selected_kind = {"type": kind["type"]}
        if kind["type"] == "update" and "move_path" in kind:
            selected_kind["move_path"] = (
                None if kind["move_path"] is None else _text(kind["move_path"], 4096)
            )
        selected.append(
            {"path": path, "kind": selected_kind, "diff": _text(change.get("diff"), 64 * 1024)}
        )
    return selected


def read_request_evidence(
    worker: RequestEvidenceWorker, session_id: str, request_id: str | int
) -> dict[str, Any]:
    """Return one current patch without exposing other provider history.

    Missing or incomplete evidence returns ``evidence_unavailable``. A changed
    owner or request raises ``ValueError``. Neither result grants approval.
    """
    binding = _binding(worker, session_id, request_id)
    result = {**binding, "error": "evidence_unavailable"}
    try:
        history = worker.provider.request(
            "thread/read", {"threadId": binding["threadId"], "includeTurns": True}
        )
        candidate = {**binding, "evidence": {"changes": _changes(history, binding)}}
        _json_bytes(candidate, _MAX_EVIDENCE)
        result = candidate
    except (RuntimeError, OSError, TimeoutError, TypeError, ValueError, RecursionError):
        # Provider failures must not place private diagnostic text in the reply.
        pass
    if _binding(worker, session_id, request_id) != binding:
        raise ValueError("request_not_active")
    return result
