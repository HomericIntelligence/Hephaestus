"""Unit tests for the deterministic-session invocation helper."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.claude_invoke import invoke_claude_with_session
from hephaestus.automation.session_naming import (
    AGENT_PLAN_REVIEWER,
    AGENT_PLANNER,
    session_jsonl_path,
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
    """Pre-create the transcript file so the helper takes the --resume path."""
    del home
    target = session_jsonl_path(sid, cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n")


class TestCreateThenResume:
    """#1168: first call --session-id (create), later calls --resume.

    ``claude --resume`` does NOT auto-create — it errors "No conversation found"
    for an unknown id — so the first call for a (repo, issue, agent, model) key
    must create the session, and later calls resume it.
    """

    def test_first_call_creates_with_model_keyed_id(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
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
        # No transcript yet → create path.
        assert "--session-id" in argv
        assert "--name" in argv
        assert "--resume" not in argv
        assert sid in argv
        assert out == "ok"
        # The session id includes the model (#1166).
        assert sid == session_uuid("R", 1, AGENT_PLANNER, "sonnet")

    def test_subsequent_call_resumes_existing_transcript(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "sonnet")
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
        # Transcript exists → resume path; no re-create.
        assert "--resume" in argv
        assert sid in argv
        assert "--session-id" not in argv
        assert "--name" not in argv

    def test_different_models_get_different_uuids(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        """Switching the model gives a DIFFERENT session id (#1166).

        --resume is locked to the creating model, so each model must have its own
        create-once-then-resume lineage; the id therefore varies by model.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        _, sid_sonnet = invoke_claude_with_session(
            repo="R", issue=1, agent=AGENT_PLANNER, prompt="hi", model="sonnet", cwd=cwd
        )
        _, sid_opus = invoke_claude_with_session(
            repo="R", issue=1, agent=AGENT_PLANNER, prompt="hi", model="opus", cwd=cwd
        )
        assert sid_sonnet != sid_opus
        assert sid_sonnet == session_uuid("R", 1, AGENT_PLANNER, "sonnet")
        assert sid_opus == session_uuid("R", 1, AGENT_PLANNER, "opus")

    def test_different_agents_get_different_uuids(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        _, sid_planner = invoke_claude_with_session(
            repo="R", issue=1, agent=AGENT_PLANNER, prompt="hi", model="sonnet", cwd=cwd
        )
        _, sid_reviewer = invoke_claude_with_session(
            repo="R", issue=1, agent=AGENT_PLAN_REVIEWER, prompt="hi", model="sonnet", cwd=cwd
        )
        assert sid_planner != sid_reviewer

    def test_failure_propagates_without_recreate_cascade(self, fake_home: Path) -> None:
        """A create/resume non-zero exit is raised; no recreate/fresh fallback."""
        cwd = fake_home / "work"
        cwd.mkdir()
        err = subprocess.CalledProcessError(
            returncode=2, cmd=["claude"], output="", stderr="some error"
        )
        with patch("hephaestus.automation.claude_invoke.subprocess.run", side_effect=err) as m:
            with pytest.raises(subprocess.CalledProcessError):
                invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    prompt="hi",
                    model="sonnet",
                    cwd=cwd,
                )
        # Exactly one attempt — no recreate/fresh cascade.
        assert m.call_count == 1


class TestArgvAssembly:
    """Optional flags appear in argv at the right positions."""

    def test_omitted_timeout_uses_centralized_plan_timeout(
        self,
        stub_run: MagicMock,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Omitted timeout resolves through the centralized timeout helper."""
        monkeypatch.setenv("HEPH_AGENT_PLAN_TIMEOUT", "333")
        cwd = fake_home / "work"
        cwd.mkdir()

        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )

        assert stub_run.call_args.kwargs["timeout"] == 333

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
    """recreate_on_resume_failure is a back-compat no-op now (#1166).

    The always-resume model never recreates, so the toggle's value no longer
    changes behavior — a --resume failure always propagates as a single call.
    The kwarg is retained only so existing callers keep working.
    """

    def test_toggle_is_accepted_and_call_propagates(self, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        boom = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="session not found"
        )
        for toggle in (True, False):
            with patch("hephaestus.automation.claude_invoke.subprocess.run", side_effect=boom) as m:
                with pytest.raises(subprocess.CalledProcessError):
                    invoke_claude_with_session(
                        repo="R",
                        issue=1,
                        agent=AGENT_PLANNER,
                        prompt="hi",
                        model="sonnet",
                        cwd=cwd,
                        recreate_on_resume_failure=toggle,
                    )
            assert m.call_count == 1  # single attempt regardless of toggle


class TestEndToEndSessionResume:
    """Two sequential invocations for the same key: create then resume (#1168).

    The first call has no JSONL → ``--session-id`` (create). The mocked
    subprocess writes the JSONL on the first call so the existence probe reports
    True on the second, which must then ``--resume`` the same UUID. Empirical
    proof that cross-iteration cache reuse triggers.
    """

    def test_create_then_resume_same_uuid_distinct_prompts(self, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        expected_sid = session_uuid("ProjectScylla", 1944, AGENT_PLANNER, "sonnet")

        # First call writes the transcript so the second call's probe finds it.
        def _side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            _make_existing_jsonl(fake_home, cwd, expected_sid)
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
        # First creates, second resumes — same id.
        assert "--session-id" in first_argv
        assert expected_sid in first_argv
        assert "--resume" not in first_argv
        assert "--resume" in second_argv
        assert expected_sid in second_argv
        assert "--session-id" not in second_argv
        # Distinct prompts — the second call did NOT replay the first.
        assert first_argv[-1] == "iter 0"
        assert second_argv[-1] == "iter 1"

    def test_session_id_is_githash_invariant(self, fake_home: Path) -> None:
        """The session UUID depends only on (repo, issue, agent, model) — #841/#1166.

        Regression for #841: the prior behavior fed ``current_trunk_githash``
        into the session-naming tuple, so every main-bump forked a new session
        family. The loop is PR/issue-scoped: the same (repo, issue, agent, model)
        key must always resume the same transcript regardless of the trunk SHA.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        expected_sid = session_uuid("R", 1, AGENT_PLANNER, "sonnet")
        # Existing transcript → resume path.
        _make_existing_jsonl(fake_home, cwd, expected_sid)

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

        assert returned_sid == expected_sid
        argv = _argv(m.call_args)
        assert "--resume" in argv
        assert expected_sid in argv
        assert "--session-id" not in argv
        assert "--session-id" not in argv


class TestPromptNullByteSanitization:
    r"""#1661: a NUL byte in the prompt must not crash the invoke.

    subprocess.run raises ``ValueError: embedded null byte`` if any argv element
    (or text stdin) contains ``\x00``. The prompt is assembled from untrusted
    multi-source text (issue body + advise/agent output + prior review), so a
    single stray NUL would otherwise permanently strand the issue.
    """

    def test_argv_prompt_has_no_null_byte(self, stub_run: MagicMock, fake_home: Path) -> None:
        """A NUL in the prompt is stripped before it reaches the argv."""
        cwd = fake_home / "work"
        cwd.mkdir()
        invoke_claude_with_session(
            repo="R",
            issue=1509,
            agent=AGENT_PLANNER,
            prompt="plan this\x00issue",
            model="sonnet",
            cwd=cwd,
        )
        argv = _argv(stub_run.call_args)
        assert all("\x00" not in arg for arg in argv)
        # The prompt is the last positional argv element (after --print).
        assert argv[-1] == "plan thisissue"

    def test_stdin_prompt_has_no_null_byte(self, stub_run: MagicMock, fake_home: Path) -> None:
        """A NUL is stripped on the stdin path too (input_via_stdin=True)."""
        cwd = fake_home / "work"
        cwd.mkdir()
        invoke_claude_with_session(
            repo="R",
            issue=1509,
            agent=AGENT_PLANNER,
            prompt="plan this\x00issue",
            model="sonnet",
            cwd=cwd,
            input_via_stdin=True,
        )
        kwargs = stub_run.call_args.kwargs
        assert kwargs["input"] == "plan thisissue"
        assert "\x00" not in kwargs["input"]

    def test_real_subprocess_does_not_raise_with_null_byte(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end regression: the real subprocess.run path tolerates a NUL.

        Reproduces the #1509 crash. We point the invoked binary at a portable
        no-op (``sys.executable -c ""``, always present — unlike ``true``) so the
        call succeeds; WITHOUT the fix, argv marshaling raises
        ``ValueError: embedded null byte`` here and never reaches the child.
        """
        cwd = fake_home / "work"
        cwd.mkdir()

        real_run = subprocess.run
        noop = [sys.executable, "-c", ""]

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            # Swap the "claude" binary for a guaranteed no-op while preserving the
            # rest of argv verbatim — so the real argv/stdin marshaling (which
            # raised the original ValueError) is still exercised.
            return real_run([*noop, *cmd[1:]], **kwargs)

        monkeypatch.setattr("hephaestus.automation.claude_invoke.subprocess.run", fake_run)

        out, _sid = invoke_claude_with_session(
            repo="R",
            issue=1509,
            agent=AGENT_PLANNER,
            prompt="plan this\x00issue",
            model="sonnet",
            cwd=cwd,
        )
        assert out == ""
