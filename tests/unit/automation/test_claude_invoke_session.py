"""Unit tests for the deterministic-session invocation helper."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.agent_config import OPUS_48
from hephaestus.automation.claude_invoke import (
    invoke_claude_with_session,
    is_model_capped,
    reset_capped_models,
)
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


MODEL_CAP_MESSAGE = (
    "You've reached your Fable 5 limit. "
    "Run /usage-credits to continue or switch models with /model."
)


def _cap_error(stderr: str = MODEL_CAP_MESSAGE, stdout: str = "") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(returncode=1, cmd=["claude"], output=stdout, stderr=stderr)


def _cap_envelope() -> str:
    return json.dumps({"is_error": True, "api_error_status": 429, "result": MODEL_CAP_MESSAGE})


class TestModelCapFallback:
    """#1793: a model-specific usage cap falls back to the default model.

    The "reached your <model> limit … switch models with /model" 429 carries no
    reset epoch, so the wait-until-reset handlers can't help — the correct
    remediation is to retry once on :func:`agent_config.fallback_model` and pin
    the fallback for the rest of the process (sticky registry).
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        reset_capped_models()
        yield
        reset_capped_models()

    def test_called_process_error_falls_back_once(
        self, fake_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A cap CalledProcessError retries the SAME request on the fallback."""
        import logging

        cwd = fake_home / "work"
        cwd.mkdir()
        ok = MagicMock(stdout="fallback-ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[_cap_error(), ok],
        ) as m:
            with caplog.at_level(logging.WARNING, logger="hephaestus.automation.claude_invoke"):
                out, _sid = invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    prompt="hi",
                    model="claude-fable-5",
                    cwd=cwd,
                )
        assert out == "fallback-ok"
        assert m.call_count == 2
        first_argv = _argv(m.call_args_list[0])
        second_argv = _argv(m.call_args_list[1])
        assert first_argv[first_argv.index("--model") + 1] == "claude-fable-5"
        assert second_argv[second_argv.index("--model") + 1] == OPUS_48
        # Same prompt on both attempts — the request is retried, not dropped.
        assert first_argv[-1] == second_argv[-1] == "hi"
        assert any(
            "claude-fable-5" in r.getMessage() and OPUS_48 in r.getMessage() for r in caplog.records
        )

    def test_error_envelope_falls_back_once(self, fake_home: Path) -> None:
        """An exit-0 is_error:true cap envelope (json format) also falls back."""
        cwd = fake_home / "work"
        cwd.mkdir()
        capped = MagicMock(stdout=_cap_envelope(), stderr="", returncode=0)
        ok = MagicMock(stdout='{"result": "ok"}', stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[capped, ok],
        ) as m:
            out, _sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                cwd=cwd,
                output_format="json",
            )
        assert out == '{"result": "ok"}'
        assert m.call_count == 2
        second_argv = _argv(m.call_args_list[1])
        assert second_argv[second_argv.index("--model") + 1] == OPUS_48

    def test_registry_is_sticky_across_calls(self, fake_home: Path) -> None:
        """After a cap, later calls go straight to the fallback (1 attempt)."""
        cwd = fake_home / "work"
        cwd.mkdir()
        ok = MagicMock(stdout="ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[_cap_error(), ok],
        ):
            invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                cwd=cwd,
            )
        assert is_model_capped("claude-fable-5") is True
        with patch("hephaestus.automation.claude_invoke.subprocess.run", return_value=ok) as m2:
            invoke_claude_with_session(
                repo="R",
                issue=2,
                agent=AGENT_PLANNER,
                prompt="next",
                model="claude-fable-5",
                cwd=cwd,
            )
        assert m2.call_count == 1
        argv = _argv(m2.call_args)
        assert argv[argv.index("--model") + 1] == OPUS_48

    def test_no_fallback_loop_when_model_is_already_fallback(self, fake_home: Path) -> None:
        """A cap on the fallback model itself propagates — no retry loop."""
        cwd = fake_home / "work"
        cwd.mkdir()
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=_cap_error(),
        ) as m:
            with pytest.raises(subprocess.CalledProcessError):
                invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    prompt="hi",
                    model=OPUS_48,
                    cwd=cwd,
                )
        assert m.call_count == 1
        assert is_model_capped(OPUS_48) is False

    def test_fallback_also_capped_envelope_returned_verbatim(self, fake_home: Path) -> None:
        """If the fallback retry ALSO returns a cap envelope it is returned as-is.

        Callers' existing ``raise_for_error_envelope`` guards remain the
        backstop; the fallback model is never added to the registry.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        capped = MagicMock(stdout=_cap_envelope(), stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[capped, capped],
        ) as m:
            out, _sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                cwd=cwd,
                output_format="json",
            )
        assert m.call_count == 2
        assert out == _cap_envelope()
        assert is_model_capped(OPUS_48) is False

    def test_non_cap_failure_propagates_untouched(self, fake_home: Path) -> None:
        """A 529 overload (or any non-cap error) is NOT a fallback trigger."""
        cwd = fake_home / "work"
        cwd.mkdir()
        boom = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="API Error: 529 Overloaded"
        )
        with patch("hephaestus.automation.claude_invoke.subprocess.run", side_effect=boom) as m:
            with pytest.raises(subprocess.CalledProcessError):
                invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    prompt="hi",
                    model="claude-fable-5",
                    cwd=cwd,
                )
        assert m.call_count == 1

    def test_non_cap_error_envelope_returned_untouched(self, fake_home: Path) -> None:
        """A non-cap is_error envelope is returned verbatim (callers handle it)."""
        cwd = fake_home / "work"
        cwd.mkdir()
        envelope = json.dumps({"is_error": True, "result": "tool execution failed"})
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            return_value=MagicMock(stdout=envelope, stderr="", returncode=0),
        ) as m:
            out, _sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                cwd=cwd,
                output_format="json",
            )
        assert m.call_count == 1
        assert out == envelope

    def test_text_format_stdout_not_scanned(self, fake_home: Path) -> None:
        """Plain-text exit-0 output mentioning /usage-credits is NOT a cap.

        Agent prose can legitimately contain the phrase (e.g. this repo's own
        code under review); only the json error envelope is trusted on exit 0.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            return_value=MagicMock(
                stdout="the fix mentions /usage-credits handling", stderr="", returncode=0
            ),
        ) as m:
            out, _sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                cwd=cwd,
            )
        assert m.call_count == 1
        assert "usage-credits" in out
        assert is_model_capped("claude-fable-5") is False

    def test_fallback_gets_its_own_session_lineage(self, fake_home: Path) -> None:
        """The returned sid is the FALLBACK model's uuid (resume is model-locked)."""
        cwd = fake_home / "work"
        cwd.mkdir()
        ok = MagicMock(stdout="ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[_cap_error(), ok],
        ):
            _, sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                cwd=cwd,
            )
        assert sid == session_uuid("R", 1, AGENT_PLANNER, OPUS_48)

    def test_heph_fallback_model_env_override(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HEPH_FALLBACK_MODEL redirects the fallback target."""
        cwd = fake_home / "work"
        cwd.mkdir()
        monkeypatch.setenv("HEPH_FALLBACK_MODEL", "claude-haiku-4-5")
        ok = MagicMock(stdout="ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run",
            side_effect=[_cap_error(), ok],
        ) as m:
            invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                cwd=cwd,
            )
        second_argv = _argv(m.call_args_list[1])
        assert second_argv[second_argv.index("--model") + 1] == "claude-haiku-4-5"


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


class TestBrokenResumeRecreatesOnce:
    """#1780: a broken/truncated transcript recreates ONCE; 429s never do.

    Killed workers leave truncated session JSONLs; ``claude --resume`` of one
    exits 1 with EMPTY stderr. The mesh burned whole redelivery budgets
    re-failing on the same corrupt session. Quarantine + recreate-once is
    scoped so transient/quota failures still propagate unchanged.
    """

    def _invoke(self, cwd: Path) -> tuple[str, str]:
        return invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            prompt="hi",
            model="sonnet",
            cwd=cwd,
        )

    def test_empty_output_resume_failure_quarantines_and_recreates(self, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "sonnet")
        _make_existing_jsonl(fake_home, cwd, sid)
        err = subprocess.CalledProcessError(returncode=1, cmd=["claude"], output="", stderr="")
        ok = MagicMock(stdout="recovered", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run", side_effect=[err, ok]
        ) as m:
            stdout, _ = self._invoke(cwd)
        assert stdout == "recovered"
        assert m.call_count == 2
        retry_argv = _argv(m.call_args_list[1])
        assert "--session-id" in retry_argv
        assert "--resume" not in retry_argv
        # The corrupt transcript was quarantined out of the resume probe's path.
        assert not session_jsonl_path(sid, cwd).exists()

    def test_quota_failure_on_resume_still_propagates(self, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "sonnet")
        _make_existing_jsonl(fake_home, cwd, sid)
        err = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="429 rate limit exceeded"
        )
        with patch("hephaestus.automation.claude_invoke.subprocess.run", side_effect=err) as m:
            with pytest.raises(subprocess.CalledProcessError):
                self._invoke(cwd)
        assert m.call_count == 1
        assert session_jsonl_path(sid, cwd).exists()

    def test_expired_phrase_on_resume_recreates(self, fake_home: Path) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "sonnet")
        _make_existing_jsonl(fake_home, cwd, sid)
        err = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="No conversation found: cannot resume"
        )
        ok = MagicMock(stdout="fresh", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke.subprocess.run", side_effect=[err, ok]
        ) as m:
            stdout, _ = self._invoke(cwd)
        assert stdout == "fresh"
        assert m.call_count == 2
