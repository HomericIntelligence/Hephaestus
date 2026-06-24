"""Tests for provider-neutral agent runtime helpers."""

from __future__ import annotations

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
        **_: Any,
    ) -> None:
        self.cmd = cmd
        self.stdout = proc_stdout
        self.stderr = proc_stderr
        self.hang = hang
        self.returncode = returncode
        self.killed = False
        self.terminated = False
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text(final_message, encoding="utf-8")

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input, timeout
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

    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        with patch("subprocess.Popen", side_effect=fake_popen):
            result = agent_runtime.run_codex_session(
                "prompt",
                cwd=tmp_path,
                timeout=30,
                sandbox="workspace-write",
            )

    assert result.session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert result.stdout == "final answer"


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

    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        with patch("subprocess.Popen", side_effect=fake_popen):
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
    assert captured["cmd"][:-1] == [
        "pi",
        "--mode",
        "json",
        "--model",
        "private-alias",
    ]
    assert captured["cmd"][-1].startswith("@")
    assert "private prompt content" not in captured["cmd"]
    assert captured["prompt_text"] == "private prompt content"
    assert captured["prompt_mode"] == 0o600
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["timeout"] == 30
    assert captured["kwargs"]["check"] is True
    assert captured["kwargs"]["env"]["PI_TELEMETRY"] == "0"
    assert captured["kwargs"]["env"]["PI_SKIP_VERSION_CHECK"] == "1"


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


def test_resume_pi_session_passes_resume_id(tmp_path: Path) -> None:
    """Pi feedback loops should resume the captured session id."""
    captured: dict[str, Any] = {}
    stdout = "\n".join(
        [
            '{"type":"session","id":"pi-session-789"}',
            '{"type":"turn_end","message":{"role":"assistant","content":"resumed"}}',
        ]
    )

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        captured["cmd"] = cmd
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
        )

    assert captured["cmd"][:-1] == ["pi", "--mode", "json", "--session", "pi-session-789"]
    assert captured["cmd"][-1].startswith("@")
    assert "private feedback content" not in captured["cmd"]
    assert captured["prompt_text"] == "private feedback content"
    assert result.stdout == "resumed"
    assert result.session_id == "pi-session-789"


def test_direct_agent_model_uses_operator_pi_alias() -> None:
    """Pi uses its own local alias env var instead of phase-specific model names."""
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
        assert agent_runtime.direct_agent_model("claude", "HEPH_IMPLEMENTER_MODEL") == (
            "phase-model"
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
