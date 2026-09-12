"""Execute admitted Fleet commands with one private Codex runtime."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_environments import EnvironmentRegistry
from hephaestus.automation.fleet_isolation import (
    require_execution_platform,
    shell_environment_policy,
    validate_worker_storage,
)
from hephaestus.automation.fleet_journal import WorkerJournal as WorkerJournal, result_for
from hephaestus.automation.fleet_provider import (
    LIVE_ACTIVITY_METHODS,
    CodexAppServer,
    ProviderError,
)

_REQUESTS = {
    "item/commandExecution/requestApproval": "waiting_approval",
    "item/fileChange/requestApproval": "waiting_approval",
    "item/tool/requestUserInput": "waiting_input",
}
_TOOLS = {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "webSearch"}
_ACTIVITY_REFRESH_SECONDS = 5.0
_PENDING_REQUEST_MAX_RECORDS = 256
_PENDING_REQUEST_MAX_BYTES = 4 * 1024 * 1024
_SESSION_OPERATIONS = frozenset({"start", "input", "respond", "interrupt", "cancel", "resume"})


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("invalid_identity")
    return value


def _assignment_payload(command: dict[str, Any]) -> dict[str, Any]:
    payload = dict(command["payload"])
    for key in ("workspace", "agentId", "taskId", "sessionId", "executionId", "stage", "issueRefs"):
        if key in command:
            if key in payload and payload[key] != command[key]:
                raise ValueError("assignment_conflict")
            payload[key] = command[key]
    if payload.get("sessionId", command["targetId"]) != command["targetId"]:
        raise ValueError("assignment_conflict")
    return payload


def _reserved(session: dict[str, Any]) -> bool:
    return bool(session.get("admissionReserved", not session.get("released", False)))


def _current_turn(session: dict[str, Any], turn_id: Any) -> bool:
    return (
        isinstance(turn_id, str)
        and turn_id == session.get("providerTurnId")
        and session.get("outcome") is None
        and session["activity"] not in {"idle", "disconnected", "unknown"}
    )


class FleetWorker:
    """Bind controller assignments to independent conversations and workspaces."""

    def __init__(
        self,
        *,
        state_dir: Path,
        workspace_root: Path,
        codex_home: Path,
        worker_id: str,
        pool_id: str,
        host_id: str,
        generation: int,
        capacity: int,
        allocation_id: str | None = None,
        provider_command: list[str] | None = None,
        environment_registry: EnvironmentRegistry | None = None,
    ) -> None:
        """Open private receipts without starting a provider or admitting work."""
        if type(generation) is not int or generation < 1 or not 1 <= capacity <= 24:
            raise ValueError("invalid_worker_budget")
        self.workspace_root = workspace_root.resolve(strict=True)
        self.codex_home = codex_home.resolve(strict=True)
        if codex_home.is_symlink() or self.codex_home.stat().st_mode & 0o077:
            raise ValueError("codex_home_must_be_private")
        self.journal = WorkerJournal(state_dir)
        self.generation = generation
        self.capacity = capacity
        self.identity = {
            "workerId": _text(worker_id),
            "poolId": _text(pool_id),
            "hostId": _text(host_id),
            "allocationId": allocation_id,
            "generation": generation,
        }
        self.provider = CodexAppServer(provider_command or ["codex"], self.codex_home)
        self.environment_registry = environment_registry
        self.pending: dict[str | int, dict[str, Any]] = {}
        self._pending_bytes = 0
        self._pending_sizes: dict[str | int, int] = {}
        self.activity_clock: Callable[[], float] = time.monotonic
        self.storage_guard: Callable[[], None] = lambda: validate_worker_storage(
            self.codex_home, self.journal.directory, self.workspace_root
        )
        self.execution_guard: Callable[[], None] = lambda: require_execution_platform(sys.platform)
        self._activity_emitted: dict[str, float] = {}
        self._closed = False
        self._started = False

    def start(self) -> None:
        """Fence prior processes before acquiring a new provider runtime."""
        self.storage_guard()
        if self.journal.generation not in {0, self.generation}:
            raise RuntimeError("generation_change_requires_reconciliation")
        if self.journal.runtime_uncertain:
            raise RuntimeError("prior_provider_cleanup_requires_reconciliation")
        if self.journal.runtime_pid is not None:
            try:
                os.kill(self.journal.runtime_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("prior_provider_may_be_running")
        self.journal.append("generation", {"generation": self.generation})
        if self.environment_registry is not None:
            self.environment_registry.write_configuration()
        self.provider.start()
        process = self.provider.process
        if process is None:
            raise ProviderError("provider_not_started")
        self.journal.append("runtime", {"pid": process.pid})
        self._started = True
        for session in list(self.journal.sessions.values()):
            if not session.get("released", False):
                self._activity(session, "disconnected", "restart_requires_resume")

    def _validate(self, command: dict[str, Any]) -> None:
        if command.get("schema") != "hi/fleet/v1":
            raise ValueError("unsupported_schema")
        for key in ("commandId", "idempotencyKey", "targetId", "operation"):
            _text(command.get(key))
        if command.get("workerId") != self.identity["workerId"]:
            raise ValueError("wrong_worker")
        if type(command.get("generation")) is not int or command["generation"] != self.generation:
            raise ValueError("stale_generation")
        if command.get("targetKind") not in {"sessions", "workers"}:
            raise ValueError("unsupported_target_kind")
        operation = command.get("operation")
        if (operation == "drain" and command["targetKind"] != "workers") or (
            operation in _SESSION_OPERATIONS and command["targetKind"] != "sessions"
        ):
            raise ValueError("wrong_target_kind")
        if not isinstance(command.get("payload"), dict):
            raise ValueError("invalid_payload")

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        """Apply an admitted, idempotent command and return its durable receipt."""
        intent_created = False
        try:
            self._validate(command)
            self.poll()
            existing = self.journal.begin(command)
            if existing is not None:
                return existing
            intent_created = True
            result = self._execute(command)
        except ValueError as error:
            result = result_for(command, "failed", error=str(error))
        except ProviderError:
            result = result_for(command, "failed", error="provider_uncertain")
            for session in list(self.journal.sessions.values()):
                self._activity(session, "unknown", "provider_uncertain")
        if intent_created:
            self.journal.complete(command, result)
        return result

    def _execute(self, command: dict[str, Any]) -> dict[str, Any]:
        operation = command["operation"]
        if operation == "drain":
            self.journal.append("drain", {"draining": True})
            return result_for(command, "completed", draining=True)
        if operation == "start":
            return self._start_session(command)
        session = self.journal.sessions.get(command["targetId"])
        if session is None:
            raise ValueError("session_not_found")
        if session.get("released", False):
            raise ValueError("session_cancelled")
        if operation == "input":
            return self._input(command, session)
        if operation == "respond":
            return self._respond(command, session)
        if operation in {"interrupt", "cancel"}:
            return self._interrupt(command, session)
        if operation == "resume":
            return self._resume(command, session)
        raise ValueError("unsupported_operation")

    def _resume(self, command: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        self.execution_guard()
        if self.environment_registry is not None:
            self.environment_registry.parameters(session, "thread/resume")
        if session.get("backgroundCleanup") == "unconfirmed":
            raise ValueError("background_cleanup_requires_reconciliation")
        interrupted = session["activity"] == "idle" and session.get("outcome") == "interrupted"
        if not interrupted and session["activity"] not in {"disconnected", "unknown"}:
            raise ValueError("session_not_resumable")
        if not session.get("providerThreadId"):
            raise ValueError("thread_identity_unknown")
        if not _reserved(session):
            if self._reservation_count() >= self.capacity:
                raise ValueError("worker_capacity")
            session["admissionReserved"] = True
        session.pop("backgroundCleanup", None)
        self.journal.append("session", session)
        result = self.provider.request(
            "thread/resume",
            {
                **self._thread_parameters(session),
                "threadId": session["providerThreadId"],
                "excludeTurns": True,
            },
        )
        if result["thread"]["id"] != session["providerThreadId"]:
            raise ProviderError("resume_identity_mismatch")
        session.pop("stopCommandId", None)
        session.pop("stopOperation", None)
        session["outcome"] = None
        self._activity(session, "idle")
        return result_for(command, "completed", sessionId=session["sessionId"])

    def _thread_parameters(self, session: dict[str, Any]) -> dict[str, Any]:
        workspace = session["workspace"]
        profile = {
            "filesystem": {
                ":minimal": "read",
                workspace: "write",
                str(self.codex_home): "deny",
                str(self.journal.directory.resolve()): "deny",
            },
            "network": {"enabled": False},
        }
        return {
            **self._environment_parameters(session, "thread/start"),
            "cwd": workspace,
            "runtimeWorkspaceRoots": [workspace],
            "permissions": "fleet",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "config": {
                "permissions": {"fleet": profile},
                "agents": {"enabled": False},
                "features": {
                    "multi_agent": False,
                    "multi_agent_v2": False,
                    "shell_snapshot": False,
                },
                "shell_environment_policy": shell_environment_policy(Path(workspace)),
            },
        }

    def _environment_parameters(self, session: dict[str, Any], operation: str) -> dict[str, Any]:
        if self.environment_registry is None:
            return {}
        return self.environment_registry.parameters(session, operation)

    def _start_session(self, command: dict[str, Any]) -> dict[str, Any]:
        self.execution_guard()
        if self.journal.draining:
            raise ValueError("worker_draining")
        if command["targetId"] in self.journal.sessions:
            raise ValueError("session_exists")
        if self._reservation_count() >= self.capacity:
            raise ValueError("worker_capacity")
        payload = _assignment_payload(command)
        try:
            workspace = (self.workspace_root / _text(payload.get("workspace"))).resolve(strict=True)
        except OSError as error:
            raise ValueError("workspace_not_found") from error
        if workspace == self.workspace_root or not workspace.is_relative_to(self.workspace_root):
            raise ValueError("workspace_outside_root")
        if not workspace.is_dir():
            raise ValueError("workspace_not_directory")
        for existing in self.journal.sessions.values():
            if existing.get("released", False):
                continue
            other = Path(existing["workspace"])
            if workspace.is_relative_to(other) or other.is_relative_to(workspace):
                raise ValueError("workspace_owned")
        issue_refs = payload.get("issueRefs", [])
        if not isinstance(issue_refs, list) or len(issue_refs) > 100:
            raise ValueError("invalid_issue_refs")
        session = {
            **self.identity,
            "sessionId": command["targetId"],
            "workspace": str(workspace),
            **{
                key: _text(payload.get(key))
                for key in ("agentId", "taskId", "executionId", "stage")
            },
            "issueRefs": [_text(value) for value in issue_refs],
            "providerThreadId": None,
            "providerTurnId": None,
            "activity": "unknown",
            "waitingReason": None,
            "released": False,
            "admissionReserved": True,
            "outcome": None,
        }
        # A workspace reservation must survive a lost thread/start response.
        self.journal.append("session", session)
        result = self.provider.request(
            "thread/start", {**self._thread_parameters(session), "ephemeral": False}
        )
        session["providerThreadId"] = _text(result["thread"]["id"])
        self._activity(session, "idle")
        return result_for(
            command,
            "completed",
            sessionId=session["sessionId"],
            providerThreadId=session["providerThreadId"],
        )

    def _input(self, command: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        self.execution_guard()
        if self.journal.draining:
            raise ValueError("worker_draining")
        if not _reserved(session):
            raise ValueError("session_not_admitted")
        if session["activity"] in {"disconnected", "unknown", "waiting_approval", "waiting_input"}:
            raise ValueError("session_not_ready")
        text = command["payload"].get("text")
        if not isinstance(text, str) or not text or len(text) > 512 * 1024:
            raise ValueError("invalid_input")
        params = {
            "threadId": session["providerThreadId"],
            "input": [{"type": "text", "text": text}],
        }
        selection = self._environment_parameters(session, "turn/start")
        method = "turn/start"
        if session["activity"] != "idle":
            method = "turn/steer"
            params["expectedTurnId"] = session["providerTurnId"]
        else:
            params.update(selection)
        # Persist invalidation before a provider call can admit another turn.
        session.pop("backgroundCleanup", None)
        self.journal.append("session", session)
        result = self.provider.request(method, params)
        if method == "turn/start":
            session.pop("stopCommandId", None)
            session.pop("stopOperation", None)
            session["outcome"] = None
            session["providerTurnId"] = _text(result["turn"]["id"])
        self._activity(session, "model_working")
        return result_for(command, "completed", providerTurnId=session["providerTurnId"])

    def _respond(self, command: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        self.execution_guard()
        request_id = command["payload"].get("requestId")
        if not isinstance(request_id, (str, int)):
            raise ValueError("invalid_request_id")
        pending = self.pending.get(request_id)
        if pending is None or pending["sessionId"] != session["sessionId"]:
            raise ValueError("request_not_owned")
        if session["activity"] not in {"waiting_approval", "waiting_input"} or session.get(
            "stopCommandId"
        ):
            raise ValueError("request_not_active")
        if not _current_turn(session, pending["params"].get("turnId")):
            raise ValueError("request_not_active")
        response = command["payload"].get("response")
        if not isinstance(response, dict):
            raise ValueError("invalid_response")
        if pending["method"] != "item/tool/requestUserInput":
            if response.get("decision") not in {"accept", "decline", "cancel"}:
                raise ValueError("unsupported_approval_decision")
        elif not isinstance(response.get("answers"), dict):
            raise ValueError("invalid_answers")
        self.provider.respond(request_id, response)
        self._drop_pending(request_id)
        self._activity(session, "model_working")
        return result_for(command, "completed", requestId=request_id)

    def _interrupt(self, command: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        if command["operation"] == "cancel" and session["activity"] == "idle":
            return self._cancel_idle(command, session)
        if not session.get("providerTurnId") or session["activity"] == "idle":
            raise ValueError("no_active_turn")
        self.provider.request(
            "turn/interrupt",
            {
                "threadId": session["providerThreadId"],
                "turnId": session["providerTurnId"],
            },
        )
        session["stopCommandId"] = command["commandId"]
        session["stopOperation"] = command["operation"]
        self.journal.append("session", session)
        return result_for(command, "accepted", waitingReason="provider_stop_confirmation")

    def _cancel_idle(self, command: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        result = self.provider.request(
            "thread/read",
            {
                "threadId": session["providerThreadId"],
                "includeTurns": False,
            },
        )
        thread = result.get("thread", {})
        if (
            thread.get("id") != session["providerThreadId"]
            or thread.get("status", {}).get("type") != "idle"
        ):
            self._activity(session, "unknown", "provider_not_confirmed_idle")
            raise ValueError("provider_not_confirmed_idle")
        session["stopCommandId"] = command["commandId"]
        session["stopOperation"] = "cancel"
        self.journal.append("session", session)
        if not self._clean_background_terminals(session):
            raise ValueError("background_cleanup_unconfirmed")
        session["released"] = True
        session["admissionReserved"] = False
        session["outcome"] = "cancelled"
        self._activity(session, "idle", commandId=command["commandId"])
        return result_for(command, "completed", sessionId=session["sessionId"])

    def _clean_background_terminals(self, session: dict[str, Any]) -> bool:
        params = {"threadId": session["providerThreadId"]}
        deadline = time.monotonic() + 1.0
        try:
            self.provider.request("thread/backgroundTerminals/clean", params, timeout=1.0)
            # A clean response confirms submission only. Observe the real inventory.
            for _ in range(3):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                inventory = self.provider.request(
                    "thread/backgroundTerminals/list", params, timeout=remaining
                )
                if inventory.get("data") == [] and not inventory.get("nextCursor"):
                    session["backgroundCleanup"] = "confirmed_empty"
                    return True
                time.sleep(0.02)
        except ProviderError:
            pass
        session["backgroundCleanup"] = "unconfirmed"
        session["outcome"] = None
        self._activity(session, "unknown", "background_cleanup_unconfirmed")
        return False

    def _observe_background_inventory(self, session: dict[str, Any]) -> str | None:
        session["backgroundCleanup"] = "unconfirmed"
        try:
            inventory = self.provider.request(
                "thread/backgroundTerminals/list",
                {"threadId": session["providerThreadId"]},
                timeout=1.0,
            )
            if inventory.get("data") == [] and not inventory.get("nextCursor"):
                session["backgroundCleanup"] = "confirmed_empty"
                return None
        except ProviderError:
            pass
        return "background_cleanup_unconfirmed"

    def _activity(
        self, session: dict[str, Any], activity: str, reason: str | None = None, **details: Any
    ) -> None:
        session = {
            **session,
            "activity": activity,
            "waitingReason": reason,
            "observedAt": datetime.now(UTC).isoformat(),
        }
        self.journal.append("session", session)
        self._activity_emitted[session["sessionId"]] = self.activity_clock()
        fact = {
            key: value
            for key, value in session.items()
            if key not in {"workspace", "stopOperation"}
        }
        fact.update(details)
        sequence = len(self.journal.events) + 1
        self.journal.append(
            "event",
            {
                "schema": "hi/fleet/v1",
                "seq": sequence,
                "sourceSequence": sequence,
                "workerId": self.identity["workerId"],
                "generation": self.generation,
                "eventId": f"{self.identity['workerId']}:{self.generation}:{sequence}",
                "kind": "activity",
                "targetKind": "sessions",
                "targetId": session["sessionId"],
                "event": fact,
            },
        )

    def poll(self) -> None:
        """Route available protocol observations to their bound logical agent."""
        for message in self.provider.drain_notifications():
            self._notification(message)
        if self.provider.failed:
            for session in list(self.journal.sessions.values()):
                if session["activity"] != "unknown" and not session.get("released", False):
                    self._activity(session, "unknown", "provider_disconnected")

    def _notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params", {})
        thread_id = params.get("threadId") or params.get("thread", {}).get("id")
        session = next(
            (
                item
                for item in self.journal.sessions.values()
                if item["providerThreadId"] == thread_id
            ),
            None,
        )
        if session is None:
            if "id" in message:
                self.provider.reject(message["id"])
            return
        if session.get("released", False):
            if "id" in message:
                self.provider.reject(message["id"])
            return
        if "id" in message:
            self._server_request(message, session)
            return
        if method == "turn/started":
            if _current_turn(session, params.get("turn", {}).get("id")):
                self._activity(session, "model_working")
        elif method == "turn/completed":
            self._turn_completed(session, params)
        elif method in {"item/started", "item/completed"}:
            self._item_activity(session, method, params)
        elif method == "error":
            self._activity(session, "unknown", "provider_error")
        elif method in LIVE_ACTIVITY_METHODS:
            self._refresh_activity(session, params)

    def _item_activity(self, session: dict[str, Any], method: str, params: dict[str, Any]) -> None:
        if not _current_turn(session, params.get("turnId")):
            return
        item = params.get("item", {})
        activity = (
            "tool_running"
            if item.get("type") in _TOOLS and method == "item/started"
            else "model_working"
        )
        self._activity(session, activity, itemType=item.get("type"))

    def _refresh_activity(self, session: dict[str, Any], params: dict[str, Any]) -> None:
        if session["activity"] not in {"model_working", "tool_running"}:
            return
        if params.get("turnId") != session.get("providerTurnId"):
            return
        since = self.activity_clock() - self._activity_emitted.get(session["sessionId"], 0.0)
        if since >= _ACTIVITY_REFRESH_SECONDS:
            self._activity(session, session["activity"], session["waitingReason"])

    def _server_request(self, message: dict[str, Any], session: dict[str, Any]) -> None:
        method = message["method"]
        request_id = message["id"]
        params = message["params"]
        if (
            method not in _REQUESTS
            or not _current_turn(session, params.get("turnId"))
            or session.get("stopCommandId")
        ):
            self.provider.reject(request_id)
            return
        retained = {
            "id": request_id,
            "method": method,
            "params": params,
            "sessionId": session["sessionId"],
        }
        try:
            request_bytes = len(
                json.dumps(
                    retained,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            self.provider.reject(request_id)
            self._activity(session, "unknown", "provider_request_invalid")
            return
        if (
            request_id in self.pending
            or len(self.pending) >= _PENDING_REQUEST_MAX_RECORDS
            or self._pending_bytes + request_bytes > _PENDING_REQUEST_MAX_BYTES
        ):
            self.provider.reject(request_id)
            self._activity(session, "unknown", "provider_request_limit")
            return
        self.pending[request_id] = retained
        self._pending_sizes[request_id] = request_bytes
        self._pending_bytes += request_bytes
        self._activity(session, _REQUESTS[method], method, requestId=request_id)

    def _drop_pending(self, request_id: str | int) -> None:
        """Remove one pending request and release its memory-budget receipt."""
        del self.pending[request_id]
        self._pending_bytes -= self._pending_sizes.pop(request_id)

    def _turn_completed(self, session: dict[str, Any], params: dict[str, Any]) -> None:
        turn = params["turn"]
        if turn.get("id") != session.get("providerTurnId"):
            return
        status = turn.get("status")
        outcome = status if status in {"completed", "failed", "interrupted"} else "unknown"
        for request_id, pending in list(self.pending.items()):
            if pending["sessionId"] == session["sessionId"]:
                self._drop_pending(request_id)
        if (
            status == "interrupted"
            and session.get("stopCommandId")
            and not self._clean_background_terminals(session)
        ):
            return
        if status == "interrupted" and session.get("stopOperation") == "cancel":
            outcome = "cancelled"
            session["released"] = True
        if status == "interrupted" and session.get("stopCommandId"):
            session["admissionReserved"] = False
        session["outcome"] = outcome
        reason = None
        if status in {"completed", "failed"}:
            reason = self._observe_background_inventory(session)
        self._activity(
            session,
            "idle" if outcome != "unknown" else "unknown",
            reason,
            outcome=outcome,
            commandId=session.get("stopCommandId"),
        )

    def inventory(self) -> dict[str, Any]:
        """Report retained identities and current observed execution activity."""
        self.poll()
        return {
            **self.identity,
            "capacity": self.capacity,
            "draining": self.journal.draining,
            "activeReservations": self._reservation_count(),
            "sessions": list(self.journal.sessions.values()),
        }

    def _reservation_count(self) -> int:
        return sum(_reserved(item) for item in self.journal.sessions.values())

    def events(self, after: int, *, limit: int = 500) -> dict[str, Any]:
        """Return metadata events with a cursor in this worker's ordered journal."""
        self.poll()
        if type(after) is not int or after < 0 or after > len(self.journal.events):
            raise ValueError("invalid_event_cursor")
        if not 1 <= limit <= 500:
            raise ValueError("invalid_event_limit")
        events = self.journal.events[after : after + limit]
        return {"events": events, "cursor": events[-1]["seq"] if events else after}

    def close(self) -> None:
        """Stop the owned provider before releasing its journal writer."""
        if self._closed:
            return
        confirmed = False
        try:
            try:
                confirmed = self.provider.close()
            finally:
                if self._started:
                    self.journal.append("runtime", {"pid": None, "uncertain": not confirmed})
        finally:
            self.journal.close()
            self._closed = True
