"""Behavior tests for session-id-first Claude resume (issue #3518)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents import claude_invoke
from hephaestus.agents.claude_invoke import resume_claude_session
from hephaestus.agents.session_errors import AgentSessionLostError
from hephaestus.errors import ExternalToolError

_PROMPT = "$athena:learn"


def _completed(stdout: str = "learn output") -> subprocess.CompletedProcess[str]:
    """Return one successful subprocess result."""
    return subprocess.CompletedProcess(["claude"], 0, stdout=stdout, stderr="")


def test_resume_passes_exact_session_id_and_prompt(tmp_path: Path) -> None:
    """A bare session id is resumed verbatim; no derived id or repo key is used."""
    with (
        patch.object(claude_invoke, "run_subprocess", return_value=_completed()) as run,
        patch.object(claude_invoke, "build_claude_child_env", return_value={"A": "1"}) as env,
    ):
        stdout = resume_claude_session(
            session_id="opaque-session-id",
            prompt=_PROMPT,
            cwd=tmp_path,
            timeout=42,
        )

    assert stdout == "learn output"
    args, kwargs = run.call_args
    assert args[0] == [
        "claude",
        "--resume",
        "opaque-session-id",
        "--output-format",
        "text",
        "--print",
    ]
    assert kwargs["input_text"] == _PROMPT
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["timeout"] == 42
    assert kwargs["env"] == {"A": "1"}
    assert kwargs["check"] is True
    assert kwargs["log_on_error"] is False
    assert kwargs["track_process_group"] is True
    env.assert_called_once_with()


def test_resume_uses_library_default_timeout(tmp_path: Path) -> None:
    """An omitted timeout uses the shared library default."""
    with (
        patch.object(claude_invoke, "run_subprocess", return_value=_completed()) as run,
        patch.object(claude_invoke, "agent_default_timeout", return_value=99),
    ):
        resume_claude_session(session_id="sid", prompt=_PROMPT, cwd=tmp_path)

    assert run.call_args.kwargs["timeout"] == 99


def test_expired_session_raises_lost_error(tmp_path: Path) -> None:
    """A missing transcript becomes a named lost-session failure."""
    failure = subprocess.CalledProcessError(
        1, ["claude"], stderr="No conversation found with session ID sid"
    )
    with patch.object(claude_invoke, "run_subprocess", side_effect=failure):
        with pytest.raises(AgentSessionLostError):
            resume_claude_session(session_id="sid", prompt=_PROMPT, cwd=tmp_path)


def test_other_failure_becomes_bounded_external_tool_error(tmp_path: Path) -> None:
    """A non-expiry failure is reported with bound subprocess detail."""
    failure = subprocess.CalledProcessError(1, ["claude"], stderr="provider refused")
    with patch.object(claude_invoke, "run_subprocess", side_effect=failure):
        with pytest.raises(ExternalToolError) as caught:
            resume_claude_session(session_id="sid", prompt=_PROMPT, cwd=tmp_path)

    assert "provider refused" in caught.value.message


@pytest.mark.parametrize("session_id", ["", "   ", "sid\x00tail"])
def test_invalid_session_id_is_rejected(session_id: str, tmp_path: Path) -> None:
    """An empty or NUL-bearing id never reaches a subprocess."""
    with patch.object(claude_invoke, "run_subprocess") as run:
        with pytest.raises(ValueError):
            resume_claude_session(session_id=session_id, prompt=_PROMPT, cwd=tmp_path)
    run.assert_not_called()


def test_timeout_propagates(tmp_path: Path) -> None:
    """A timeout is not changed into a different failure class."""
    timeout = subprocess.TimeoutExpired(["claude"], 1)
    with patch.object(claude_invoke, "run_subprocess", side_effect=timeout):
        with pytest.raises(subprocess.TimeoutExpired):
            resume_claude_session(session_id="sid", prompt=_PROMPT, cwd=tmp_path, timeout=1)


def test_remaining_timeout_and_shutdown_are_forwarded(tmp_path: Path) -> None:
    """Deadline and cancellation seams reach the tracked subprocess."""
    shutdown = MagicMock()

    def deadline() -> int:
        return 7

    with patch.object(claude_invoke, "run_subprocess", return_value=_completed()) as run:
        resume_claude_session(
            session_id="sid",
            prompt=_PROMPT,
            cwd=tmp_path,
            timeout=30,
            remaining_timeout=deadline,
            shutdown=shutdown,
        )
    assert run.call_args.kwargs["remaining_timeout"] is deadline
    assert run.call_args.kwargs["shutdown"] is shutdown
