"""Tests for provider-neutral agent runtime helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.agents import runtime as agent_runtime


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

    assert cmd == ["codex", "exec", "resume", "session-123", "--json"]


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


def test_run_pi_session_uses_json_mode_and_captures_session(tmp_path: Path) -> None:
    """Pi stage execution should consume JSONL and preserve the session id."""
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
        result = agent_runtime.run_pi_session(
            "private prompt content",
            cwd=tmp_path,
            timeout=30,
            model="private-alias",
        )

    assert result.session_id == "pi-session-789"
    assert result.stdout == "pi output"
    assert captured["cmd"][:-1] == ["pi", "--mode", "json"]
    assert captured["cmd"][-1].startswith("@")
    assert "--model" not in captured["cmd"]
    assert "private-alias" not in captured["cmd"]
    assert "private prompt content" not in captured["cmd"]
    assert captured["prompt_text"] == "private prompt content"
    assert captured["prompt_mode"] == 0o600
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["timeout"] == 30
    assert captured["kwargs"]["check"] is True
    assert captured["kwargs"]["env"]["HEPH_PI_MODEL"] == "private-alias"
    assert captured["kwargs"]["env"]["PI_TELEMETRY"] == "0"
    assert captured["kwargs"]["env"]["PI_SKIP_VERSION_CHECK"] == "1"


def test_run_pi_session_redacts_private_values_from_failures(tmp_path: Path) -> None:
    """Pi subprocess failure diagnostics should not leak local aliases or tokens."""
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\n",
        encoding="utf-8",
    )

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            7,
            cmd,
            output="PRIVATE_ENDPOINT_TOKEN",
            stderr="private-test-alias PRIVATE_ENDPOINT_TOKEN",
        )

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            agent_runtime.run_pi_session(
                "prompt",
                cwd=tmp_path,
                timeout=30,
                model="private-test-alias",
            )

    exc = exc_info.value
    assert "private-test-alias" not in str(exc.cmd)
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.stdout or "")
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.stderr or "")
    assert agent_runtime.PI_PRIVATE_REDACTION in (exc.stderr or "")


def test_run_pi_session_redacts_private_values_from_timeouts(tmp_path: Path) -> None:
    """Pi timeout diagnostics should redact cmd, partial stdout, and stderr."""
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\n",
        encoding="utf-8",
    )

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd,
            7,
            output="private-test-alias PRIVATE_ENDPOINT_TOKEN",
            stderr="PRIVATE_ENDPOINT_TOKEN private-test-alias",
        )

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            agent_runtime.run_pi_session(
                "prompt",
                cwd=tmp_path,
                timeout=30,
                model="private-test-alias",
            )

    exc = exc_info.value
    assert "private-test-alias" not in str(exc)
    assert "private-test-alias" not in str(exc.cmd)
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.output or "")
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.stdout or "")
    assert "PRIVATE_ENDPOINT_TOKEN" not in (exc.stderr or "")
    assert agent_runtime.PI_PRIVATE_REDACTION in (exc.output or "")
    assert agent_runtime.PI_PRIVATE_REDACTION in (exc.stderr or "")


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


def test_run_pi_session_read_only_restricts_tools(tmp_path: Path) -> None:
    """Read-only Pi stages should request a read-only tool surface."""
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
        result = agent_runtime.run_pi_session(
            "review prompt",
            cwd=tmp_path,
            timeout=30,
            sandbox="read-only",
        )

    assert result.stdout == "pi output"
    tools_index = captured_cmd.index("--tools")
    assert captured_cmd[tools_index + 1] == agent_runtime.PI_READ_ONLY_TOOLS
    assert captured_cmd[-1].startswith("@")
    assert "review prompt" not in captured_cmd


def test_resume_pi_session_passes_resume_id_without_alias_argv_leak(tmp_path: Path) -> None:
    """Pi feedback loops should resume the captured session id."""
    captured: dict[str, Any] = {}
    stdout = "\n".join(
        [
            '{"type":"session","id":"pi-session-789"}',
            '{"type":"turn_end","message":{"role":"assistant","content":"resumed"}}',
        ]
    )

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        prompt_arg = next(arg for arg in cmd if arg.startswith("@"))
        captured["prompt_text"] = Path(prompt_arg[1:]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with (
        patch.dict("os.environ", {"HEPH_PI_MODEL": ""}),
        patch("subprocess.run", side_effect=fake_run),
    ):
        result = agent_runtime.resume_pi_session(
            "pi-session-789",
            "private feedback content",
            cwd=tmp_path,
            timeout=30,
            model="private-alias",
        )

    assert captured["cmd"][:-1] == ["pi", "--mode", "json", "--session", "pi-session-789"]
    assert captured["cmd"][-1].startswith("@")
    assert "--model" not in captured["cmd"]
    assert "private-alias" not in captured["cmd"]
    assert "private feedback content" not in captured["cmd"]
    assert captured["prompt_text"] == "private feedback content"
    assert captured["kwargs"]["env"]["HEPH_PI_MODEL"] == "private-alias"
    assert result.stdout == "resumed"
    assert result.session_id == "pi-session-789"


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


def test_agent_json_stdout_wraps_direct_agent_text() -> None:
    """Direct-agent text output should use a provider-neutral JSON wrapper."""
    assert agent_runtime.agent_json_stdout("learned", "pi-session") == (
        '{"result": "learned", "session_id": "pi-session", "is_error": false}'
    )


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


def test_run_claude_text_read_only_omits_write_permissions(tmp_path: Path) -> None:
    """Read-only stages should not grant Claude write-capable tool permissions."""
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

    assert "--permission-mode" not in captured_cmd
    assert "--allowedTools" not in captured_cmd


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


def test_resolve_agent_uses_pi_when_claude_and_codex_absent(tmp_path: Path) -> None:
    """Pi is the third auto-detected backend after Claude and Codex."""
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
            assert agent_runtime.resolve_agent(None) == "pi"


def test_resolve_agent_explicit_pi(tmp_path: Path) -> None:
    """An explicit Pi backend should be accepted when the CLI preflight succeeds."""
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
        assert agent_runtime.resolve_agent("pi") == "pi"


def test_resolve_agent_explicit_rejects_uninstalled_pi() -> None:
    """An explicit Pi selection should fail clearly when the CLI is missing."""
    with patch("hephaestus.agents.runtime.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="not installed on PATH"):
            agent_runtime.resolve_agent("pi")


def test_resolve_agent_explicit_rejects_unconfigured_pi(tmp_path: Path) -> None:
    """An installed Pi CLI without model configuration should fail preflight."""
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
        with pytest.raises(RuntimeError, match="not authenticated"):
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
