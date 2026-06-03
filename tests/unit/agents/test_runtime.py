"""Tests for provider-neutral agent runtime helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.agents import runtime as agent_runtime


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


def test_run_codex_session_returns_session_id_and_last_message(tmp_path: Path) -> None:
    """The runtime should prefer --output-last-message and preserve session id."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("final answer", encoding="utf-8")
        stdout = (
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
            '{"type":"agent_message","message":"fallback"}\n'
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("hephaestus.agents.runtime.codex_approval_args", return_value=[]):
        with patch("subprocess.run", side_effect=fake_run):
            result = agent_runtime.run_codex_session(
                "prompt",
                cwd=tmp_path,
                timeout=30,
                sandbox="workspace-write",
            )

    assert result.session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert result.stdout == "final answer"


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

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("resumed", encoding="utf-8")
        stdout = '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.resume_codex_session(
            "019e1e57-7652-7892-b1ca-c31c93d4b160",
            "feedback",
            cwd=tmp_path,
            timeout=30,
        )

    assert captured_cmd[:4] == [
        "codex",
        "exec",
        "resume",
        "019e1e57-7652-7892-b1ca-c31c93d4b160",
    ]
    assert result.stdout == "resumed"
    assert result.session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"


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


def test_resolve_agent_prefers_claude_when_both_exist() -> None:
    """Omitted --agent auto-detects, preferring Claude when both CLIs exist."""
    with patch("hephaestus.agents.runtime.shutil.which") as mock_which:
        mock_which.side_effect = lambda name: (
            f"/bin/{name}" if name in {"claude", "codex"} else None
        )

        assert agent_runtime.resolve_agent(None) == "claude"


def test_resolve_agent_uses_codex_when_claude_absent() -> None:
    """Codex is the fallback when Claude is not installed."""
    with patch("hephaestus.agents.runtime.shutil.which") as mock_which:
        mock_which.side_effect = lambda name: "/bin/codex" if name == "codex" else None

        assert agent_runtime.resolve_agent(None) == "codex"


def test_resolve_agent_explicit_codex_overrides_claude() -> None:
    """An explicit --agent value wins over auto-detection."""
    with patch("hephaestus.agents.runtime.shutil.which", return_value="/bin/claude"):
        assert agent_runtime.resolve_agent("codex") == "codex"


def test_resolve_agent_errors_when_no_provider_exists() -> None:
    """Auto-detection should fail clearly when no supported provider is installed."""
    with patch("hephaestus.agents.runtime.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="No supported agent backend"):
            agent_runtime.resolve_agent(None)


def test_add_agent_argument_defaults_to_auto_detect() -> None:
    """The parser should not hardcode Claude before runtime resolution."""
    import argparse

    parser = argparse.ArgumentParser()
    agent_runtime.add_agent_argument(parser)

    assert parser.parse_args([]).agent is None
    assert parser.parse_args(["--agent", "codex"]).agent == "codex"
