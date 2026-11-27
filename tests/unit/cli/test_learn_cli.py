"""Behavior tests for the hephaestus-learn CLI (issue #3518)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hephaestus.agents.session_errors import AgentSessionLostError
from hephaestus.cli import learn
from hephaestus.cli.learn import LEARN_PROMPT, _build_parser, main
from hephaestus.errors import ExitCode, ExternalToolError


@pytest.fixture
def no_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Bind the CLI to a directory with no source evidence and stub provider seams."""
    monkeypatch.setattr(learn, "resolve_agent", lambda *a, **k: "claude")
    monkeypatch.setattr(learn, "resume_claude_session", lambda **k: "learn output")
    return tmp_path


def _args(tmp_path: Path, *extra: str) -> list[str]:
    """Build one valid argument vector."""
    return ["--session-id", "opaque-session", "--agent", "claude", "--cwd", str(tmp_path), *extra]


def test_parser_requires_session_id() -> None:
    """A missing session id is rejected at the parser boundary."""
    with pytest.raises(SystemExit) as caught:
        _build_parser().parse_args([])
    assert caught.value.code == int(ExitCode.INVALID_INPUT)


def test_parser_accepts_catalog_agents_and_rejects_other_names() -> None:
    """The harness selector comes from the catalog, not a role namespace."""
    parser = _build_parser()
    for agent in ("claude", "codex", "pi", "opencode", "opencode2"):
        assert parser.parse_args(["--session-id", "s", "--agent", agent]).agent == agent
    with pytest.raises(SystemExit):
        parser.parse_args(["--session-id", "s", "--agent", "planner"])


def test_parser_has_no_role_flag() -> None:
    """Harness and session role stay separate; no role flag is exposed."""
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--session-id", "s", "--role", "learner"])


def test_parser_advertises_standard_flags() -> None:
    """The console-script contract requires --json and --version."""
    help_text = _build_parser().format_help()
    assert "--json" in help_text
    assert "--version" in help_text
    assert "--session-id" in help_text


def test_claude_resumes_given_session_with_learn_prompt(
    tmp_path: Path, no_evidence: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The learn turn runs in the named session with the directive as prompt."""
    with patch.object(learn, "resume_claude_session", return_value="learn output") as resume:
        result = main(_args(tmp_path))

    captured = capsys.readouterr()
    assert result == int(ExitCode.OK)
    assert "learn output" in captured.out
    kwargs = resume.call_args.kwargs
    assert kwargs["session_id"] == "opaque-session"
    assert kwargs["prompt"] == LEARN_PROMPT
    assert kwargs["cwd"] == tmp_path
    assert "role" not in kwargs
    assert "session_agent" not in kwargs
    assert "learning_deferred:source_evidence_required" in captured.err


def test_unresolved_agent_uses_catalog_auto_detection(tmp_path: Path) -> None:
    """An omitted harness is resolved once through the shared runtime."""
    with (
        patch.object(learn, "resolve_agent", return_value="claude") as resolve,
        patch.object(learn, "resume_claude_session", return_value="learn output"),
    ):
        result = main(["--session-id", "s", "--cwd", str(tmp_path)])

    assert result == int(ExitCode.OK)
    resolve.assert_called_once()
    assert resolve.call_args.args[0] is None


def test_pi_fails_closed_before_any_provider_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pi without an isolation adapter is refused before provider execution."""
    with (
        patch.object(learn, "resolve_agent") as resolve,
        patch.object(learn, "resume_claude_session") as claude,
        patch.object(learn, "resume_agent_session") as agent,
    ):
        result = main(["--session-id", "s", "--agent", "pi", "--cwd", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == int(ExitCode.CONFIGURATION)
    assert "isolation adapter" in captured.err
    resolve.assert_not_called()
    claude.assert_not_called()
    agent.assert_not_called()


def test_non_claude_dispatch_passes_only_provider_arguments(tmp_path: Path) -> None:
    """Codex-style providers receive a harness name; no role is invented."""
    with (
        patch.object(learn, "resolve_agent", return_value="codex"),
        patch.object(
            learn,
            "resume_agent_session",
            return_value=SimpleNamespace(stdout="codex output"),
        ) as resume,
    ):
        result = main(["--session-id", "s", "--agent", "codex", "--cwd", str(tmp_path)])

    assert result == int(ExitCode.OK)
    args, kwargs = resume.call_args
    assert args[0] == "codex"
    assert args[1] == "s"
    assert args[2] == LEARN_PROMPT
    assert kwargs["cwd"] == tmp_path
    assert "role" not in kwargs
    assert "session_agent" not in kwargs


def test_lost_session_returns_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing transcript maps to the named not-found exit code."""
    with patch.object(
        learn, "resume_claude_session", side_effect=AgentSessionLostError("session lost")
    ):
        result = main(_args(tmp_path))
    assert result == int(ExitCode.NOT_FOUND)
    assert "session lost" in capsys.readouterr().err


def test_provider_failure_returns_error(tmp_path: Path) -> None:
    """A native provider failure reports the generic error exit code."""
    failure = subprocess.CalledProcessError(1, ["codex"])
    with (
        patch.object(learn, "resolve_agent", return_value="codex"),
        patch.object(learn, "resume_agent_session", side_effect=failure),
    ):
        assert main(["--session-id", "s", "--agent", "codex", "--cwd", str(tmp_path)]) == int(
            ExitCode.ERROR
        )


def test_claude_external_failure_returns_external_tool(tmp_path: Path) -> None:
    """A Claude subprocess failure keeps its bounded external-tool exit code."""
    failure = ExternalToolError("provider refused")
    with patch.object(learn, "resume_claude_session", side_effect=failure):
        assert main(_args(tmp_path)) == int(ExitCode.EXTERNAL_TOOL)


def test_resolve_failure_is_configuration(tmp_path: Path) -> None:
    """An absent or unauthenticated backend is reported as configuration."""
    with patch.object(learn, "resolve_agent", side_effect=RuntimeError("not installed")):
        result = main(_args(tmp_path))
    assert result == int(ExitCode.CONFIGURATION)


def test_json_envelope_reports_deferred_delivery(
    tmp_path: Path, no_evidence: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Machine-readable output names the deferred delivery state and reason."""
    with patch.object(learn, "emit_json_status") as emit:
        result = main([*_args(tmp_path), "--json"])

    assert result == int(ExitCode.OK)
    envelope = emit.call_args.args[0]
    assert envelope == int(ExitCode.OK)
    assert emit.call_args.kwargs["session_id"] == "opaque-session"
    assert emit.call_args.kwargs["delivery"] == "deferred"
    assert emit.call_args.kwargs["delivery_reason"] == "learning_deferred:source_evidence_required"
    assert json.loads(json.dumps(emit.call_args.kwargs))["agent"] == "claude"


def test_invalid_session_id_is_rejected_before_provider_call(tmp_path: Path) -> None:
    """An empty session id never reaches a provider."""
    with (
        patch.object(learn, "resolve_agent") as resolve,
        patch.object(learn, "resume_claude_session") as resume,
    ):
        result = main(["--session-id", " ", "--agent", "claude", "--cwd", str(tmp_path)])
    assert result == int(ExitCode.INVALID_INPUT)
    resolve.assert_not_called()
    resume.assert_not_called()
