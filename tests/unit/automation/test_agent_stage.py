"""Tests for the provider-selectable agent stage runner."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hephaestus.agents.runtime import AgentRunResult
from hephaestus.automation import agent_stage


def _args(tmp_path: Path, *, agent: str = "claude") -> argparse.Namespace:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("stage prompt", encoding="utf-8")
    return argparse.Namespace(
        agent=agent,
        prompt_file=str(prompt_file),
        repo_root=str(tmp_path),
        stage="strict-review",
        output=str(tmp_path / "out.txt"),
        log_file=str(tmp_path / "agent.log"),
        skill_file=None,
        model="",
        sandbox="workspace-write",
        approval="never",
        timeout=30,
        debug=False,
    )


def test_read_prompt_prepends_skill_instructions(tmp_path: Path) -> None:
    """Skill context should be prepended without dropping the stage prompt."""
    prompt_file = tmp_path / "prompt.md"
    skill_file = tmp_path / "skill.md"
    prompt_file.write_text("do the work", encoding="utf-8")
    skill_file.write_text("strict instructions", encoding="utf-8")

    prompt = agent_stage.read_prompt(prompt_file, skill_file, "review")

    assert "ProjectHephaestus agent stage `review`" in prompt
    assert "strict instructions" in prompt
    assert prompt.endswith("do the work")


def test_run_agent_dispatches_claude_and_writes_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude stages should write both final output and logs."""

    def fake_run_claude_text(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["claude"], 0, stdout="claude output", stderr="")

    monkeypatch.setattr(agent_stage, "run_claude_text", fake_run_claude_text)

    args = _args(tmp_path, agent="claude")
    rc = agent_stage.run_agent(args)

    assert rc == 0
    assert Path(args.output).read_text(encoding="utf-8") == "claude output"
    assert Path(args.log_file).read_text(encoding="utf-8") == "claude output"


def test_run_agent_dispatches_codex_and_logs_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex stages should persist the captured session id in the log."""

    def fake_run_codex_session(*args: object, **kwargs: object) -> AgentRunResult:
        return AgentRunResult(stdout="codex output", stderr="", session_id="session-123")

    monkeypatch.setattr(agent_stage, "run_codex_session", fake_run_codex_session)

    args = _args(tmp_path, agent="codex")
    rc = agent_stage.run_agent(args)

    assert rc == 0
    assert Path(args.output).read_text(encoding="utf-8") == "codex output"
    assert (
        Path(args.log_file).read_text(encoding="utf-8") == "SESSION_ID: session-123\n\ncodex output"
    )


def test_run_agent_rejects_unsupported_direct_agent_value(tmp_path: Path) -> None:
    """Direct API callers should not silently route unknown providers to Codex."""
    args = _args(tmp_path, agent="bogus")

    with pytest.raises(ValueError, match="Unsupported agent: bogus"):
        agent_stage.run_agent(args)


def test_main_rejects_approval_flag_with_claude_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--approval is Codex-only; passing it with --agent=claude must error (issue #773)."""
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("p", encoding="utf-8")
    argv = [
        "--prompt-file",
        str(prompt_file),
        "--repo-root",
        str(tmp_path),
        "--stage",
        "x",
        "--output",
        str(tmp_path / "out.txt"),
        "--agent",
        "claude",
        "--approval",
        "on-request",
    ]
    with pytest.raises(SystemExit) as exc:
        agent_stage.main(argv)
    assert exc.value.code == 2
    assert "--approval=on-request" in capsys.readouterr().err


def test_main_rejects_danger_full_access_sandbox_with_claude_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--sandbox=danger-full-access silently no-ops on claude; must error (issue #773)."""
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("p", encoding="utf-8")
    argv = [
        "--prompt-file",
        str(prompt_file),
        "--repo-root",
        str(tmp_path),
        "--stage",
        "x",
        "--output",
        str(tmp_path / "out.txt"),
        "--agent",
        "claude",
        "--sandbox",
        "danger-full-access",
    ]
    with pytest.raises(SystemExit) as exc:
        agent_stage.main(argv)
    assert exc.value.code == 2
    assert "--sandbox=danger-full-access" in capsys.readouterr().err


def test_main_allows_read_only_sandbox_with_claude_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--sandbox=read-only IS honored by run_claude_text and must NOT be rejected.

    Regression guard for the over-restriction caught in plan review of issue #773.
    """

    def fake_run_claude_text(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["claude"], 0, stdout="ok", stderr="")

    monkeypatch.setattr(agent_stage, "run_claude_text", fake_run_claude_text)
    monkeypatch.setattr(agent_stage, "resolve_agent", lambda x: "claude")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("p", encoding="utf-8")
    argv = [
        "--prompt-file",
        str(prompt_file),
        "--repo-root",
        str(tmp_path),
        "--stage",
        "x",
        "--output",
        str(tmp_path / "out.txt"),
        "--agent",
        "claude",
        "--sandbox",
        "read-only",
    ]
    assert agent_stage.main(argv) == 0


def test_main_allows_default_flags_with_claude_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults must pass validation; only explicit no-op values are rejected."""

    def fake_run_claude_text(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["claude"], 0, stdout="ok", stderr="")

    monkeypatch.setattr(agent_stage, "run_claude_text", fake_run_claude_text)
    monkeypatch.setattr(agent_stage, "resolve_agent", lambda x: "claude")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("p", encoding="utf-8")
    argv = [
        "--prompt-file",
        str(prompt_file),
        "--repo-root",
        str(tmp_path),
        "--stage",
        "x",
        "--output",
        str(tmp_path / "out.txt"),
        "--agent",
        "claude",
    ]
    assert agent_stage.main(argv) == 0


def test_main_allows_approval_with_codex_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex honors --approval, so validation must not fire."""

    def fake_run_codex_session(*a: object, **kw: object) -> AgentRunResult:
        return AgentRunResult(stdout="ok", stderr="", session_id=None)

    monkeypatch.setattr(agent_stage, "run_codex_session", fake_run_codex_session)
    monkeypatch.setattr(agent_stage, "resolve_agent", lambda x: "codex")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("p", encoding="utf-8")
    argv = [
        "--prompt-file",
        str(prompt_file),
        "--repo-root",
        str(tmp_path),
        "--stage",
        "x",
        "--output",
        str(tmp_path / "out.txt"),
        "--agent",
        "codex",
        "--approval",
        "on-request",
    ]
    assert agent_stage.main(argv) == 0
