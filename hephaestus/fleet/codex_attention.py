"""Project sanitized Codex hook events into observed-session attention state."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}")
_SUBJECT_IDENTITY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_AGENT_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_HOOK_SOURCES = {"startup", "resume", "clear", "compact", "fork"}
_MODEL_STATUSES = {"completed", "failed", "cancelled"}
_ACTIVITY = {
    "SessionStart": "idle",
    "UserPromptSubmit": "running",
    "PreModelRequest": "model_working",
    "PreToolUse": "tool_running",
    "PostToolUse": "running",
    "PermissionRequest": "waiting_approval",
    "Stop": "idle",
    "Interrupt": "interrupted",
    "PreCompact": "running",
    "PostCompact": "running",
    "SubagentStart": "running",
    "SubagentStop": "idle",
    "SessionEnd": "disconnected",
}
_SOURCE_STATE = {name: re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() for name in _ACTIVITY}
SUPPORTED_HOOKS = frozenset((*_ACTIVITY, "PostModelRequest"))
_SUMMARY = {
    "SessionStart": "Codex session started",
    "UserPromptSubmit": "Codex received user input",
    "PreModelRequest": "Codex model request started",
    "PreToolUse": "Codex tool call started",
    "PostToolUse": "Codex tool call completed",
    "PermissionRequest": "Codex permission requested",
    "Stop": "Codex turn stopped; session is idle",
    "Interrupt": "Codex turn interrupted",
    "PreCompact": "Codex compaction started",
    "PostCompact": "Codex compaction completed",
    "SubagentStart": "Codex subagent started",
    "SubagentStop": "Codex subagent stopped",
    "SessionEnd": "Codex session ended",
}


def _bounded(value: Any, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"invalid_{name}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"invalid_{name}")
    return value


def _identifier(value: Any, name: str, *, agent: bool = False) -> str:
    pattern = _AGENT_IDENTIFIER if agent else _IDENTIFIER
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid_{name}")
    return value


def _timestamp(seconds: float) -> str:
    return (
        datetime.fromtimestamp(seconds, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class AttentionIdentity:
    """Identify the allocation-local observer without task ownership."""

    cluster: str
    account_tier: str
    unix_user: str
    node: str
    harness_version: str
    adapter_instance_id: str
    slurm_job_id: str | None = None

    def __post_init__(self) -> None:
        """Reject identities that cannot match the Odysseus subject contract."""
        if not _SUBJECT_IDENTITY.fullmatch(self.cluster):
            raise ValueError("invalid_cluster")
        if self.account_tier not in {"root", "admin", "user"}:
            raise ValueError("invalid_account_tier")
        _bounded(self.unix_user, "unix_user", 64)
        _identifier(self.node, "node")
        _bounded(self.harness_version, "harness_version", 64)
        _identifier(self.adapter_instance_id, "adapter_instance_id", agent=True)
        if self.slurm_job_id is not None:
            _bounded(self.slurm_job_id, "slurm_job_id", 64)


@dataclass
class _SessionState:
    session_id: str
    agent_id: str
    epoch: str
    sequence: int
    activity: str
    source_state: str
    since: str
    progress_at: str
    summary: str
    last_emitted: float


def sanitize_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only observation metadata from one Codex hook payload.

    Raw prompts, transcript paths, workspaces, model names, tool arguments and
    results, assistant messages, errors, and token usage never enter the result.
    """
    if not isinstance(payload, dict):
        raise ValueError("invalid_hook_payload")
    event = payload.get("hook_event_name")
    if event not in _ACTIVITY and event != "PostModelRequest":
        raise ValueError("invalid_hook_event_name")
    session_id = _identifier(payload.get("session_id"), "session_id")
    result: dict[str, Any] = {
        "session_id": session_id,
        "hook_event_name": event,
        "agent_id": _identifier(payload.get("agent_id", "main"), "agent_id", agent=True),
    }
    if event == "SessionStart":
        source = payload.get("source")
        if source not in _HOOK_SOURCES:
            raise ValueError("invalid_source")
        result["source"] = source
    if event == "PostModelRequest":
        status = payload.get("status")
        if status not in _MODEL_STATUSES:
            raise ValueError("invalid_status")
        result["status"] = status
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and _TOOL_NAME.fullmatch(tool_name):
        result["tool_name"] = tool_name
    return result


class CodexAttentionAdapter:
    """Own bounded in-memory state for observed Codex sessions."""

    def __init__(
        self,
        identity: AttentionIdentity,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        epoch_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        event_factory: Callable[[], str] = lambda: f"attention-{uuid.uuid4().hex}",
        heartbeat_seconds: float = 30.0,
        max_sessions: int = 256,
    ) -> None:
        """Initialize deterministic time, identity, heartbeat, and capacity seams."""
        if not 0 < heartbeat_seconds <= 300:
            raise ValueError("invalid_heartbeat_seconds")
        if isinstance(max_sessions, bool) or not 1 <= max_sessions <= 4096:
            raise ValueError("invalid_max_sessions")
        self.identity = identity
        self._clock = clock
        self._monotonic = monotonic
        self._epoch_factory = epoch_factory
        self._event_factory = event_factory
        self._heartbeat_seconds = heartbeat_seconds
        self._max_sessions = max_sessions
        self._sessions: dict[tuple[str, str], _SessionState] = {}
        self._ended_sessions: OrderedDict[str, None] = OrderedDict()

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one sanitized hook and return its immediate full-state frame."""
        event = sanitize_hook(payload)
        key = (event["session_id"], event["agent_id"])
        name = event["hook_event_name"]
        starts_epoch = name == "SessionStart" and event["source"] != "compact"
        state: _SessionState | None
        if name == "SessionEnd":
            if key[0] in self._ended_sessions:
                raise ValueError("codex_session_ended")
            state = self._sessions.get(key) or self._new_state(key)
        elif starts_epoch:
            has_session_state = any(session_id == key[0] for session_id, _ in self._sessions)
            if not has_session_state and len(self._sessions) >= self._max_sessions:
                raise ValueError("attention_session_capacity")
            replacement = self._new_state(key)
            self._remove_session_states(key[0])
            self._ended_sessions.pop(key[0], None)
            self._sessions[key] = replacement
            state = replacement
        else:
            if key[0] in self._ended_sessions:
                raise ValueError("codex_session_ended")
            state = self._sessions.get(key)
            if state is None:
                if len(self._sessions) >= self._max_sessions:
                    raise ValueError("attention_session_capacity")
                state = self._new_state(key)
                self._sessions[key] = state

        activity, source_state, summary = self._map_event(event)
        now = _timestamp(self._clock())
        if state.activity != activity:
            state.since = now
        state.activity = activity
        state.source_state = source_state
        state.progress_at = now
        state.summary = summary
        frame = self._emit(state)
        if name == "SessionEnd":
            self._remove_session_states(key[0])
            self._ended_sessions[key[0]] = None
            if len(self._ended_sessions) > self._max_sessions:
                self._ended_sessions.popitem(last=False)
        return frame

    def _new_state(self, key: tuple[str, str]) -> _SessionState:
        """Create state without changing active or ended-session ownership."""
        now = _timestamp(self._clock())
        return _SessionState(
            session_id=key[0],
            agent_id=key[1],
            epoch=_identifier(self._epoch_factory(), "session_epoch", agent=True),
            sequence=0,
            activity="idle",
            source_state="session_start",
            since=now,
            progress_at=now,
            summary=_SUMMARY["SessionStart"],
            last_emitted=self._monotonic(),
        )

    def _remove_session_states(self, session_id: str) -> None:
        """Remove all agent state owned by one containing Codex session."""
        for key in [key for key in self._sessions if key[0] == session_id]:
            del self._sessions[key]

    def due_heartbeats(self) -> list[dict[str, Any]]:
        """Emit full-state heartbeats that reached the monotonic deadline."""
        now = self._monotonic()
        return [
            self._emit(state)
            for state in self._sessions.values()
            if state.source_state != "session_end"
            and now - state.last_emitted >= self._heartbeat_seconds
        ]

    def _map_event(self, event: dict[str, Any]) -> tuple[str, str, str]:
        name = event["hook_event_name"]
        source_state = _SOURCE_STATE.get(name, "post_model_request")
        if name == "PostModelRequest":
            activity = {
                "completed": "running",
                "failed": "failed",
                "cancelled": "interrupted",
            }[event["status"]]
            summary = f"Codex model request {event['status']}"
        else:
            activity = _ACTIVITY[name]
            summary = _SUMMARY[name]
        if name in {"PreToolUse", "PermissionRequest"} and "tool_name" in event:
            summary = f"{summary} for {event['tool_name']}"
        elif name == "PreToolUse":
            summary = "Codex tool call started"
        return activity, source_state, summary

    def _emit(self, state: _SessionState) -> dict[str, Any]:
        state.sequence += 1
        state.last_emitted = self._monotonic()
        observed_at = _timestamp(self._clock())
        session_token = hashlib.sha256(state.session_id.encode()).hexdigest()[:24]
        interactive = {
            "schema": "hi/fleet-interactive/v1",
            "cluster": self.identity.cluster,
            "accountTier": self.identity.account_tier,
            "unixUser": self.identity.unix_user,
            "harness": "codex",
            "harnessVersion": self.identity.harness_version,
            "sessionToken": session_token,
            "node": self.identity.node,
            "adapterInstanceId": self.identity.adapter_instance_id,
            "sessionEpoch": state.epoch,
            "sequence": state.sequence,
            "activity": state.activity,
            "since": state.since,
            "progressAt": state.progress_at,
            "summary": state.summary,
            "summaryKind": "redacted-event",
            "sourceState": state.source_state,
            "sourceHealth": "connected",
        }
        if self.identity.slurm_job_id is not None:
            interactive["slurmJobId"] = self.identity.slurm_job_id
        return {
            "type": "observation",
            "subject": ".".join(
                (
                    "hi.fleet.observations.interactive.v1",
                    self.identity.cluster,
                    self.identity.account_tier,
                    "codex",
                    session_token,
                )
            ),
            "observation": {
                "source": "hephaestus",
                "target": "odysseus",
                "eventId": _identifier(self._event_factory(), "event_id"),
                "operation": "snapshot",
                "observedAt": observed_at,
                "sessionId": state.session_id,
                "agentId": state.agent_id,
                "host": self.identity.node,
                "interactive": interactive,
            },
        }
