"""Tests for provider-neutral agent runtime helpers."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.agents import runtime as agent_runtime
from hephaestus.agents.execution_policy import ExecutionPolicyError

PI_SMOKE_COMMAND_PREFIX = [
    "pi",
    "--mode",
    "json",
    "--print",
    "--no-session",
    "--no-approve",
    "--no-context-files",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--offline",
    "--no-tools",
]


@pytest.fixture
def private_pi_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate prompt storage and ACL commands when a test mocks Pi itself."""
    temp_dir = tmp_path / "private-pi-temp"
    temp_dir.mkdir(mode=0o700)
    monkeypatch.setattr(agent_runtime, "_prepare_pi_private_temp_dir", lambda: temp_dir)
    monkeypatch.setattr(agent_runtime, "_verify_pi_private_acl", lambda *_args, **_kwargs: None)
    return temp_dir


def test_pi_capability_contract_separates_native_packages_and_unsupported_controls() -> None:
    """Pi's runtime boundary must expose its fail-closed parity contract."""
    capabilities = agent_runtime.AGENT_CAPABILITIES["pi"]

    assert capabilities.core_capabilities == frozenset(
        {
            agent_runtime.AgentCapability.FILE_READ,
            agent_runtime.AgentCapability.FILE_WRITE,
            agent_runtime.AgentCapability.SHELL,
            agent_runtime.AgentCapability.SEARCH,
            agent_runtime.AgentCapability.SESSION,
            agent_runtime.AgentCapability.RESUME,
            agent_runtime.AgentCapability.SKILL,
            agent_runtime.AgentCapability.TOOL_ALLOWLIST,
        }
    )
    assert capabilities.package_capabilities == frozenset(
        {
            agent_runtime.AgentCapability.SUBAGENT,
            agent_runtime.AgentCapability.WEB_ACCESS,
        }
    )
    assert capabilities.unavailable_capabilities == frozenset(
        {
            agent_runtime.AgentCapability.INTERACTIVE_APPROVAL,
            agent_runtime.AgentCapability.OS_SANDBOX,
        }
    )
    assert capabilities.core_capabilities.isdisjoint(capabilities.package_capabilities)
    assert capabilities.core_capabilities.isdisjoint(capabilities.unavailable_capabilities)
    assert capabilities.package_capabilities.isdisjoint(capabilities.unavailable_capabilities)
    assert (
        capabilities.core_capabilities
        | capabilities.package_capabilities
        | capabilities.unavailable_capabilities
    ) == frozenset(agent_runtime.AgentCapability)
    assert capabilities.direct_runner is True
    assert capabilities.supports_approval is False
    assert capabilities.supports_sandbox is False
    assert capabilities.supports_sessions is True


def _write_pi_models_config(home: Path) -> None:
    """Create a minimal Pi model config under a fake home directory."""
    config_path = home / ".pi" / "agent" / "models.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"models": {"local-test": {}}}', encoding="utf-8")


def test_parse_codex_json_events_extracts_session_id_and_messages() -> None:
    """Codex JSONL exposes the resumable UUID in the session_meta event."""
    text = "\n".join(
        [
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}',
            '{"type":"agent_message","message":"first"}',
            '{"type":"agent_message","message":"second"}',
        ]
    )

    session_id, output = agent_runtime._parse_codex_json_events(text)

    assert session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert output == "first\nsecond"


def test_parse_codex_json_events_extracts_nested_agent_message() -> None:
    """Current Codex JSONL nests user-visible messages inside event_msg payloads."""
    text = "\n".join(
        [
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}',
            '{"type":"event_msg","payload":{"type":"agent_message","message":"nested"}}',
        ]
    )

    session_id, output = agent_runtime._parse_codex_json_events(text)

    assert session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert output == "nested"


def test_parse_pi_json_events_extracts_session_id_and_final_message() -> None:
    """Pi JSON mode starts with a session header and emits final assistant messages."""
    text = "\n".join(
        [
            '{"type":"session","version":3,"id":"pi-session-123","cwd":"/repo"}',
            (
                '{"type":"message_end","message":{"role":"assistant",'
                '"content":[{"type":"text","text":"final answer"}]}}'
            ),
        ]
    )

    session_id, output = agent_runtime._parse_pi_json_events(text)

    assert session_id == "pi-session-123"
    assert output == "final answer"


def test_parse_pi_json_events_prefers_turn_end_message() -> None:
    """The parser should handle the canonical turn_end event shape too."""
    text = "\n".join(
        [
            '{"type":"session","id":"pi-session-456"}',
            (
                '{"type":"turn_end","message":{"role":"assistant",'
                '"content":[{"type":"text","text":"turn answer"}]},'
                '"toolResults":[]}'
            ),
        ]
    )

    session_id, output = agent_runtime._parse_pi_json_events(text)

    assert session_id == "pi-session-456"
    assert output == "turn answer"


def test_parse_pi_json_events_keeps_final_message_once() -> None:
    """Pi may emit the same assistant response at multiple terminal event levels."""
    text = "\n".join(
        [
            '{"type":"session","id":"pi-session-456"}',
            (
                '{"type":"message_end","message":{"role":"assistant",'
                '"content":[{"type":"text","text":"draft answer"}]}}'
            ),
            (
                '{"type":"turn_end","message":{"role":"assistant",'
                '"content":[{"type":"text","text":"final answer"}]}}'
            ),
            (
                '{"type":"agent_end","messages":[{"role":"assistant",'
                '"content":[{"type":"text","text":"final answer"}]}]}'
            ),
        ]
    )

    session_id, output = agent_runtime._parse_pi_json_events(text)

    assert session_id == "pi-session-456"
    assert output == "final answer"


class _FakeCodexPopen:
    def __init__(
        self,
        cmd: list[str],
        *,
        proc_stdout: str,
        proc_stderr: str = "",
        final_message: str = "",
        hang: bool = False,
        returncode: int = 0,
        captured_input: list[str | None] | None = None,
        **_: Any,
    ) -> None:
        self.cmd = cmd
        self.stdout = proc_stdout
        self.stderr = proc_stderr
        self.hang = hang
        self.returncode = returncode
        self.killed = False
        self.terminated = False
        self._captured_input = captured_input
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text(final_message, encoding="utf-8")

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        if self._captured_input is not None:
            self._captured_input.append(input)
        del timeout
        if self.hang and not (self.killed or self.terminated):
            raise subprocess.TimeoutExpired(self.cmd, 1)
        return self.stdout, self.stderr

    def poll(self) -> int | None:
        if self.hang and not (self.killed or self.terminated):
            return None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_run_codex_session_returns_session_id_and_last_message(tmp_path: Path) -> None:
    """The runtime should prefer --output-last-message and preserve session id."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        stdout = (
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
            '{"type":"agent_message","message":"fallback"}\n'
        )
        return _FakeCodexPopen(cmd, proc_stdout=stdout, final_message="final answer", **kwargs)

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        result = agent_runtime.run_codex_session(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )

    assert result.session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert result.stdout == "final answer"


def test_run_codex_session_tracks_a_dedicated_process_group(tmp_path: Path) -> None:
    """An automation caller can reap the complete Codex process group on shutdown."""
    popen_kwargs: dict[str, Any] = {}
    tracker_events: list[tuple[str, int]] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        popen_kwargs.update(kwargs)
        proc = _FakeCodexPopen(cmd, proc_stdout="", final_message="done", **kwargs)
        proc.pid = 2468  # type: ignore[attr-defined]
        return proc

    @contextmanager
    def track_process_group(pid: int) -> Any:
        tracker_events.append(("enter", pid))
        try:
            yield
        finally:
            tracker_events.append(("exit", pid))

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        agent_runtime.run_codex_session(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
            process_tracker=track_process_group,
        )

    assert popen_kwargs["start_new_session"] is True
    assert tracker_events == [("enter", 2468), ("exit", 2468)]


def test_run_claude_text_strips_null_byte_from_stdin(tmp_path: Path) -> None:
    """#1661: a NUL in the prompt must not crash the Claude-text stdin path.

    subprocess.run marshals ``input=`` as text stdin and raises
    ``ValueError: embedded null byte`` on a stray NUL — the same crash the
    claude_invoke chokepoint guards against, on a sibling runner path.
    """
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("hephaestus.agents.runtime.subprocess.run", side_effect=fake_run):
        agent_runtime.run_claude_text("plan this\x00issue", cwd=tmp_path, timeout=30)

    assert captured["input"] == "plan thisissue"
    assert "\x00" not in captured["input"]


def test_run_codex_session_strips_null_byte_from_stdin(tmp_path: Path) -> None:
    """#1661: a NUL in the prompt must not crash the Codex stdin path."""
    captured_input: list[str | None] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        stdout = (
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
            '{"type":"agent_message","message":"fallback"}\n'
        )
        return _FakeCodexPopen(
            cmd,
            proc_stdout=stdout,
            final_message="final answer",
            captured_input=captured_input,
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        agent_runtime.run_codex_session(
            "plan this\x00issue",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )

    assert captured_input == ["plan thisissue"]


def test_run_codex_session_recovers_last_message_on_wrapper_timeout(tmp_path: Path) -> None:
    """If Codex writes the final answer but its wrapper hangs, keep the answer."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        stdout = (
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
            '{"type":"agent_message","message":"fallback"}\n'
        )
        return _FakeCodexPopen(
            cmd,
            proc_stdout=stdout,
            final_message="final answer",
            hang=True,
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch.dict("os.environ", {"HEPH_CODEX_FINAL_MESSAGE_GRACE": "0"}),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        result = agent_runtime.run_codex_session(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )

    assert result.session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert result.stdout == "final answer"
    assert "final message" in result.stderr


def test_run_codex_session_timeout_without_last_message_still_raises(tmp_path: Path) -> None:
    """A real Codex timeout with no completed message must still fail."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        return _FakeCodexPopen(cmd, proc_stdout="", final_message="", hang=True, **kwargs)

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            agent_runtime.run_codex_session(
                "prompt",
                cwd=tmp_path,
                timeout=1,
                sandbox="workspace-write",
            )


def test_run_codex_session_rejects_nested_sandbox_tool_failure_with_no_edits(
    tmp_path: Path,
) -> None:
    """A failed nested-worktree sandbox must not look like a no-edit success."""
    worktree = tmp_path / "repo" / "build" / ".worktrees" / "issue-2634"
    git_common_dir = tmp_path / "repo" / ".git"
    worktree.mkdir(parents=True)
    git_common_dir.mkdir(parents=True)
    captured_cmd: list[str] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        captured_cmd.extend(cmd)
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"codex-session"}',
                (
                    '{"type":"item.completed","item":{"id":"item_1",'
                    '"type":"command_execution","status":"failed","exit_code":-1,'
                    '"aggregated_output":"sandbox_apply: Operation not permitted"}}'
                ),
                '{"type":"turn.completed","usage":{}}',
            ]
        )
        return _FakeCodexPopen(
            cmd,
            proc_stdout=stdout,
            final_message="No edits were made.",
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch(
            "hephaestus.agents.runtime._codex_extra_writable_dirs",
            return_value=[git_common_dir],
        ),
        patch("subprocess.Popen", side_effect=fake_popen),
        pytest.raises(
            agent_runtime.AgentExecutionError,
            match="codex_nested_sandbox_unsupported",
        ),
    ):
        agent_runtime.run_codex_session(
            "implement",
            cwd=worktree,
            timeout=30,
            sandbox="workspace-write",
        )

    assert captured_cmd[captured_cmd.index("--cd") + 1] == str(worktree)
    assert captured_cmd[captured_cmd.index("--sandbox") + 1] == "workspace-write"
    assert captured_cmd[captured_cmd.index("--add-dir") + 1] == str(git_common_dir)
    assert "danger-full-access" not in captured_cmd


@pytest.mark.parametrize(
    ("event", "expected_detail"),
    [
        ({"type": "error", "message": "provider unavailable"}, "provider unavailable"),
        (
            {"type": "turn.failed", "error": {"message": "turn failed"}},
            "turn failed",
        ),
        (
            {
                "type": "item.completed",
                "item": {"id": "item_1", "type": "error", "message": "tool item failed"},
            },
            "tool item failed",
        ),
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "error",
                    "message": "Skill descriptions failed to load: invalid context budget",
                },
            },
            "Skill descriptions failed to load: invalid context budget",
        ),
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "error",
                    "message": (
                        "Skill descriptions were shortened to fit the invalid% skills context "
                        "budget."
                    ),
                },
            },
            "Skill descriptions were shortened to fit the invalid% skills context budget",
        ),
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "error",
                    "message": (
                        "Skill descriptions were shortened to fit the skills context budget "
                        "but could not be loaded."
                    ),
                },
            },
            "Skill descriptions were shortened to fit the skills context budget but could not",
        ),
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "error",
                    "message": (
                        "Skill descriptions were shortened to fit the skills context budget. "
                        "Skills could not be loaded."
                    ),
                },
            },
            "Skill descriptions were shortened to fit the skills context budget. Skills could",
        ),
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "file_change",
                    "status": "failed",
                    "changes": [],
                },
            },
            "file_change status=failed",
        ),
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "mcp_tool_call",
                    "status": "failed",
                    "error": {"message": "MCP unavailable"},
                },
            },
            "MCP unavailable",
        ),
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "collab_tool_call",
                    "status": "failed",
                },
            },
            "collab_tool_call status=failed",
        ),
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "web_search_call",
                    "status": "failed",
                },
            },
            "web_search_call status=failed",
        ),
    ],
)
def test_run_codex_session_rejects_structured_fatal_events(
    tmp_path: Path,
    event: dict[str, Any],
    expected_detail: str,
) -> None:
    """Terminal provider and non-shell tool events are explicit failures."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        return _FakeCodexPopen(
            cmd,
            proc_stdout=json.dumps(event),
            final_message="No edits were made.",
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
        pytest.raises(
            agent_runtime.AgentExecutionError,
            match=f"codex_tool_or_provider_failure: {expected_detail}",
        ),
    ):
        agent_runtime.run_codex_session(
            "implement",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )


def test_run_codex_session_rejects_nonzero_nested_sandbox_failure(tmp_path: Path) -> None:
    """A nonzero Codex process should retain the actionable sandbox diagnosis."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        return _FakeCodexPopen(
            cmd,
            proc_stdout="",
            proc_stderr="sandbox_apply: Operation not permitted",
            final_message="No edits were made.",
            returncode=1,
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
        pytest.raises(
            agent_runtime.AgentExecutionError,
            match="codex_nested_sandbox_unsupported",
        ),
    ):
        agent_runtime.run_codex_session(
            "implement",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )


def test_run_codex_session_rejects_failure_before_timeout_recovered_no_edits(
    tmp_path: Path,
) -> None:
    """A recovered final message cannot override an earlier fatal tool event."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "status": "failed",
                    "aggregated_output": "sandbox_apply: Operation not permitted",
                },
            }
        )
        return _FakeCodexPopen(
            cmd,
            proc_stdout=stdout,
            final_message="No edits were made.",
            hang=True,
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch.dict("os.environ", {"HEPH_CODEX_FINAL_MESSAGE_GRACE": "0"}),
        patch("subprocess.Popen", side_effect=fake_popen),
        pytest.raises(
            agent_runtime.AgentExecutionError,
            match="codex_nested_sandbox_unsupported",
        ),
    ):
        agent_runtime.run_codex_session(
            "implement",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )


def test_run_codex_session_allows_recovered_command_failure(tmp_path: Path) -> None:
    """A task command may fail before the agent successfully completes its work."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "command_execution",
                            "status": "failed",
                            "exit_code": 1,
                            "aggregated_output": "one test failed",
                        },
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        return _FakeCodexPopen(
            cmd,
            proc_stdout=stdout,
            final_message="Implemented the fix after correcting the test.",
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        result = agent_runtime.run_codex_session(
            "implement",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )

    assert result.stdout == "Implemented the fix after correcting the test."


def test_run_codex_session_allows_app_server_stream_lag_after_successful_edits(
    tmp_path: Path,
) -> None:
    """A nonfatal app-server lag notice does not override turn completion."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "codex-session"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "file_change",
                            "status": "completed",
                            "changes": [{"path": "fixed.py", "kind": "update"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_2",
                            "type": "error",
                            "message": (
                                "in-process app-server event stream lagged; dropped 17 events"
                            ),
                        },
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        return _FakeCodexPopen(
            cmd,
            proc_stdout=stdout,
            final_message="Implemented and verified the fix.",
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        result = agent_runtime.run_codex_session(
            "implement",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )

    assert result.stdout == "Implemented and verified the fix."


@pytest.mark.parametrize(
    "notice",
    [
        (
            "Skill descriptions were shortened to fit the 2% skills context budget. "
            "Codex can still see every skill, but some descriptions are shorter. "
            "Disable unused skills or plugins to leave more room for the rest."
        ),
        (
            "Skill descriptions were shortened to fit the 7.5% skills context budget. "
            "Some descriptions use compact summaries."
        ),
        (
            "Skill descriptions were shortened to fit the skills context budget. "
            "Codex can still see every skill, but some descriptions are shorter. "
            "Disable unused skills or plugins to leave more room for the rest."
        ),
    ],
)
def test_run_codex_session_allows_skills_budget_notice_after_successful_turn(
    tmp_path: Path,
    notice: str,
) -> None:
    """Skills-budget format and guidance changes do not discard final output."""

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "codex-session"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "error",
                            "message": notice,
                        },
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        return _FakeCodexPopen(
            cmd,
            proc_stdout=stdout,
            final_message='{"grade":"A","summary":"Reviewed.","comments":[]}',
            **kwargs,
        )

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        result = agent_runtime.run_codex_session(
            "review",
            cwd=tmp_path,
            timeout=30,
            sandbox="read-only",
        )

    assert result.stdout == '{"grade":"A","summary":"Reviewed.","comments":[]}'


def test_codex_approval_args_uses_config_override_for_current_cli() -> None:
    """Current Codex exposes approval policy through -c config overrides."""
    help_text = """
Options:
  -c, --config <key=value>
          Override a configuration value from config.toml.
"""

    with patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["codex"], 0, stdout=help_text, stderr=""),
    ):
        assert agent_runtime.codex_approval_args("never") == [
            "-c",
            'approval_policy="never"',
        ]


def test_codex_approval_args_preserves_legacy_flag() -> None:
    """Older Codex CLIs with a native flag should keep using it."""
    help_text = "Options:\n      --approval-policy <APPROVAL>\n"

    with patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["codex"], 0, stdout=help_text, stderr=""),
    ):
        assert agent_runtime.codex_approval_args("never") == [
            "--approval-policy",
            "never",
        ]


@pytest.mark.parametrize(
    ("model", "expected_model", "expected_reasoning"),
    [
        ("claude-fable-5", "gpt-5.5", "xhigh"),
        ("claude-opus-4-7", "gpt-5.5", "xhigh"),
        ("claude-sonnet-4-6", "gpt-5.5", "medium"),
        ("sol", "gpt-5.6-sol", "xhigh"),
        ("terra", "gpt-5.6-terra", "xhigh"),
        ("luna", "gpt-5.6-luna", "medium"),
        ("gpt-5.6-sol", "gpt-5.6-sol", "xhigh"),
        ("gpt-5.6-terra", "gpt-5.6-terra", "xhigh"),
        ("gpt-5.6-luna", "gpt-5.6-luna", "medium"),
    ],
)
def test_codex_base_cmd_maps_claude_reasoning_tiers(
    tmp_path: Path,
    model: str,
    expected_model: str,
    expected_reasoning: str,
) -> None:
    """Codex must receive recognized tier IDs plus matching reasoning config."""
    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        cmd = agent_runtime._codex_base_cmd(cwd=tmp_path, model=model)

    assert cmd[cmd.index("--model") + 1] == expected_model
    assert cmd[cmd.index("-c") + 1] == (f"model_reasoning_effort={json.dumps(expected_reasoning)}")


def test_codex_base_cmd_maps_haiku_to_mini_without_reasoning_override(
    tmp_path: Path,
) -> None:
    """Haiku-tier Codex work should use GPT-5.4-Mini without forcing reasoning."""
    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        cmd = agent_runtime._codex_base_cmd(cwd=tmp_path, model="claude-haiku-4-5")

    assert cmd[cmd.index("--model") + 1] == "gpt-5.4-mini"
    assert "model_reasoning_effort" not in cmd


@pytest.mark.parametrize("model", ["terra:default", "gpt-5.6-terra:default"])
def test_codex_base_cmd_allows_terra_default_reasoning(model: str, tmp_path: Path) -> None:
    """An explicit default suffix must omit the Codex reasoning override."""
    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        cmd = agent_runtime._codex_base_cmd(cwd=tmp_path, model=model)

    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-terra"
    assert "model_reasoning_effort" not in cmd


@pytest.mark.parametrize(
    ("model", "expected_model", "reasoning_effort", "expected_reasoning"),
    [
        ("sol", "gpt-5.6-sol", "medium", "medium"),
        ("gpt-5.6-terra", "gpt-5.6-terra", "xhigh", "xhigh"),
        ("gpt-5.6-luna", "gpt-5.6-luna", "default", ""),
        ("gpt-5.6", "gpt-5.6", "default", ""),
        ("gpt-5.6", "gpt-5.6", "high", "high"),
    ],
)
def test_codex_base_cmd_honors_explicit_reasoning_override(
    model: str,
    expected_model: str,
    reasoning_effort: str,
    expected_reasoning: str,
    tmp_path: Path,
) -> None:
    """Per-role transport settings override a tier alias's default reasoning."""
    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        cmd = agent_runtime._codex_base_cmd(cwd=tmp_path, model=f"{model}:{reasoning_effort}")

    assert cmd[cmd.index("--model") + 1] == expected_model
    reasoning_args = [arg for arg in cmd if arg.startswith("model_reasoning_effort=")]
    assert bool(reasoning_args) is bool(expected_reasoning)
    if expected_reasoning:
        assert reasoning_args == [f"model_reasoning_effort={json.dumps(expected_reasoning)}"]


@pytest.mark.parametrize(
    "native_model",
    ["gpt-5.4-mini", "gpt-5.5", "gpt-5.6"],
)
def test_codex_base_cmd_keeps_native_codex_model_ids(
    tmp_path: Path,
    native_model: str,
) -> None:
    """Explicit native Codex model overrides should still pass through unchanged."""
    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        cmd = agent_runtime._codex_base_cmd(cwd=tmp_path, model=native_model)

    assert cmd[cmd.index("--model") + 1] == native_model
    assert "model_reasoning_effort" not in cmd


def test_codex_base_cmd_defaults_new_sessions_to_gpt_55_xhigh(tmp_path: Path) -> None:
    """A fresh Codex session should not depend on the operator's CLI default."""
    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        cmd = agent_runtime._codex_base_cmd(cwd=tmp_path)

    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="xhigh"'


def test_run_codex_session_does_not_inherit_parent_thread_id(tmp_path: Path) -> None:
    """Automation child sessions must not inherit the interactive Codex thread."""
    captured_env: dict[str, str] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        captured_env.update(kwargs["env"])
        stdout = (
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
            '{"type":"agent_message","message":"fallback"}\n'
        )
        return _FakeCodexPopen(cmd, proc_stdout=stdout, final_message="final answer", **kwargs)

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime._codex_extra_writable_dirs", return_value=[]),
        patch.dict("os.environ", {"CODEX_THREAD_ID": "parent-thread"}, clear=False),
        patch("subprocess.Popen", side_effect=fake_popen),
    ):
        agent_runtime.run_codex_session(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            sandbox="workspace-write",
        )

    assert "CODEX_THREAD_ID" not in captured_env
    assert captured_env["CODEX_HOME"]


def test_codex_base_cmd_adds_git_common_dir_for_worktree_metadata(tmp_path: Path) -> None:
    """Codex worktree sessions need write access to the main clone's git dir."""
    worktree = tmp_path / "repo" / "build" / ".worktrees" / "issue-1"
    git_common_dir = tmp_path / "repo" / ".git"
    worktree.mkdir(parents=True)
    git_common_dir.mkdir(parents=True)

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd == ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"]
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{git_common_dir}\n", stderr="")

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime.subprocess.run", side_effect=fake_run),
    ):
        cmd = agent_runtime._codex_base_cmd(cwd=worktree)

    assert "--add-dir" in cmd
    add_dir_index = cmd.index("--add-dir")
    assert cmd[add_dir_index + 1] == str(git_common_dir)


def test_codex_base_cmd_does_not_add_git_common_dir_for_read_only(
    tmp_path: Path,
) -> None:
    """Read-only Codex sessions must not receive writable git metadata roots."""
    worktree = tmp_path / "repo" / "build" / ".worktrees" / "issue-1"
    worktree.mkdir(parents=True)

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime.subprocess.run") as run_mock,
    ):
        cmd = agent_runtime._codex_base_cmd(cwd=worktree, sandbox="read-only")

    assert "--add-dir" not in cmd
    run_mock.assert_not_called()


def test_codex_base_cmd_omits_add_dir_when_git_common_dir_is_inside_cwd(
    tmp_path: Path,
) -> None:
    """Normal checkouts already have their git dir inside the writable root."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=".git\n", stderr="")

    with (
        patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]),
        patch("hephaestus.agents.runtime.subprocess.run", side_effect=fake_run),
    ):
        cmd = agent_runtime._codex_base_cmd(cwd=repo)

    assert "--add-dir" not in cmd


def test_codex_base_cmd_resume_without_model_preserves_session_model() -> None:
    """Resume should not force the default model unless a model is requested."""
    cmd = agent_runtime._codex_base_cmd(resume_id="session-123", sandbox=None)

    assert cmd == [
        "codex",
        "exec",
        "resume",
        "session-123",
        "-c",
        'approval_policy="never"',
        "--json",
    ]


def test_resume_codex_session_uses_exec_resume(tmp_path: Path) -> None:
    """Codex feedback loops must resume the captured non-interactive session."""
    captured_cmd: list[str] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        captured_cmd.extend(cmd)
        stdout = '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
        return _FakeCodexPopen(cmd, proc_stdout=stdout, final_message="resumed", **kwargs)

    with patch("subprocess.Popen", side_effect=fake_popen):
        result = agent_runtime.resume_codex_session(
            "019e1e57-7652-7892-b1ca-c31c93d4b160",
            "feedback",
            cwd=tmp_path,
            timeout=1,
        )

    assert captured_cmd[:4] == [
        "codex",
        "exec",
        "resume",
        "019e1e57-7652-7892-b1ca-c31c93d4b160",
    ]
    assert result.stdout == "resumed"
    assert result.session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"


def test_resume_codex_session_applies_the_requested_sandbox_and_approval(tmp_path: Path) -> None:
    """A resumed read-only session must override permissive local defaults."""
    captured_cmd: list[str] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeCodexPopen:
        captured_cmd.extend(cmd)
        stdout = '{"type":"session_meta","payload":{"id":"session-123"}}\\n'
        return _FakeCodexPopen(cmd, proc_stdout=stdout, final_message="resumed", **kwargs)

    with patch("subprocess.Popen", side_effect=fake_popen):
        agent_runtime.resume_codex_session(
            "session-123",
            "review again",
            cwd=tmp_path,
            timeout=1,
            sandbox="read-only",
            approval="never",
        )

    assert 'sandbox_mode="read-only"' in captured_cmd
    assert 'approval_policy="never"' in captured_cmd


def test_run_pi_session_rejects_unadmitted_execution(tmp_path: Path) -> None:
    """The legacy public Pi session runner cannot bypass scoped dispatch."""
    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            ["pi", "--mode", "json"],
            0,
            stdout='{"type":"session","id":"pi-session-789"}',
            stderr="",
        )
        with pytest.raises(agent_runtime.AgentExecutionError, match="Unscoped run_pi_session"):
            agent_runtime.run_pi_session("prompt", cwd=tmp_path, timeout=30)

    run.assert_not_called()


def test_run_pi_text_rejects_unadmitted_execution(tmp_path: Path) -> None:
    """The legacy public Pi text runner cannot bypass scoped dispatch."""
    with patch("hephaestus.agents.runtime._run_pi_command") as run:
        with pytest.raises(agent_runtime.AgentExecutionError, match="Unscoped run_pi_text"):
            agent_runtime.run_pi_text("prompt", cwd=tmp_path, timeout=30)

    run.assert_not_called()


def test_private_pi_helpers_reject_unadmitted_execution(tmp_path: Path) -> None:
    """Reflective private-helper access cannot bypass the Pi admission boundary."""
    with (
        patch("subprocess.run") as run,
        patch(
            "hephaestus.agents.runtime._require_pi_automation_admission",
            side_effect=RuntimeError("Pi automation preflight is unavailable"),
        ),
    ):
        with pytest.raises(RuntimeError, match="Pi automation preflight is unavailable"):
            agent_runtime._invoke_pi_session(
                "prompt",
                cwd=tmp_path,
                timeout=30,
                model="",
                sandbox="no-tools",
            )
        with pytest.raises(RuntimeError, match="Pi automation preflight is unavailable"):
            agent_runtime._run_pi_command(
                ["pi", "--mode", "json"],
                prompt="prompt",
                cwd=tmp_path,
                timeout=30,
                sandbox="no-tools",
            )

    run.assert_not_called()


def test_resume_pi_session_rejects_unadmitted_execution(tmp_path: Path) -> None:
    """The legacy public Pi resume runner cannot bypass scoped dispatch."""
    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            ["pi", "--mode", "json", "--session", "pi-session-789"],
            0,
            stdout='{"type":"session","id":"pi-session-789"}',
            stderr="",
        )
        with pytest.raises(agent_runtime.AgentExecutionError, match="Unscoped resume_pi_session"):
            agent_runtime.resume_pi_session(
                "pi-session-789",
                "prompt",
                cwd=tmp_path,
                timeout=30,
            )

    run.assert_not_called()


def test_run_pi_smoke_session_is_noninteractive_and_tool_free(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """The only unadmitted Pi seam is fixed, ephemeral, and tool-free."""
    captured_cmd: list[str] = []
    stdout = "\n".join(
        [
            '{"type":"session","id":"pi-session-789"}',
            '{"type":"message_end","message":{"role":"assistant","content":"OK"}}',
        ]
    )

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=stdout,
            stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.run_pi_smoke_session(
            "smoke prompt",
            cwd=tmp_path,
            timeout=30,
            model="local-alias",
        )

    assert captured_cmd[:-1] == PI_SMOKE_COMMAND_PREFIX
    assert result.session_id is None
    assert result.stdout == "OK"


def test_run_pi_smoke_session_accepts_sessionless_terminal_response(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """The ``--no-session`` smoke seam must not require a session header."""
    captured_cmd: list[str] = []
    stdout = '{"type":"message_end","message":{"role":"assistant","content":"OK"}}'

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.run_pi_smoke_session(
            "smoke prompt",
            cwd=tmp_path,
            timeout=30,
        )

    assert captured_cmd[:-1] == PI_SMOKE_COMMAND_PREFIX
    assert result.session_id is None
    assert result.stdout == "OK"


def test_run_pi_smoke_session_redacts_generated_session_from_diagnostics(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """A generated session id must not escape through a smoke result payload."""
    session_id = "pi-session-789"
    stdout = "\n".join(
        [
            f'{{"type":"session","id":"{session_id}"}}',
            (
                '{"type":"message_end","message":{"role":"assistant","content":'
                f'"completed {session_id}"}}}}'
            ),
        ]
    )

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=session_id)

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.run_pi_smoke_session(
            "smoke prompt",
            cwd=tmp_path,
            timeout=30,
        )

    assert result.session_id is None
    assert session_id not in result.stdout
    assert session_id not in result.stderr
    assert agent_runtime.PI_PRIVATE_REDACTION in result.stdout
    assert agent_runtime.PI_PRIVATE_REDACTION in result.stderr


def test_run_pi_smoke_session_redacts_every_observed_session_from_diagnostics(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """A multi-event response must not retain an earlier generated session id."""
    first_session_id = "pi-session-first"
    final_session_id = "pi-session-final"
    stdout = "\n".join(
        [
            f'{{"type":"session","id":"{first_session_id}"}}',
            f'{{"type":"session","id":"{final_session_id}"}}',
            (
                '{"type":"message_end","message":{"role":"assistant","content":'
                f'"completed {first_session_id} and {final_session_id}"}}}}'
            ),
        ]
    )

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=stdout,
            stderr=f"diagnostics {first_session_id} and {final_session_id}",
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.run_pi_smoke_session(
            "smoke prompt",
            cwd=tmp_path,
            timeout=30,
        )

    assert result.session_id is None
    assert first_session_id not in result.stdout
    assert first_session_id not in result.stderr
    assert final_session_id not in result.stdout
    assert final_session_id not in result.stderr
    assert result.stdout.count(agent_runtime.PI_PRIVATE_REDACTION) == 2
    assert result.stderr.count(agent_runtime.PI_PRIVATE_REDACTION) == 2


def test_run_pi_smoke_session_uses_json_mode_without_retaining_session(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """The constrained smoke seam must discard an ephemeral Pi session id."""
    captured: dict[str, Any] = {}
    stdout = "\n".join(
        [
            '{"type":"session","id":"pi-session-789"}',
            '{"type":"message_end","message":{"role":"assistant","content":"pi output"}}',
        ]
    )

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        prompt_arg = next(arg for arg in cmd if arg.startswith("@"))
        prompt_path = Path(prompt_arg[1:])
        captured["prompt_text"] = prompt_path.read_text(encoding="utf-8")
        captured["prompt_mode"] = prompt_path.stat().st_mode & 0o777
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with (
        patch.dict("os.environ", {"HEPH_PI_MODEL": ""}),
        patch("subprocess.run", side_effect=fake_run),
    ):
        result = agent_runtime.run_pi_smoke_session(
            "private prompt content",
            cwd=tmp_path,
            timeout=30,
            model="private-alias",
        )

    assert result.session_id is None
    assert result.stdout == "pi output"
    assert captured["cmd"][:-1] == PI_SMOKE_COMMAND_PREFIX
    assert captured["cmd"][-1].startswith("@")
    assert "--model" not in captured["cmd"]
    assert "private-alias" not in captured["cmd"]
    assert "private prompt content" not in captured["cmd"]
    assert captured["prompt_text"] == "private prompt content"
    assert captured["prompt_mode"] == 0o600
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["timeout"] == 30
    assert captured["kwargs"]["check"] is True
    assert "HEPH_PI_MODEL" not in captured["kwargs"]["env"]
    assert "private-alias" not in captured["kwargs"]["env"].values()
    assert captured["kwargs"]["env"]["PI_TELEMETRY"] == "0"
    assert captured["kwargs"]["env"]["PI_SKIP_VERSION_CHECK"] == "1"


def test_run_pi_smoke_session_detaches_parent_stdin(tmp_path: Path) -> None:
    """The fixed smoke prompt must not absorb data piped to its parent process."""
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"type":"message_end","message":{"role":"assistant","content":"OK"}}',
            stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.run_pi_smoke_session(
            "smoke prompt",
            cwd=tmp_path,
            timeout=30,
        )

    assert result.stdout == "OK"
    assert captured["kwargs"].get("stdin") is subprocess.DEVNULL


def test_run_pi_smoke_session_removes_prompt_after_write_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A prompt encoding failure must not strand its private temporary file."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    with pytest.raises(UnicodeEncodeError):
        agent_runtime.run_pi_smoke_session("\ud800", cwd=tmp_path, timeout=30)

    assert list(tmp_path.rglob("pi-prompt-*.md")) == []


def test_run_pi_smoke_session_rejects_an_unsafe_temp_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ambient temporary roots writable by peers cannot host Pi prompt files."""
    unsafe_root = tmp_path / "unsafe-temp"
    unsafe_root.mkdir()
    unsafe_root.chmod(0o777)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(unsafe_root))

    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            ["pi"],
            0,
            stdout='{"type":"message_end","message":{"role":"assistant","content":"OK"}}',
            stderr="",
        )
        with pytest.raises(OSError, match="writable by another user"):
            agent_runtime.run_pi_smoke_session("smoke prompt", cwd=tmp_path, timeout=30)

    run.assert_not_called()


def test_run_pi_smoke_session_uses_an_isolated_private_temp_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pi and its prompt file share a fresh owner-only temporary directory."""
    ambient_temp = tmp_path / "ambient-temp"
    ambient_temp.mkdir()
    ambient_temp.chmod(0o700)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(ambient_temp))
    monkeypatch.setattr(agent_runtime, "_verify_pi_private_acl", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TMPDIR", str(ambient_temp))
    monkeypatch.setenv("TMP", str(ambient_temp))
    monkeypatch.setenv("TEMP", str(ambient_temp))
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        prompt_path = Path(next(arg[1:] for arg in cmd if arg.startswith("@")))
        captured["prompt_path"] = prompt_path
        captured["env"] = kwargs["env"]
        captured["temp_mode"] = stat.S_IMODE(prompt_path.parent.stat().st_mode)
        (prompt_path.parent / "pi-created-temp").write_text("temporary", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"type":"message_end","message":{"role":"assistant","content":"OK"}}',
            stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.run_pi_smoke_session("smoke prompt", cwd=tmp_path, timeout=30)

    private_temp = Path(captured["env"]["TMPDIR"])
    assert result.stdout == "OK"
    assert private_temp != ambient_temp
    assert private_temp.parent.parent == ambient_temp
    assert captured["env"]["TMP"] == str(private_temp)
    assert captured["env"]["TEMP"] == str(private_temp)
    assert captured["prompt_path"].parent == private_temp
    assert captured["temp_mode"] == 0o700
    assert not private_temp.exists()


def test_pi_child_environment_excludes_ambient_credentials_and_forces_privacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi receives only the explicit runtime environment, never ambient credentials."""
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-actions-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("PI_TELEMETRY", "1")
    monkeypatch.setenv("PI_SKIP_VERSION_CHECK", "0")
    monkeypatch.setenv("HEPH_PI_PROVIDER", "private-provider-alias")
    monkeypatch.setenv("HEPH_PI_MODEL", "private-model-alias")
    monkeypatch.setenv("TMPDIR", "/untrusted-temp")
    monkeypatch.setenv("TMP", "/untrusted-temp")
    monkeypatch.setenv("TEMP", "/untrusted-temp")

    env = agent_runtime._pi_env(model="private-model-alias")

    assert env["PI_TELEMETRY"] == "0"
    assert env["PI_SKIP_VERSION_CHECK"] == "1"
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "HEPH_PI_PROVIDER",
        "HEPH_PI_MODEL",
        "TMPDIR",
        "TMP",
        "TEMP",
    ):
        assert name not in env


@pytest.mark.parametrize(
    ("stdout", "error"),
    [
        ("not Pi JSON output", "JSON event"),
        ("{}", "JSON event"),
        ('{"type":"session","id":"pi-session-789"}', "terminal assistant JSON event"),
        ('{"type":"error","message":"provider failed"}', "terminal assistant JSON event"),
        ('{"type":"message_end","message":null}', "terminal assistant JSON event"),
        ('{"type":"message_end","message":{"content":"OK"}}', "terminal assistant JSON event"),
    ],
)
def test_run_pi_smoke_session_rejects_incomplete_event_stdout(
    tmp_path: Path,
    stdout: str,
    error: str,
) -> None:
    """The smoke seam must receive a terminal assistant response, not arbitrary JSON."""

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with (
        patch.dict("os.environ", {"HEPH_PI_MODEL": ""}),
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(RuntimeError, match=error),
    ):
        agent_runtime.run_pi_smoke_session(
            "smoke prompt",
            cwd=tmp_path,
            timeout=30,
        )


def _assert_pi_exception_chain_is_redacted(exc: BaseException) -> None:
    """Structured exception chains must not retain unredacted Pi diagnostics."""
    assert exc.__cause__ is None
    for chained in (exc.__cause__, exc.__context__):
        if chained is None:
            continue
        diagnostics = " ".join(
            [
                str(chained),
                repr(chained.args),
                *(
                    str(getattr(chained, attribute, ""))
                    for attribute in ("cmd", "output", "stdout", "stderr")
                ),
            ]
        )
        for private_value in (
            "private-provider-alias",
            "private-test-alias",
            "PRIVATE_ENDPOINT_TOKEN",
            "private-session-id",
        ):
            assert private_value not in diagnostics


def test_run_pi_smoke_session_redacts_private_values_from_failures(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """Pi subprocess failure diagnostics should not leak local aliases or tokens."""
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\nprivate-session-id\n",
        encoding="utf-8",
    )

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            7,
            [*cmd, "private-session-id"],
            output="private-provider-alias private-test-alias PRIVATE_ENDPOINT_TOKEN",
            stderr="private-provider-alias private-test-alias PRIVATE_ENDPOINT_TOKEN",
        )

    with (
        patch.dict(
            "os.environ",
            {
                "HEPH_PI_PROVIDER": "private-provider-alias",
                "HEPH_PI_MODEL": "private-test-alias",
            },
        ),
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(subprocess.CalledProcessError) as exc_info,
    ):
        agent_runtime.run_pi_smoke_session(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            model="private-test-alias",
        )

    exc = exc_info.value
    assert "private-provider-alias" not in str(exc.cmd)
    assert "private-test-alias" not in str(exc.cmd)
    assert "private-provider-alias" not in (exc.stdout or "")
    assert "private-test-alias" not in (exc.stdout or "")
    assert "private-provider-alias" not in (exc.stderr or "")
    assert "private-test-alias" not in (exc.stderr or "")
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.stdout or "")
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.stderr or "")
    assert "private-session-id" not in str(exc.cmd)
    assert agent_runtime.PI_PRIVATE_REDACTION in (exc.stderr or "")
    _assert_pi_exception_chain_is_redacted(exc)


def test_run_pi_smoke_session_redacts_private_values_from_timeouts(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """Pi timeout diagnostics should redact cmd, partial stdout, and stderr."""
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\nprivate-session-id\n",
        encoding="utf-8",
    )

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            [*cmd, "private-session-id"],
            7,
            output="private-provider-alias private-test-alias PRIVATE_ENDPOINT_TOKEN",
            stderr="PRIVATE_ENDPOINT_TOKEN private-test-alias private-provider-alias",
        )

    with (
        patch.dict(
            "os.environ",
            {
                "HEPH_PI_PROVIDER": "private-provider-alias",
                "HEPH_PI_MODEL": "private-test-alias",
            },
        ),
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(subprocess.TimeoutExpired) as exc_info,
    ):
        agent_runtime.run_pi_smoke_session(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            model="private-test-alias",
        )

    exc = exc_info.value
    assert "private-provider-alias" not in str(exc)
    assert "private-test-alias" not in str(exc)
    assert "private-provider-alias" not in str(exc.cmd)
    assert "private-test-alias" not in str(exc.cmd)
    assert "private-provider-alias" not in (exc.output or "")
    assert "private-test-alias" not in (exc.output or "")
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.output or "")
    assert "private-provider-alias" not in (exc.stdout or "")
    assert "private-test-alias" not in (exc.stdout or "")
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.stdout or "")
    assert "private-provider-alias" not in (exc.stderr or "")
    assert "private-test-alias" not in (exc.stderr or "")
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.stderr or "")
    assert "private-session-id" not in str(exc.cmd)
    assert agent_runtime.PI_PRIVATE_REDACTION in (exc.output or "")
    assert agent_runtime.PI_PRIVATE_REDACTION in (exc.stderr or "")
    _assert_pi_exception_chain_is_redacted(exc)


def test_run_pi_smoke_session_redacts_generated_session_from_nonzero_failure(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """A failed smoke command must not disclose a just-created Pi session id."""
    session_id = "generated-pi-session-id"
    partial_stdout = f'{{"type":"session","id":"{session_id}"}}\n{{"type":"error"}}'

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            7,
            cmd,
            output=partial_stdout,
            stderr=f"provider failed after {session_id}",
        )

    with (
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(subprocess.CalledProcessError) as exc_info,
    ):
        agent_runtime.run_pi_smoke_session("prompt", cwd=tmp_path, timeout=30)

    diagnostics = " ".join(
        str(value)
        for value in (
            exc_info.value,
            exc_info.value.args,
            exc_info.value.cmd,
            exc_info.value.output,
            exc_info.value.stderr,
        )
    )
    assert session_id not in diagnostics
    assert agent_runtime.PI_PRIVATE_REDACTION in diagnostics


def test_run_pi_smoke_session_redacts_generated_session_from_timeout(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """A timed-out smoke command must not disclose a just-created Pi session id."""
    session_id = "generated-pi-session-id"
    partial_stdout = f'{{"type":"session","id":"{session_id}"}}\n{{"type":"error"}}'

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd,
            7,
            output=partial_stdout,
            stderr=f"provider timed out after {session_id}",
        )

    with (
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(subprocess.TimeoutExpired) as exc_info,
    ):
        agent_runtime.run_pi_smoke_session("prompt", cwd=tmp_path, timeout=30)

    diagnostics = " ".join(
        str(value)
        for value in (
            exc_info.value,
            exc_info.value.args,
            exc_info.value.cmd,
            exc_info.value.output,
            exc_info.value.stderr,
        )
    )
    assert session_id not in diagnostics
    assert agent_runtime.PI_PRIVATE_REDACTION in diagnostics


def test_pi_private_redaction_tokens_merge_project_and_local_denylists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The committed policy and local policy must both protect Pi diagnostics."""
    monkeypatch.delenv("HEPH_PI_PROVIDER", raising=False)
    monkeypatch.delenv("HEPH_PI_MODEL", raising=False)
    (tmp_path / ".heph-project-denylist").write_text(
        "PROJECT_DENYLIST_TOKEN\nSHARED_DENYLIST_TOKEN\n",
        encoding="utf-8",
    )
    (tmp_path / ".heph-private-denylist").write_text(
        "LOCAL_DENYLIST_TOKEN\nSHARED_DENYLIST_TOKEN\n",
        encoding="utf-8",
    )

    tokens = agent_runtime.pi_private_redaction_tokens(tmp_path)

    assert "PROJECT_DENYLIST_TOKEN" in tokens
    assert "LOCAL_DENYLIST_TOKEN" in tokens
    assert tokens.count("SHARED_DENYLIST_TOKEN") == 1


def test_pi_private_redaction_tokens_fail_closed_on_broken_policy_link(tmp_path: Path) -> None:
    """A configured-but-unreadable privacy policy cannot silently disable redaction."""
    (tmp_path / ".heph-project-denylist").symlink_to("missing-policy-file")

    with pytest.raises(OSError, match="not a regular file"):
        agent_runtime.pi_private_redaction_tokens(tmp_path, require_readable=True)


def test_prepare_pi_private_log_dir_creates_unique_owner_only_run_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each smoke run must receive a distinct private directory below its safe root."""
    assert hasattr(agent_runtime, "prepare_pi_private_log_dir")
    verify_calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        agent_runtime,
        "_verify_pi_private_acl",
        lambda path, *, clear: verify_calls.append((path, clear)),
    )
    root = tmp_path / "logs"

    first = agent_runtime.prepare_pi_private_log_dir(root)
    second = agent_runtime.prepare_pi_private_log_dir(root)

    assert first.parent == root
    assert second.parent == root
    assert first != second
    assert first.name.startswith("pi-smoke-")
    assert second.name.startswith("pi-smoke-")
    assert stat.S_IMODE(first.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(second.stat().st_mode) & 0o077 == 0
    assert (first, True) in verify_calls
    assert (second, True) in verify_calls


def test_prepare_pi_private_log_dir_accepts_a_sticky_ancestor_after_atomic_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sticky ancestor cannot replace a newly owned private run directory."""
    assert hasattr(agent_runtime, "prepare_pi_private_log_dir")
    monkeypatch.setattr(agent_runtime, "_verify_pi_private_acl", lambda *_args, **_kwargs: None)
    sticky_parent = tmp_path / "sticky"
    sticky_parent.mkdir()
    sticky_parent.chmod(0o1777)

    run_dir = agent_runtime.prepare_pi_private_log_dir(sticky_parent / "logs")

    assert run_dir.parent == sticky_parent / "logs"
    assert stat.S_IMODE(run_dir.stat().st_mode) & 0o077 == 0


def test_prepare_pi_private_log_dir_rejects_nonsticky_writable_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An artifact root under a replaceable directory must fail before use."""
    monkeypatch.setattr(agent_runtime, "_verify_pi_private_acl", lambda *_args, **_kwargs: None)
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir()
    unsafe_parent.chmod(0o777)

    with pytest.raises(OSError, match="writable by another user"):
        agent_runtime.prepare_pi_private_log_dir(unsafe_parent / "logs")


def test_prepare_pi_private_log_dir_accepts_a_safe_ancestor_with_an_access_acl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Inherited ancestor ACLs do not invalidate a freshly private leaf directory."""
    acl_ancestor = tmp_path / "acl-ancestor"
    acl_ancestor.mkdir()
    acl_ancestor.chmod(0o755)

    def verify_acl(path: Path, *, clear: bool) -> None:
        if path == acl_ancestor:
            raise OSError("inherited access ACL")

    monkeypatch.setattr(agent_runtime, "_verify_pi_private_acl", verify_acl)

    run_dir = agent_runtime.prepare_pi_private_log_dir(acl_ancestor / "logs")

    assert run_dir.is_dir()
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700


def test_prepare_pi_private_temp_dir_canonicalizes_a_system_temp_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The canonical system temp target is verified instead of its alias path."""
    real_temp_root = tmp_path / "real-temp"
    real_temp_root.mkdir(mode=0o700)
    temp_alias = tmp_path / "temp-alias"
    temp_alias.symlink_to(real_temp_root, target_is_directory=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_alias))
    monkeypatch.setattr(agent_runtime, "_verify_pi_private_acl", lambda *_args, **_kwargs: None)

    private_temp = agent_runtime._prepare_pi_private_temp_dir()

    assert private_temp.parent.parent == real_temp_root
    assert stat.S_IMODE(private_temp.stat().st_mode) == 0o700


def test_verify_pi_private_prompt_file_rejects_a_replaced_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A prompt replacement must be rejected before its path reaches Pi."""
    target = tmp_path / "target.md"
    target.write_text("private prompt", encoding="utf-8")
    prompt_path = tmp_path / "pi-prompt.md"
    prompt_path.symlink_to(target)
    monkeypatch.setattr(agent_runtime, "_verify_pi_private_acl", lambda *_args, **_kwargs: None)

    with pytest.raises(OSError, match="regular file, not a symlink"):
        agent_runtime._verify_pi_private_prompt_file(prompt_path)


def test_linux_pi_private_filesystem_type_uses_the_most_specific_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ACL policy follows the deepest escaped mount path from mountinfo."""
    mountinfo = "\n".join(
        (
            "36 25 0:31 / / rw,relatime - overlay overlay rw",
            "42 36 0:32 / /safe\\040root rw,relatime - nfs server:/share rw",
        )
    )
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: mountinfo)

    assert agent_runtime._linux_pi_private_filesystem_type(Path("/safe root/worktree")) == "nfs"


def test_verify_pi_private_acl_fails_closed_for_nfs_style_acl_filesystems(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Linux filesystems with non-POSIX ACL semantics must not be trusted by omission."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        agent_runtime,
        "_linux_pi_private_filesystem_type",
        lambda _path: "nfs",
        raising=False,
    )

    def absent_posix_acl(*_args: Any, **_kwargs: Any) -> bytes:
        raise OSError(errno.ENODATA, "missing POSIX ACL")

    monkeypatch.setattr(os, "getxattr", absent_posix_acl, raising=False)

    with pytest.raises(OSError, match="local filesystem with verifiable POSIX ACLs"):
        agent_runtime._verify_pi_private_acl(tmp_path, clear=False)


def test_prepare_pi_private_log_dir_fails_closed_without_acl_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Private artifacts cannot be created when ACL grants cannot be verified."""
    monkeypatch.setattr(agent_runtime, "_pi_private_log_permissions_supported", lambda: False)

    with pytest.raises(OSError, match="requires verifiable private artifact permissions"):
        agent_runtime.prepare_pi_private_log_dir(tmp_path / "logs")


def test_redact_pi_private_values_replaces_all_tokens() -> None:
    """The standalone redactor should replace each configured private value."""
    text = "private-test-alias uses PRIVATE_ENDPOINT_TOKEN"

    redacted = agent_runtime.redact_pi_private_values(
        text,
        ("private-test-alias", "PRIVATE_ENDPOINT_TOKEN"),
    )

    assert redacted == (
        f"{agent_runtime.PI_PRIVATE_REDACTION} uses {agent_runtime.PI_PRIVATE_REDACTION}"
    )


def test_run_pi_smoke_session_disables_tools(
    tmp_path: Path,
    private_pi_temp: Path,
) -> None:
    """The unadmitted smoke seam should disable every built-in and extension tool."""
    captured_cmd: list[str] = []
    stdout = "\n".join(
        [
            '{"type":"session","id":"pi-session-789"}',
            '{"type":"message_end","message":{"role":"assistant","content":"pi output"}}',
        ]
    )

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with (
        patch.dict("os.environ", {"HEPH_PI_MODEL": ""}),
        patch("subprocess.run", side_effect=fake_run),
    ):
        result = agent_runtime.run_pi_smoke_session(
            "review prompt",
            cwd=tmp_path,
            timeout=30,
        )

    assert result.stdout == "pi output"
    assert "--no-tools" in captured_cmd
    assert "--tools" not in captured_cmd
    assert captured_cmd[-1].startswith("@")
    assert "review prompt" not in captured_cmd


def test_resume_pi_session_rejects_raw_resume_even_after_admission(tmp_path: Path) -> None:
    """A raw session id cannot select a writable Pi resume path."""
    with patch("hephaestus.agents.runtime._invoke_pi_session") as invoke:
        with pytest.raises(agent_runtime.AgentExecutionError, match="Unscoped resume_pi_session"):
            agent_runtime.resume_pi_session(
                "pi-session-789",
                "private feedback content",
                cwd=tmp_path,
                timeout=30,
                model="private-alias",
                sandbox="read-only",
            )

    invoke.assert_not_called()


def test_direct_agent_model_uses_operator_pi_alias_and_codex_default() -> None:
    """Direct-runner model defaults are provider-aware and explicit."""
    with patch.dict(
        "os.environ",
        {
            "HEPH_PI_MODEL": "operator-local-alias",
            "HEPH_IMPLEMENTER_MODEL": "phase-model",
        },
        clear=True,
    ):
        assert agent_runtime.direct_agent_model("pi", "HEPH_IMPLEMENTER_MODEL") == (
            "operator-local-alias"
        )
        assert (
            agent_runtime.direct_agent_model(
                "codex",
                "HEPH_IMPLEMENTER_MODEL",
                codex_default="fallback-model",
            )
            == "phase-model"
        )
        assert (
            agent_runtime.direct_agent_model(
                "codex",
                "HEPH_UNSET_MODEL",
                codex_default="fallback-model",
            )
            == "fallback-model"
        )
        assert agent_runtime.direct_agent_model("codex", "HEPH_UNSET_MODEL") == ""
        assert agent_runtime.direct_agent_model("claude", "HEPH_IMPLEMENTER_MODEL") == (
            "phase-model"
        )
        assert (
            agent_runtime.direct_agent_model("pi", codex_default="standalone-default")
            == "operator-local-alias"
        )
        assert (
            agent_runtime.direct_agent_model("codex", codex_default="standalone-default")
            == "standalone-default"
        )
        assert (
            agent_runtime.direct_agent_model("claude", codex_default="standalone-default")
            == "standalone-default"
        )


def test_agent_json_stdout_wraps_direct_agent_text() -> None:
    """Direct-agent text output should use a provider-neutral JSON wrapper."""
    assert agent_runtime.agent_json_stdout("learned", "pi-session") == (
        '{"result": "learned", "session_id": "pi-session", "is_error": false}'
    )


def test_run_agent_text_rejects_unadmitted_pi_before_dispatch(tmp_path: Path) -> None:
    """The shared text boundary requires a scoped execution request."""
    with (
        patch("hephaestus.agents.runtime._require_pi_automation_admission"),
        patch("hephaestus.agents.runtime.run_pi_text") as run_pi_text,
    ):
        with pytest.raises(
            ExecutionPolicyError,
            match="Pi automation requires an ExecutionRequest",
        ):
            agent_runtime.run_agent_text("pi", "prompt", cwd=tmp_path, timeout=30)

    run_pi_text.assert_not_called()


def test_run_agent_session_rejects_unadmitted_pi_before_dispatch(tmp_path: Path) -> None:
    """The shared session boundary requires a scoped execution request."""
    with (
        patch("hephaestus.agents.runtime._require_pi_automation_admission"),
        patch("hephaestus.agents.runtime.run_pi_session") as run_pi_session,
    ):
        with pytest.raises(
            ExecutionPolicyError,
            match="Pi automation requires an ExecutionRequest",
        ):
            agent_runtime.run_agent_session("pi", "prompt", cwd=tmp_path, timeout=30)

    run_pi_session.assert_not_called()


def test_resume_agent_session_rejects_unadmitted_pi_before_dispatch(tmp_path: Path) -> None:
    """The shared resume boundary requires a scoped execution request."""
    with (
        patch("hephaestus.agents.runtime._require_pi_automation_admission"),
        patch("hephaestus.agents.runtime.resume_pi_session") as resume_pi_session,
    ):
        with pytest.raises(
            ExecutionPolicyError,
            match="Pi automation requires an ExecutionRequest",
        ):
            agent_runtime.resume_agent_session(
                "pi",
                "pi-session-123",
                "prompt",
                cwd=tmp_path,
                timeout=30,
            )

    resume_pi_session.assert_not_called()


@pytest.mark.parametrize(
    "invoke",
    (
        lambda cwd: agent_runtime.run_agent_text("pi", "prompt", cwd=cwd, timeout=30),
        lambda cwd: agent_runtime.run_agent_session("pi", "prompt", cwd=cwd, timeout=30),
        lambda cwd: agent_runtime.resume_agent_session(
            "pi", "pi-session-123", "prompt", cwd=cwd, timeout=30
        ),
    ),
    ids=("text", "session", "resume"),
)
def test_admitted_pi_requires_execution_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invoke: Any,
) -> None:
    """Admitted Pi dispatches preserve the missing-request policy error."""
    monkeypatch.setattr(agent_runtime, "_require_pi_automation_admission", lambda _cwd: None)

    with patch("hephaestus.agents.runtime._run_pi_with_policy") as run_pi:
        with pytest.raises(
            ExecutionPolicyError,
            match="Pi automation requires an ExecutionRequest",
        ):
            invoke(tmp_path)

    run_pi.assert_not_called()


def test_run_claude_text_builds_stage_command(tmp_path: Path) -> None:
    """Claude stage execution should share the agents runtime boundary."""
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.run_claude_text(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            model="sonnet",
            sandbox="workspace-write",
        )

    assert result.stdout == "done"
    assert captured["cmd"] == [
        "claude",
        "--print",
        "--output-format",
        "text",
        "--model",
        "sonnet",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "Read,Write,Edit,Glob,Grep,Bash",
    ]
    assert captured["kwargs"]["input"] == "prompt"
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["timeout"] == 30
    assert captured["kwargs"]["check"] is False
    assert captured["kwargs"]["env"]["CLAUDECODE"] == ""


def test_run_claude_text_read_only_uses_explicit_non_mutating_policy(
    tmp_path: Path,
) -> None:
    """Read-only Claude execution must receive a fixed, explicit policy."""
    captured_cmd: list[str] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        agent_runtime.run_claude_text(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            sandbox="read-only",
        )

    assert captured_cmd == [
        "claude",
        "--print",
        "--output-format",
        "text",
        "--bare",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Glob,Grep",
        "--allowedTools",
        "Read,Glob,Grep",
        "--strict-mcp-config",
    ]


def test_run_claude_text_read_only_cannot_be_broadened_by_caller_tools(
    tmp_path: Path,
) -> None:
    """Caller grants cannot expose write, edit, or shell tools in read-only mode."""
    captured_cmd: list[str] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        agent_runtime.run_claude_text(
            "prompt",
            cwd=tmp_path,
            timeout=30,
            sandbox="read-only",
            allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
        )

    tools = set(captured_cmd[captured_cmd.index("--tools") + 1].split(","))
    allowed = set(captured_cmd[captured_cmd.index("--allowedTools") + 1].split(","))
    assert tools == {"Read", "Glob", "Grep"}
    assert allowed == tools
    assert tools.isdisjoint({"Write", "Edit", "Bash"})


def test_resolve_agent_prefers_claude_when_both_are_authenticated() -> None:
    """Omitted --agent prefers Claude only when both CLIs are authenticated."""
    with patch("hephaestus.agents.runtime.shutil.which") as mock_which:
        mock_which.side_effect = lambda name: (
            f"/bin/{name}" if name in {"claude", "codex"} else None
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["auth", "status"], 0, stdout="logged in", stderr=""
            )

            assert agent_runtime.resolve_agent(None) == "claude"


def test_resolve_agent_uses_authenticated_codex_when_claude_absent() -> None:
    """Codex is the fallback when Claude is not installed and Codex is authenticated."""
    with patch("hephaestus.agents.runtime.shutil.which") as mock_which:
        mock_which.side_effect = lambda name: "/bin/codex" if name == "codex" else None

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["codex", "login", "status"], 0, stdout="Logged in using ChatGPT", stderr=""
            ),
        ):
            assert agent_runtime.resolve_agent(None) == "codex"


def test_resolve_agent_uses_codex_when_only_codex_is_authenticated() -> None:
    """An installed but unauthenticated Claude CLI should not beat authenticated Codex."""
    with patch("hephaestus.agents.runtime.shutil.which") as mock_which:
        mock_which.side_effect = lambda name: (
            f"/bin/{name}" if name in {"claude", "codex"} else None
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd == ["claude", "auth", "status"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Not logged in")
            if cmd == ["codex", "login", "status"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="Logged in using ChatGPT", stderr=""
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            assert agent_runtime.resolve_agent(None) == "codex"


def test_is_agent_authenticated_pi_rejects_missing_model_config(tmp_path: Path) -> None:
    """Pi is not ready for automation until a local model alias is configured."""
    with (
        patch("hephaestus.agents.runtime.shutil.which", return_value="/bin/pi"),
        patch("hephaestus.agents.runtime.Path.home", return_value=tmp_path),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["pi", "--version"], 0, stdout="pi 1.0.0", stderr=""
            ),
        ),
    ):
        assert not agent_runtime.is_agent_authenticated("pi")


def test_is_agent_authenticated_uses_env_configured_status_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth status probes use the centralized call-time timeout reader."""
    monkeypatch.setenv("HEPH_AGENT_AUTH_STATUS_TIMEOUT", "77")
    with (
        patch("hephaestus.agents.runtime.shutil.which", return_value="/bin/claude"),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["claude", "auth", "status"], 0, stdout="", stderr=""
            ),
        ) as mock_run,
    ):
        assert agent_runtime.is_agent_authenticated("claude")

    assert mock_run.call_args.kwargs["timeout"] == 77


def test_resolve_agent_rejects_pi_auto_detection_until_preflight_exists(tmp_path: Path) -> None:
    """Pi cannot enter normal automation before the required preflight exists."""
    _write_pi_models_config(tmp_path)
    with patch("hephaestus.agents.runtime.shutil.which") as mock_which:
        mock_which.side_effect = lambda name: "/bin/pi" if name == "pi" else None

        with (
            patch("hephaestus.agents.runtime.Path.home", return_value=tmp_path),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["pi", "--version"], 0, stdout="pi 1.0.0", stderr=""
                ),
            ),
        ):
            with pytest.raises(RuntimeError, match="Pi automation preflight is unavailable"):
                agent_runtime.resolve_agent(None)


def test_resolve_agent_explicit_pi_fails_closed_until_preflight_exists(tmp_path: Path) -> None:
    """A local Pi model alias is insufficient evidence for automation admission."""
    _write_pi_models_config(tmp_path)
    with (
        patch("hephaestus.agents.runtime.shutil.which", return_value="/bin/pi"),
        patch("hephaestus.agents.runtime.Path.home", return_value=tmp_path),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["pi", "--version"], 0, stdout="pi 1.0.0", stderr=""
            ),
        ),
    ):
        with pytest.raises(RuntimeError, match="Pi automation preflight is unavailable"):
            agent_runtime.resolve_agent("pi")


def test_resolve_pi_reports_package_preflight_remediation(tmp_path: Path) -> None:
    """A failed package gate names the bootstrap command and effective cwd."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    failure = PiPreflightResult(
        ready=False,
        status="package_inventory_mismatch",
        remediation="Run hephaestus-install-pi-plugins --global --yes --no-approve",
    )
    with patch(
        "hephaestus.agents.runtime.preflight_pi_environment", return_value=failure
    ) as preflight:
        with pytest.raises(RuntimeError, match="hephaestus-install-pi-plugins"):
            agent_runtime.resolve_agent("pi", cwd=tmp_path)

    preflight.assert_called_once_with(tmp_path)


def test_resolve_pi_is_na_without_a_host_isolation_adapter(tmp_path: Path) -> None:
    """Provider selection cannot expose Pi without an OS-isolation broker."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    with (
        patch(
            "hephaestus.agents.runtime.preflight_pi_environment",
            return_value=PiPreflightResult.ready_result(),
        ),
        patch("hephaestus.agents.runtime.is_agent_authenticated", return_value=True),
        patch("hephaestus.agents.runtime._PI_ISOLATION_ADAPTER", None),
    ):
        with pytest.raises(agent_runtime.PiIsolationUnavailableError, match="Pi automation is N/A"):
            agent_runtime.resolve_agent("pi", cwd=tmp_path)


def test_pi_models_configured_honors_pi_coding_agent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private Pi model discovery follows the operator-selected Pi root."""
    pi_dir = tmp_path / "pi-agent"
    pi_dir.mkdir()
    (pi_dir / "models.json").write_text(
        json.dumps({"models": [{"id": "private-alias"}]}), encoding="utf-8"
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))

    assert agent_runtime._pi_models_configured() is True


def test_direct_pi_helpers_preflight_effective_cwd_before_subprocess(tmp_path: Path) -> None:
    """Every provider-neutral direct Pi path binds preflight to its worktree."""
    from hephaestus.agents.pi_plugins import PiPreflightResult

    failure = PiPreflightResult(False, "package_inventory_mismatch", "install packages")
    with patch(
        "hephaestus.agents.runtime.preflight_pi_environment", return_value=failure
    ) as preflight:
        with pytest.raises(RuntimeError, match="package_inventory_mismatch"):
            agent_runtime.run_agent_text("pi", "prompt", cwd=tmp_path, timeout=30)
    preflight.assert_called_once_with(tmp_path)


def test_resolve_agent_explicit_rejects_uninstalled_pi() -> None:
    """An explicit Pi selection reports the actionable preflight boundary first."""
    with patch("hephaestus.agents.runtime.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="Pi automation preflight is unavailable"):
            agent_runtime.resolve_agent("pi")


def test_resolve_agent_explicit_rejects_unconfigured_pi(tmp_path: Path) -> None:
    """An installed Pi CLI does not bypass the package/scope preflight gate."""
    with (
        patch("hephaestus.agents.runtime.shutil.which", return_value="/bin/pi"),
        patch("hephaestus.agents.runtime.Path.home", return_value=tmp_path),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["pi", "--version"], 0, stdout="pi 1.0.0", stderr=""
            ),
        ),
    ):
        with pytest.raises(RuntimeError, match="Pi automation preflight is unavailable"):
            agent_runtime.resolve_agent("pi")


def test_resolve_agent_explicit_codex_overrides_claude() -> None:
    """An explicit --agent value wins over auto-detection when authenticated."""
    with patch("hephaestus.agents.runtime.shutil.which", return_value="/bin/codex"):
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["codex", "login", "status"], 0, stdout="Logged in", stderr=""
            ),
        ):
            assert agent_runtime.resolve_agent("codex") == "codex"


def test_resolve_agent_explicit_rejects_uninstalled_agent() -> None:
    """An explicit --agent for a CLI not on PATH should fail immediately."""
    with patch("hephaestus.agents.runtime.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="not installed on PATH"):
            agent_runtime.resolve_agent("codex")


def test_resolve_agent_explicit_rejects_unauthenticated_agent() -> None:
    """An explicit --agent for an installed but unauthenticated CLI should fail."""
    with patch("hephaestus.agents.runtime.shutil.which", return_value="/bin/codex"):
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["codex", "login", "status"], 1, stdout="", stderr="Not logged in"
            ),
        ):
            with pytest.raises(RuntimeError, match="not authenticated"):
                agent_runtime.resolve_agent("codex")


def test_resolve_agent_errors_when_no_provider_exists() -> None:
    """Auto-detection should fail clearly when no supported provider is installed."""
    with patch("hephaestus.agents.runtime.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="No supported agent backend"):
            agent_runtime.resolve_agent(None)


def test_resolve_agent_errors_when_no_provider_is_authenticated() -> None:
    """Installed providers must prove authentication before auto-selection."""
    with patch("hephaestus.agents.runtime.shutil.which") as mock_which:
        mock_which.side_effect = lambda name: (
            f"/bin/{name}" if name in {"claude", "codex"} else None
        )

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["auth", "status"], 1, stdout="", stderr="Not logged in"
            ),
        ):
            with pytest.raises(RuntimeError, match="none are authenticated"):
                agent_runtime.resolve_agent(None)


def test_add_agent_argument_defaults_to_auto_detect() -> None:
    """The parser should not hardcode Claude before runtime resolution."""
    import argparse

    parser = argparse.ArgumentParser()
    agent_runtime.add_agent_argument(parser)

    assert parser.parse_args([]).agent is None
    assert parser.parse_args(["--agent", "codex"]).agent == "codex"
    assert parser.parse_args(["--agent", "pi"]).agent == "pi"
