"""Unit tests for the deterministic-session invocation helper."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.claude_invoke import invoke_claude_with_session
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


class TestAlwaysResume:
    """#1166: every call uses --resume; the harness creates on first use."""

    def test_call_always_uses_resume_never_create(
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
        assert "--resume" in argv
        assert sid in argv
        # No create path anymore: no --session-id / --name probing.
        assert "--session-id" not in argv
        assert "--name" not in argv
        assert out == "ok"
        # The session id includes the model (#1166).
        assert sid == session_uuid("R", 1, AGENT_PLANNER, "sonnet")

    def test_resume_is_used_even_without_existing_transcript(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        """No transcript on disk → still --resume (harness creates it lazily)."""
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
        argv = _argv(stub_run.call_args)
        assert "--resume" in argv
        assert "--session-id" not in argv

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

    def test_resume_failure_propagates(self, fake_home: Path) -> None:
        """A --resume non-zero exit is raised; there is no recreate fallback."""
        cwd = fake_home / "work"
        cwd.mkdir()
        err = subprocess.CalledProcessError(
            returncode=2, cmd=["claude"], output="", stderr="some error"
        )
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run", side_effect=err
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
        # Exactly one attempt — no create/recreate/fresh cascade.
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
            with patch(
                "hephaestus.automation.claude_invoke.subprocess.run", side_effect=boom
            ) as m:
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
    """Two sequential invocations for the same tuple: create then resume.

    The first call has no JSONL — must use ``--session-id``. The mocked
    subprocess writes a JSONL on first call so the helper's existence
    probe will report True on the second call, which must then use
    ``--resume`` of the same UUID. This is the empirical proof that
    cross-iteration cache reuse will trigger.
    """

    def test_both_calls_resume_same_uuid_distinct_prompts(self, fake_home: Path) -> None:
        """Two calls for the same key both --resume the same uuid (#1166).

        The id includes the model. Both invocations --resume (the harness
        created it on the first), and the prompts are distinct — proving the
        second call continues the transcript rather than replaying the first.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        expected_sid = session_uuid("ProjectScylla", 1944, AGENT_PLANNER, "sonnet")

        with patch("hephaestus.automation.claude_invoke.subprocess.run") as m:
            m.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
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
        # Both calls --resume the same id; no create path.
        for argv in (first_argv, second_argv):
            assert "--resume" in argv
            assert expected_sid in argv
            assert "--session-id" not in argv
        # The prompts are distinct — the second call did NOT replay the first.
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
