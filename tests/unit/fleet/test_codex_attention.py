"""Check the sanitized Codex observed-session contract and lifecycle."""

# ruff: noqa: D103

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import hephaestus.fleet.codex_attention_cli as attention_cli
from hephaestus.fleet.codex_attention import AttentionIdentity, CodexAttentionAdapter, sanitize_hook
from hephaestus.fleet.codex_attention_cli import (
    CodexAttentionServer,
    build_hooks_config,
    main,
    send_hook,
)

pytestmark = pytest.mark.precommit


@dataclass
class Clock:
    """Supply bounded wall and monotonic test time."""

    wall: float = datetime(2026, 11, 27, 2, 40, tzinfo=UTC).timestamp()
    monotonic: float = 100.0

    def time(self) -> float:
        return self.wall

    def tick(self, seconds: float = 0.0) -> float:
        self.monotonic += seconds
        self.wall += seconds
        return self.monotonic


def identity() -> AttentionIdentity:
    return AttentionIdentity(
        cluster="cluster-a",
        account_tier="admin",
        unix_user="service-admin",
        node="cn042",
        harness_version="0.172.0",
        slurm_job_id="88421",
        adapter_instance_id="adapter-1",
    )


def hook(name: str = "SessionStart", **extra: object) -> dict[str, object]:
    payload = {
        "session_id": "session-123",
        "hook_event_name": name,
        "transcript_path": "/private/transcript.jsonl",
        "cwd": "/private/worktree",
        "model": "private-model",
        **extra,
    }
    if name == "SessionStart":
        payload.setdefault("source", "startup")
    return payload


@pytest.fixture
def socket_tmp_path() -> Iterator[Path]:
    """Provide a short socket root even when pytest uses a long TMPDIR."""
    with TemporaryDirectory(prefix="codex-attention-", dir="/tmp") as directory:
        yield Path(directory)


def test_session_start_emits_exact_sanitized_contract() -> None:
    frame = CodexAttentionAdapter(identity(), clock=Clock().time).handle(
        hook(prompt="raw prompt", tool_input={"secret": "do not emit"})
    )
    observation = frame["observation"]
    interactive = observation["interactive"]

    assert frame["type"] == "observation"
    assert frame["subject"].startswith(
        "hi.fleet.observations.interactive.v1.cluster-a.admin.codex."
    )
    assert observation["source"] == "hephaestus"
    assert observation["target"] == "odysseus"
    assert observation["operation"] == "snapshot"
    assert observation["eventId"]
    assert observation["sessionId"] == "session-123"
    assert observation["agentId"] == "main"
    assert observation["host"] == "cn042"
    assert observation["observedAt"] == "2026-11-27T02:40:00.000Z"
    assert interactive == {
        "schema": "hi/fleet-interactive/v1",
        "cluster": "cluster-a",
        "accountTier": "admin",
        "unixUser": "service-admin",
        "harness": "codex",
        "harnessVersion": "0.172.0",
        "sessionToken": interactive["sessionToken"],
        "node": "cn042",
        "slurmJobId": "88421",
        "adapterInstanceId": "adapter-1",
        "sessionEpoch": interactive["sessionEpoch"],
        "sequence": 1,
        "activity": "idle",
        "since": "2026-11-27T02:40:00.000Z",
        "progressAt": "2026-11-27T02:40:00.000Z",
        "summary": "Codex session started",
        "summaryKind": "redacted-event",
        "sourceState": "session_start",
        "sourceHealth": "connected",
    }
    encoded = json.dumps(frame)
    for private in ("raw prompt", "do not emit", "transcript", "worktree", "private-model"):
        assert private not in encoded


@pytest.mark.parametrize(
    ("event", "extra", "activity", "source_state"),
    [
        ("UserPromptSubmit", {}, "running", "user_prompt_submit"),
        ("PreModelRequest", {}, "model_working", "pre_model_request"),
        ("PostModelRequest", {"status": "completed"}, "running", "post_model_request"),
        ("PostModelRequest", {"status": "failed"}, "failed", "post_model_request"),
        ("PostModelRequest", {"status": "cancelled"}, "interrupted", "post_model_request"),
        ("PreToolUse", {}, "tool_running", "pre_tool_use"),
        ("PostToolUse", {}, "running", "post_tool_use"),
        ("PermissionRequest", {}, "waiting_approval", "permission_request"),
        ("Stop", {}, "idle", "stop"),
        ("Interrupt", {}, "interrupted", "interrupt"),
        ("PreCompact", {}, "running", "pre_compact"),
        ("PostCompact", {}, "running", "post_compact"),
        ("SubagentStart", {"agent_id": "researcher"}, "running", "subagent_start"),
        ("SubagentStop", {"agent_id": "researcher"}, "idle", "subagent_stop"),
        ("SessionEnd", {"reason": "other"}, "disconnected", "session_end"),
    ],
)
def test_supported_lifecycle_mapping(
    event: str,
    extra: dict[str, object],
    activity: str,
    source_state: str,
) -> None:
    frame = CodexAttentionAdapter(identity(), clock=Clock().time).handle(hook(event, **extra))
    interactive = frame["observation"]["interactive"]
    assert interactive["activity"] == activity
    assert interactive["sourceState"] == source_state
    if event == "SessionEnd":
        assert interactive["summary"] == "Codex session ended"


def test_subagents_have_independent_identity_under_the_parent_session() -> None:
    adapter = CodexAttentionAdapter(identity(), clock=Clock().time)
    main = adapter.handle(hook("Stop"))
    child = adapter.handle(hook("SubagentStart", agent_id="researcher"))

    assert child["observation"]["sessionId"] == main["observation"]["sessionId"]
    assert child["observation"]["agentId"] == "researcher"
    assert (
        child["observation"]["interactive"]["sessionToken"]
        == main["observation"]["interactive"]["sessionToken"]
    )
    assert (
        child["observation"]["interactive"]["sessionEpoch"]
        != main["observation"]["interactive"]["sessionEpoch"]
    )


def test_heartbeat_preserves_progress_and_resume_creates_a_new_epoch() -> None:
    clock = Clock()
    adapter = CodexAttentionAdapter(
        identity(), clock=clock.time, monotonic=clock.tick, heartbeat_seconds=30
    )
    waiting = adapter.handle(hook("PermissionRequest"))
    clock.tick(29.9)
    assert adapter.due_heartbeats() == []

    clock.tick(0.1)
    heartbeat_frame = adapter.due_heartbeats()[0]
    heartbeat = heartbeat_frame["observation"]["interactive"]
    waiting_interactive = waiting["observation"]["interactive"]

    assert heartbeat["sequence"] == 2
    assert heartbeat["since"] == waiting_interactive["since"]
    assert heartbeat_frame["observation"]["observedAt"] != waiting["observation"]["observedAt"]
    assert heartbeat["progressAt"] == waiting_interactive["progressAt"]

    resumed = adapter.handle(hook("SessionStart", source="resume"))["observation"]["interactive"]
    assert resumed["sequence"] == 1
    assert resumed["sessionEpoch"] != waiting_interactive["sessionEpoch"]
    assert resumed["activity"] == "idle"


def test_state_is_bounded_and_invalid_payloads_are_rejected() -> None:
    adapter = CodexAttentionAdapter(identity(), max_sessions=1)
    adapter.handle(hook())
    with pytest.raises(ValueError, match="attention_session_capacity"):
        adapter.handle(hook(session_id="session-456"))
    with pytest.raises(ValueError, match="invalid_hook_event_name"):
        sanitize_hook(hook("UnknownEvent"))
    with pytest.raises(ValueError, match="session_id"):
        sanitize_hook({"hook_event_name": "Stop"})


def test_hooks_are_synchronous_bounded_and_never_control_codex() -> None:
    state_dir = Path("/private/allocation/attention")
    config = build_hooks_config(Path("/opt/hephaestus/bin/python"), state_dir)
    assert set(config) == {"hooks"}
    assert len(config["hooks"]) == 14
    for groups in config["hooks"].values():
        command = groups[0]["hooks"][0]
        assert command["type"] == "command"
        assert command["timeout"] in (2, 3)
        assert "async" not in command
        assert "hook" in command["command"]
        assert "--state-dir" in command["command"]
        assert str(state_dir) in command["command"]
        assert "continue" not in command


def test_socket_boundary_emits_sanitized_ndjson_and_cleans_its_socket(
    socket_tmp_path: Path,
) -> None:
    clock = Clock()
    adapter = CodexAttentionAdapter(identity(), clock=clock.time, monotonic=clock.tick)
    output = StringIO()
    errors = StringIO()
    with CodexAttentionServer(socket_tmp_path, adapter, output=output, errors=errors) as server:
        assert server.socket_path.stat().st_mode & 0o777 == 0o600
        worker = threading.Thread(target=server.handle_once)
        worker.start()
        acknowledgement = send_hook(
            server.socket_path,
            hook(
                "PermissionRequest",
                prompt="raw-secret",
                tool_input={"command": "private command"},
            ),
        )
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert acknowledgement == {"accepted": True}
        frame = json.loads(output.getvalue())
        assert frame["observation"]["interactive"]["activity"] == "waiting_approval"
        assert "raw-secret" not in output.getvalue()
        assert "private command" not in output.getvalue()
        assert errors.getvalue() == ""
        assert server.socket_path.exists()
    assert not server.socket_path.exists()


def test_invalid_socket_input_is_rejected_without_raw_error_content(
    socket_tmp_path: Path,
) -> None:
    output = StringIO()
    errors = StringIO()
    with CodexAttentionServer(
        socket_tmp_path, CodexAttentionAdapter(identity()), output=output, errors=errors
    ) as server:
        worker = threading.Thread(target=server.handle_once)
        worker.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(server.socket_path))
            client.sendall(
                (json.dumps({"hook_event_name": "Stop", "prompt": "raw-secret"}) + "\n").encode()
            )
            acknowledgement = json.loads(client.makefile("rb").readline())
        worker.join(timeout=2)
        assert acknowledgement == {"accepted": False, "error": "invalid_observation"}
        assert output.getvalue() == ""
        assert errors.getvalue() == "codex attention: invalid observation\n"
        assert "raw-secret" not in errors.getvalue()


def test_private_state_and_unavailable_hook_fail_without_codex_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        CodexAttentionServer(
            Path("relative"),
            CodexAttentionAdapter(identity()),
            output=StringIO(),
            errors=StringIO(),
        )
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="0700"):
        CodexAttentionServer(
            public, CodexAttentionAdapter(identity()), output=StringIO(), errors=StringIO()
        )

    stdout = StringIO()
    stderr = StringIO()
    result = main(
        ["hook", "--state-dir", str(tmp_path)],
        stdin=StringIO(json.dumps(hook(prompt="raw-secret"))),
        stdout=stdout,
        stderr=stderr,
    )
    assert result == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "codex attention: observation unavailable\n"


@pytest.mark.parametrize(
    "raw",
    [
        '{"session_id":"one","session_id":"two","hook_event_name":"Stop"}\n',
        '{"session_id":"one","hook_event_name":"Stop","metadata":NaN}\n',
        json.dumps(
            {
                "session_id": "one",
                "hook_event_name": "Stop",
                "nested": json.loads("[" * 40 + "]" * 40),
            }
        )
        + "\n",
    ],
)
def test_socket_rejects_ambiguous_json(socket_tmp_path: Path, raw: str) -> None:
    output = StringIO()
    errors = StringIO()
    with CodexAttentionServer(
        socket_tmp_path, CodexAttentionAdapter(identity()), output=output, errors=errors
    ) as server:
        worker = threading.Thread(target=server.handle_once)
        worker.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(server.socket_path))
            client.sendall(raw.encode())
            acknowledgement = json.loads(client.makefile("rb").readline())
        worker.join(timeout=2)
    assert acknowledgement == {"accepted": False, "error": "invalid_observation"}
    assert output.getvalue() == ""


def test_socket_path_has_a_portable_bound(tmp_path: Path) -> None:
    long_path = tmp_path / ("x" * 120)
    with pytest.raises(ValueError, match="socket path exceeds"):
        CodexAttentionServer(
            long_path, CodexAttentionAdapter(identity()), output=StringIO(), errors=StringIO()
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"session_id":"one","session_id":"two","hook_event_name":"Stop"}',
        '{"session_id":"one","hook_event_name":"Stop","metadata":NaN}',
        json.dumps(
            {
                "session_id": "one",
                "hook_event_name": "Stop",
                "nested": json.loads("[" * 40 + "]" * 40),
            }
        ),
    ],
)
def test_hook_input_rejects_ambiguous_json_before_transport(
    tmp_path: Path, raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_transport(*args: object, **kwargs: object) -> None:
        raise AssertionError("ambiguous input reached the transport")

    monkeypatch.setattr(attention_cli, "send_hook", fail_transport)
    stdout = StringIO()
    stderr = StringIO()
    assert (
        main(
            ["hook", "--state-dir", str(tmp_path)],
            stdin=StringIO(raw),
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "codex attention: observation unavailable\n"


def test_stalled_client_cannot_block_the_observer(socket_tmp_path: Path) -> None:
    output = StringIO()
    errors = StringIO()
    with CodexAttentionServer(
        socket_tmp_path, CodexAttentionAdapter(identity()), output=output, errors=errors
    ) as server:
        worker = threading.Thread(target=server.handle_once, daemon=True)
        worker.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(server.socket_path))
            client.sendall(b'{"session_id":')
            try:
                acknowledgement = json.loads(client.makefile("rb").readline())
            finally:
                client.shutdown(socket.SHUT_RDWR)
                worker.join(timeout=2)
        assert acknowledgement == {
            "accepted": False,
            "error": "invalid_observation",
        }
        assert not worker.is_alive()
        assert output.getvalue() == ""


def test_session_end_reaps_every_agent_and_rejects_late_events() -> None:
    clock = Clock()
    adapter = CodexAttentionAdapter(
        identity(), clock=clock.time, monotonic=clock.tick, max_sessions=2
    )
    adapter.handle(hook())
    adapter.handle(hook("SubagentStart", agent_id="researcher"))
    ended = adapter.handle(hook("SessionEnd"))

    assert ended["observation"]["interactive"]["activity"] == "disconnected"
    clock.tick(30)
    assert adapter.due_heartbeats() == []
    with pytest.raises(ValueError, match="codex_session_ended"):
        adapter.handle(hook("Stop"))
    with pytest.raises(ValueError, match="codex_session_ended"):
        adapter.handle(hook("SubagentStop", agent_id="researcher"))


def test_ended_sessions_release_capacity_for_new_work() -> None:
    adapter = CodexAttentionAdapter(identity(), max_sessions=1)
    adapter.handle(hook())
    adapter.handle(hook("SessionEnd"))
    started = adapter.handle(hook(session_id="session-456"))

    assert started["observation"]["sessionId"] == "session-456"
    assert started["observation"]["interactive"]["activity"] == "idle"


def test_inconclusive_socket_probe_preserves_the_existing_owner(
    socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = CodexAttentionAdapter(identity())
    with CodexAttentionServer(
        socket_tmp_path, adapter, output=StringIO(), errors=StringIO()
    ) as server:

        class TimeoutProbe:
            def settimeout(self, timeout: float) -> None:
                pass

            def connect(self, address: str) -> None:
                raise TimeoutError("probe timed out")

            def close(self) -> None:
                pass

        monkeypatch.setattr(
            socket,
            "socket",
            lambda *args: TimeoutProbe(),
        )
        with pytest.raises(ValueError, match="availability is uncertain"):
            attention_cli._remove_stale_socket(server.socket_path)
        assert server.socket_path.exists()


def test_session_end_reaps_a_child_without_allocating_main_state() -> None:
    clock = Clock()
    adapter = CodexAttentionAdapter(
        identity(), clock=clock.time, monotonic=clock.tick, max_sessions=1
    )
    adapter.handle(hook("SubagentStart", agent_id="researcher"))

    ended = adapter.handle(hook("SessionEnd"))

    assert ended["observation"]["interactive"]["activity"] == "disconnected"
    assert adapter.due_heartbeats() == []
    with pytest.raises(ValueError, match="codex_session_ended"):
        adapter.handle(hook("Stop", agent_id="researcher"))


def test_failed_restart_preserves_the_ended_session_tombstone() -> None:
    adapter = CodexAttentionAdapter(identity(), max_sessions=2)
    adapter.handle(hook())
    adapter.handle(hook("SessionEnd"))
    adapter.handle(hook(session_id="session-456"))
    adapter.handle(hook(session_id="session-789"))

    with pytest.raises(ValueError, match="attention_session_capacity"):
        adapter.handle(hook("SessionStart", source="resume"))

    adapter.handle(hook("SessionEnd", session_id="session-456"))
    with pytest.raises(ValueError, match="codex_session_ended"):
        adapter.handle(hook("Stop"))
