"""Unit tests for the deterministic-session invocation helper."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.claude_invoke import (
    SESSION_EXPIRED_PHRASES,
    invoke_claude_with_session,
)
from hephaestus.automation.session_naming import (
    AGENT_PLAN_REVIEWER,
    AGENT_PLANNER,
    session_uuid,
)


def _argv(call_args_list_entry: Any) -> list[str]:
    """Extract argv from a ``subprocess.run`` call recorded by mock."""
    if hasattr(call_args_list_entry, "args"):
        call_args = call_args_list_entry.args
    else:
        call_args = call_args_list_entry[0]
    return list(call_args[0])


@pytest.fixture
def stub_run() -> Generator[MagicMock, None, None]:
    """Patch subprocess.run to return a successful result."""
    with patch("hephaestus.automation.claude_invoke.subprocess.run") as m:
        m.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        yield m


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME so session_jsonl_path resolves under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _make_existing_jsonl(home: Path, cwd: Path, sid: str) -> None:
    encoded = str(cwd.resolve()).replace("/", "-")
    target_dir = home / ".claude" / "projects" / encoded
    target_dir.mkdir(parents=True)
    (target_dir / f"{sid}.jsonl").write_text("{}\n")


class TestCreateVsResume:
    """First call vs subsequent call: --session-id vs --resume."""

    def test_first_call_uses_session_id(self, stub_run: MagicMock, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        out, sid = invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            githash="x",
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )
        argv = _argv(stub_run.call_args)
        assert "--session-id" in argv
        assert "--name" in argv
        assert "--resume" not in argv
        assert out == "ok"
        assert sid == session_uuid("R", 1, AGENT_PLANNER, "x")

    def test_subsequent_call_uses_resume(self, stub_run: MagicMock, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "x")
        _make_existing_jsonl(fake_home, cwd, sid)

        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            githash="x",
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )
        argv = _argv(stub_run.call_args)
        assert "--resume" in argv
        assert sid in argv
        assert "--session-id" not in argv
        assert "--name" not in argv

    def test_different_agents_get_different_uuids(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        _, sid_planner = invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            githash="x",
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )
        _, sid_reviewer = invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLAN_REVIEWER,
            githash="x",
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )
        assert sid_planner != sid_reviewer


class TestSessionExpiredFallback:
    """When --resume hits a missing session, fall back to --session-id."""

    def test_expired_resume_falls_back_to_create(self, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "x")
        _make_existing_jsonl(fake_home, cwd, sid)

        expired_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["claude"],
            output="",
            stderr=SESSION_EXPIRED_PHRASES[0],
        )
        ok = MagicMock(stdout="recovered", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[expired_exc, ok],
        ) as m:
            out, returned_sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                githash="x",
                prompt="hi",
                model="sonnet",
                cwd=cwd,
            )
        assert out == "recovered"
        assert returned_sid == sid
        assert m.call_count == 2
        first_argv = _argv(m.call_args_list[0])
        second_argv = _argv(m.call_args_list[1])
        assert "--resume" in first_argv
        assert "--session-id" in second_argv
        assert "--resume" not in second_argv

    def test_resume_failure_always_falls_back(self, fake_home: Path) -> None:
        """Any --resume non-zero exit triggers recreate, not just SESSION_EXPIRED.

        The deleted ``address_review`` code had ``or True`` on its fallback
        guard for exactly this reason: a transient CLI error on resume
        should still recreate rather than lose the call entirely. Quota
        detection lives one layer up in each phase's wrapper.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "x")
        _make_existing_jsonl(fake_home, cwd, sid)

        transient = subprocess.CalledProcessError(
            returncode=2, cmd=["claude"], output="", stderr="some unclassified error"
        )
        ok = MagicMock(stdout="ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[transient, ok],
        ) as m:
            out, _ = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                githash="x",
                prompt="hi",
                model="sonnet",
                cwd=cwd,
            )
        assert out == "ok"
        assert m.call_count == 2
        assert "--session-id" in _argv(m.call_args_list[1])

    def test_create_failure_propagates(self, fake_home: Path) -> None:
        """A first-call (--session-id) failure is not retried."""
        cwd = fake_home / "work"
        cwd.mkdir()
        other = subprocess.CalledProcessError(
            returncode=2, cmd=["claude"], output="", stderr="quota exhausted"
        )
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=other,
        ) as m:
            with pytest.raises(subprocess.CalledProcessError):
                invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    githash="x",
                    prompt="hi",
                    model="sonnet",
                    cwd=cwd,
                )
        assert m.call_count == 1


class TestArgvAssembly:
    """Optional flags appear in argv at the right positions."""

    def test_optional_flags(self, stub_run: MagicMock, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sys_prompt = fake_home / "sys.txt"
        sys_prompt.write_text("system")
        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            githash="x",
            prompt="hi",
            model="sonnet",
            cwd=cwd,
            system_prompt_file=sys_prompt,
            allowed_tools="Read,Glob,Grep",
            permission_mode="dontAsk",
            extra_args=["--foo"],
            output_format="json",
        )
        argv = _argv(stub_run.call_args)
        assert "--system-prompt" in argv
        assert str(sys_prompt) in argv
        assert "--allowedTools" in argv
        assert "Read,Glob,Grep" in argv
        assert "--permission-mode" in argv
        assert "dontAsk" in argv
        assert "--foo" in argv
        assert "--output-format" in argv
        assert "json" in argv
        # prompt is positional after --print
        assert argv[-2] == "--print"
        assert argv[-1] == "hi"

    def test_input_via_stdin_drops_prompt_from_argv(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            githash="x",
            prompt="the-prompt",
            model="sonnet",
            cwd=cwd,
            input_via_stdin=True,
        )
        argv = _argv(stub_run.call_args)
        assert "the-prompt" not in argv
        assert argv[-1] == "--print"
        # stdin kwarg carries the prompt
        kwargs = stub_run.call_args.kwargs
        assert kwargs["input"] == "the-prompt"

    def test_claudecode_env_cleared(
        self, stub_run: MagicMock, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        monkeypatch.setenv("CLAUDECODE", "1")
        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            githash="x",
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )
        passed_env = stub_run.call_args.kwargs["env"]
        assert passed_env["CLAUDECODE"] == ""


class TestRecreateOnResumeFailureToggle:
    """recreate_on_resume_failure=False propagates instead of falling back."""

    def test_propagates_called_process_error(self, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "x")
        _make_existing_jsonl(fake_home, cwd, sid)

        boom = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="session not found"
        )
        with patch("hephaestus.automation.claude_invoke.subprocess.run", side_effect=boom) as m:
            with pytest.raises(subprocess.CalledProcessError):
                invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    githash="x",
                    prompt="hi",
                    model="sonnet",
                    cwd=cwd,
                    recreate_on_resume_failure=False,
                )
        assert m.call_count == 1


class TestEndToEndSessionResume:
    """Two sequential invocations for the same tuple: create then resume.

    The first call has no JSONL — must use ``--session-id``. The mocked
    subprocess writes a JSONL on first call so the helper's existence
    probe will report True on the second call, which must then use
    ``--resume`` of the same UUID. This is the empirical proof that
    cross-iteration cache reuse will trigger.
    """

    def test_create_then_resume_lands_on_same_uuid(self, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        expected_sid = session_uuid("ProjectScylla", 1944, AGENT_PLANNER, "abc1234")

        # The first call's mock must write the JSONL on disk to simulate
        # what the real ``claude --session-id`` invocation does.
        encoded = str(cwd.resolve()).replace("/", "-")
        transcript_dir = fake_home / ".claude" / "projects" / encoded

        def _side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            (transcript_dir / f"{expected_sid}.jsonl").write_text("{}\n")
            return MagicMock(stdout="ok", stderr="", returncode=0)

        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run", side_effect=_side_effect
        ) as m:
            _, sid1 = invoke_claude_with_session(
                repo="ProjectScylla",
                issue=1944,
                agent=AGENT_PLANNER,
                githash="abc1234",
                prompt="iter 0",
                model="sonnet",
                cwd=cwd,
            )
            _, sid2 = invoke_claude_with_session(
                repo="ProjectScylla",
                issue=1944,
                agent=AGENT_PLANNER,
                githash="abc1234",
                prompt="iter 1",
                model="sonnet",
                cwd=cwd,
            )

        assert sid1 == sid2 == expected_sid
        assert m.call_count == 2

        first_argv = _argv(m.call_args_list[0])
        second_argv = _argv(m.call_args_list[1])

        assert "--session-id" in first_argv
        assert expected_sid in first_argv
        assert "--resume" not in first_argv

        assert "--resume" in second_argv
        assert expected_sid in second_argv
        assert "--session-id" not in second_argv

        # The prompts are distinct — the second call did NOT replay the first.
        assert first_argv[-1] == "iter 0"
        assert second_argv[-1] == "iter 1"

    def test_different_githash_starts_fresh_family(self, fake_home: Path) -> None:
        """A new trunk SHA must produce a different UUID (fresh session family)."""
        cwd = fake_home / "work"
        cwd.mkdir()
        sid_old = session_uuid("R", 1, AGENT_PLANNER, "abc1234")
        sid_new = session_uuid("R", 1, AGENT_PLANNER, "def5678")
        _make_existing_jsonl(fake_home, cwd, sid_old)

        with patch("hephaestus.automation.claude_invoke.subprocess.run") as m:
            m.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
            _, returned_sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                githash="def5678",  # new trunk SHA
                prompt="hi",
                model="sonnet",
                cwd=cwd,
            )

        assert returned_sid == sid_new
        assert returned_sid != sid_old
        argv = _argv(m.call_args)
        # No prior JSONL exists for the new SHA → must --session-id (create), not --resume.
        assert "--session-id" in argv
        assert sid_new in argv
        assert "--resume" not in argv
