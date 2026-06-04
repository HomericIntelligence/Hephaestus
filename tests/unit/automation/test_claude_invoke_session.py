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
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )
        argv = _argv(stub_run.call_args)
        assert "--session-id" in argv
        assert "--name" in argv
        assert "--resume" not in argv
        assert out == "ok"
        assert sid == session_uuid("R", 1, AGENT_PLANNER)

    def test_subsequent_call_uses_resume(self, stub_run: MagicMock, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER)
        _make_existing_jsonl(fake_home, cwd, sid)

        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
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
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )
        _, sid_reviewer = invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLAN_REVIEWER,
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
        sid = session_uuid("R", 1, AGENT_PLANNER)
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
        sid = session_uuid("R", 1, AGENT_PLANNER)
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
                prompt="hi",
                model="sonnet",
                cwd=cwd,
            )
        assert out == "ok"
        assert m.call_count == 2
        assert "--session-id" in _argv(m.call_args_list[1])

    def test_resume_then_recreate_collision_falls_back_to_fresh_session(
        self, fake_home: Path
    ) -> None:
        """Resume fails, recreate collides 'already in use' → fresh uuid4 session.

        Regression: the resume path recreated with the SAME deterministic sid,
        so under concurrency a sibling worker holding that session made the
        recreate collide too, and the error propagated as "Session ID … is
        already in use" (observed: planner ProjectHermes). It must instead fall
        back to a brand-new unique session, like the no-transcript path.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER)
        _make_existing_jsonl(fake_home, cwd, sid)

        resume_fail = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="transient resume error"
        )
        recreate_collision = subprocess.CalledProcessError(
            returncode=1,
            cmd=["claude"],
            output="",
            stderr="Error: Session ID is already in use.",
        )
        ok = MagicMock(stdout="fresh-ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[resume_fail, recreate_collision, ok],
        ) as m:
            out, returned_sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="sonnet",
                cwd=cwd,
            )
        assert out == "fresh-ok"
        # A brand-new uuid4 session, not the contended deterministic one.
        assert returned_sid != sid
        assert m.call_count == 3
        # The final call creates the fresh session with that new id.
        final_argv = _argv(m.call_args_list[2])
        assert "--session-id" in final_argv
        assert returned_sid in final_argv

    def test_create_failure_propagates(self, fake_home: Path) -> None:
        """A first-call (--session-id) failure for an unrelated reason is not retried."""
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
                    prompt="hi",
                    model="sonnet",
                    cwd=cwd,
                )
        assert m.call_count == 1

    def test_create_already_in_use_falls_back_to_resume(self, fake_home: Path) -> None:
        """Fall back to --resume when --session-id is rejected as already-in-use.

        Defends against encoding drift between hephaestus's transcript probe
        and the Claude CLI's actual session storage. (#822)
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        already = subprocess.CalledProcessError(
            returncode=1,
            cmd=["claude"],
            output="",
            stderr="Error: Session ID abc is already in use.",
        )
        ok = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="resumed-ok", stderr=""
        )
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[already, ok],
        ) as m:
            stdout, _ = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="sonnet",
                cwd=cwd,
            )
        assert stdout == "resumed-ok"
        assert m.call_count == 2
        first_argv = m.call_args_list[0][0][0]
        second_argv = m.call_args_list[1][0][0]
        assert "--session-id" in first_argv
        assert "--resume" in second_argv

    def test_in_use_then_resume_fails_creates_fresh_session(self, fake_home: Path) -> None:
        """Create in-use → resume keeps failing → fall back to a FRESH session.

        Under 3 parallel CI-fix workers, two can race on the same deterministic
        session UUID; the loser hits "already in use" and resume can also fail
        while the sibling is still initializing. Rather than aborting the PR
        (observed: ProjectHermes #647), derive a fresh unique session.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        already = subprocess.CalledProcessError(
            returncode=1,
            cmd=["claude"],
            output="",
            stderr="Error: Session ID abc is already in use.",
        )
        resume_fail = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="cannot resume: locked"
        )
        fresh_ok = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="fresh-ok", stderr=""
        )
        # create(in-use) → resume×3 fail → fresh create ok
        with (
            patch(
                "hephaestus.automation.claude_invoke.subprocess.run",
                side_effect=[already, resume_fail, resume_fail, resume_fail, fresh_ok],
            ) as m,
            patch("hephaestus.automation.claude_invoke.time.sleep"),
        ):
            stdout, returned_sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="sonnet",
                cwd=cwd,
            )
        assert stdout == "fresh-ok"
        # 1 create + 3 resume + 1 fresh create
        assert m.call_count == 5
        # The final call is a fresh --session-id create with a DIFFERENT id.
        final_argv = m.call_args_list[-1][0][0]
        assert "--session-id" in final_argv
        fresh_sid = final_argv[final_argv.index("--session-id") + 1]
        assert returned_sid == fresh_sid
        # The deterministic id and the fresh id must differ.
        orig_sid = m.call_args_list[0][0][0][m.call_args_list[0][0][0].index("--session-id") + 1]
        assert fresh_sid != orig_sid


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
        sid = session_uuid("R", 1, AGENT_PLANNER)
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
        expected_sid = session_uuid("ProjectScylla", 1944, AGENT_PLANNER)

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
                prompt="iter 0",
                model="sonnet",
                cwd=cwd,
            )
            _, sid2 = invoke_claude_with_session(
                repo="ProjectScylla",
                issue=1944,
                agent=AGENT_PLANNER,
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

    def test_session_id_is_githash_invariant(self, fake_home: Path) -> None:
        """The session UUID depends only on (repo, issue, agent) — #841.

        Regression for #841: the prior behavior fed ``current_trunk_githash``
        into the session-naming tuple, so every main-bump forked a new
        session family. The whole loop is now PR/issue-scoped: the same
        (repo, issue, agent) tuple must always resume the same transcript.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        sid_first_call = session_uuid("R", 1, AGENT_PLANNER)

        # Pre-populate the transcript so the call resumes rather than creates.
        _make_existing_jsonl(fake_home, cwd, sid_first_call)

        with patch("hephaestus.automation.claude_invoke.subprocess.run") as m:
            m.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
            _, returned_sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="sonnet",
                cwd=cwd,
            )

        assert returned_sid == sid_first_call
        argv = _argv(m.call_args)
        # Existing transcript → resume the same session, never create a new one.
        assert "--resume" in argv
        assert sid_first_call in argv
        assert "--session-id" not in argv
