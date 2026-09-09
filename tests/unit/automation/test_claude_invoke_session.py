"""Unit tests for the deterministic-session invocation helper."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Generator
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.session_errors import AgentSessionLostError
from hephaestus.automation import claude_invoke
from hephaestus.automation.claude_invoke import (
    _session_expired,
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

FALLBACK_MODEL = "claude-opus-4-8"


def _argv(call_args_list_entry: Any) -> list[str]:
    """Extract argv from a ``subprocess.run`` call recorded by mock (the _run_tracked seam)."""
    if hasattr(call_args_list_entry, "args"):
        call_args = call_args_list_entry.args
    else:
        call_args = call_args_list_entry[0]
    return list(call_args[0])


@pytest.fixture
def stub_run() -> Generator[MagicMock]:
    """Patch subprocess.run to return a successful result."""
    with patch("hephaestus.automation.claude_invoke._run_tracked") as m:
        m.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        yield m


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME so session_jsonl_path resolves under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "hephaestus.automation.agent_config._checkout_identity",
        lambda cwd: sha256(str(cwd.resolve()).encode()).hexdigest(),
    )
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

    @pytest.mark.parametrize("agent", [AGENT_PLANNER, AGENT_PLAN_REVIEWER])
    @pytest.mark.parametrize("require_new", [False, True])
    def test_ordinary_retry_reuses_transcript_but_durable_start_rejects_it(
        self, stub_run: MagicMock, fake_home: Path, agent: str, require_new: bool
    ) -> None:
        """Only a durable new cycle rejects an existing matching transcript."""
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("repo", 1, agent, "Model", cwd=cwd)
        _make_existing_jsonl(fake_home, cwd, sid)
        kwargs: dict[str, Any] = {
            "repo": "repo",
            "issue": 1,
            "agent": agent,
            "prompt": "retry",
            "model": "Model",
            "cwd": cwd,
            "session_lifecycle": "start_new",
            "require_new_session": require_new,
        }
        if require_new:
            with pytest.raises(AgentSessionLostError, match="collided"):
                invoke_claude_with_session(**kwargs)
            stub_run.assert_not_called()
        else:
            _, resumed_sid = invoke_claude_with_session(**kwargs)
            assert resumed_sid == sid
            assert "--resume" in _argv(stub_run.call_args)

    def test_resume_required_missing_transcript_fails_closed(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        """Strict iterative review cannot downgrade a missing resume to create."""
        cwd = fake_home / "work"
        cwd.mkdir()
        with pytest.raises(AgentSessionLostError, match="transcript is missing"):
            invoke_claude_with_session(
                repo="repo",
                issue=2766,
                agent="plan-reviewer-cycle-01234567-89ab-cdef-0123-456789abcdef",
                prompt="review",
                model="model",
                cwd=cwd,
                session_lifecycle="resume-required",
            )
        stub_run.assert_not_called()

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
        assert sid == session_uuid("R", 1, AGENT_PLANNER, "sonnet", cwd=cwd)

    def test_subsequent_call_resumes_existing_transcript(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        sid = session_uuid("R", 1, AGENT_PLANNER, "sonnet", cwd=cwd)
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

    def test_inline_effort_uses_the_claude_model_default(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        """Claude strips the effort before it builds the command and session key."""
        cwd = fake_home / "work"
        cwd.mkdir()

        _, sid = invoke_claude_with_session(
            repo="R",
            issue=2,
            agent=AGENT_PLANNER,
            prompt="hi",
            model="claude-sonnet-5:future-effort",
            cwd=cwd,
        )

        argv = _argv(stub_run.call_args)
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
        assert sid == session_uuid("R", 2, AGENT_PLANNER, "claude-sonnet-5", cwd=cwd)

    def test_empty_model_base_omits_the_claude_model_option(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        """An effort-only reference lets Claude select its provider default."""
        cwd = fake_home / "work"
        cwd.mkdir()

        _, sid = invoke_claude_with_session(
            repo="R",
            issue=3,
            agent=AGENT_PLANNER,
            prompt="hi",
            model=":provider-default",
            cwd=cwd,
        )

        assert "--model" not in _argv(stub_run.call_args)
        assert sid == session_uuid("R", 3, AGENT_PLANNER, "", cwd=cwd)

    def test_lossy_path_collision_does_not_resume_unregistered_checkout(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        """Dotted/dashed checkout path collisions stay isolated by checkout id."""
        dotted = fake_home / "owner.a" / "Repo"
        dashed = fake_home / "owner-a" / "Repo"
        dotted.mkdir(parents=True)
        dashed.mkdir(parents=True)

        dotted_sid = session_uuid("Repo", 2284, AGENT_PLANNER, "sonnet", cwd=dotted)
        dashed_sid = session_uuid("Repo", 2284, AGENT_PLANNER, "sonnet", cwd=dashed)
        assert dotted_sid != dashed_sid

        dotted_path = session_jsonl_path(dotted_sid, dotted)
        dashed_path = session_jsonl_path(dashed_sid, dashed)
        assert dotted_path.parent == dashed_path.parent
        dashed_path.parent.mkdir(parents=True, exist_ok=True)
        dashed_path.write_text("{}\n", encoding="utf-8")

        with patch(
            "hephaestus.automation.agent_config._registered_worktree_roots",
            return_value=(dotted.resolve(),),
        ):
            _, sid = invoke_claude_with_session(
                repo="Repo",
                issue=2284,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="sonnet",
                cwd=dotted,
            )

        argv = _argv(stub_run.call_args)
        assert sid == dotted_sid
        assert "--session-id" in argv
        assert dotted_sid in argv
        assert "--resume" not in argv
        assert dashed_sid not in argv

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
        assert sid_sonnet == session_uuid("R", 1, AGENT_PLANNER, "sonnet", cwd=cwd)
        assert sid_opus == session_uuid("R", 1, AGENT_PLANNER, "opus", cwd=cwd)

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
        with patch("hephaestus.automation.claude_invoke._run_tracked", side_effect=err) as m:
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

    def test_session_expired_detects_no_conversation_found(self) -> None:
        assert _session_expired("No conversation found with session ID abc", "") is True


class TestArgvAssembly:
    """Optional flags appear in argv at the right positions."""

    def test_omitted_timeout_uses_generic_default_not_planner(
        self,
        stub_run: MagicMock,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Omitted timeout resolves to the generic default, not planner's (#1415).

        This shared entry point is used by every agent type, so a planner-only
        budget must not leak into a non-planner caller that omits ``timeout``.
        """
        # Planner-specific override must NOT influence the generic fallback.
        monkeypatch.setenv("HEPH_AGENT_PLAN_TIMEOUT", "333")
        # The removed generic override is inert; the explicit value applies.
        monkeypatch.setenv("HEPH_AGENT_DEFAULT_TIMEOUT", "4321")
        cwd = fake_home / "work"
        cwd.mkdir()

        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            prompt="hi",
            model="sonnet",
            cwd=cwd,
            timeout=4321,
        )

        stub_run.assert_called_once()
        run_kwargs = stub_run.call_args.kwargs
        assert run_kwargs["timeout"] == 4321

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

    def test_empty_allowed_tools_is_forwarded_as_zero_tool_scope(
        self, stub_run: MagicMock, fake_home: Path
    ) -> None:
        """An explicit empty scope reaches Claude instead of restoring CLI defaults."""
        cwd = fake_home / "work"
        cwd.mkdir()

        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            prompt="hi",
            model="sonnet",
            cwd=cwd,
            allowed_tools="",
            permission_mode="dontAsk",
        )

        argv = _argv(stub_run.call_args)
        allowed_tools_index = argv.index("--allowedTools")
        assert argv[allowed_tools_index + 1] == ""
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"

    @pytest.mark.parametrize(
        "prompt",
        [
            pytest.param("the-prompt", id="ordinary"),
            pytest.param("sensitive-large-prompt:" + ("x" * 200_000), id="large"),
        ],
    )
    def test_input_via_stdin_drops_prompt_from_argv(
        self, stub_run: MagicMock, fake_home: Path, prompt: str
    ) -> None:
        cwd = fake_home / "work"
        cwd.mkdir()
        invoke_claude_with_session(
            repo="R",
            issue=1,
            agent=AGENT_PLANNER,
            prompt=prompt,
            model="sonnet",
            cwd=cwd,
            input_via_stdin=True,
        )
        argv = _argv(stub_run.call_args)
        assert all(prompt not in argument for argument in argv)
        assert argv[-1] == "--print"
        kwargs = stub_run.call_args.kwargs
        assert kwargs["stdin_text"] == prompt
        assert kwargs["use_devnull_stdin"] is False

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
        expected_sid = session_uuid("Scylla", 1944, AGENT_PLANNER, "sonnet", cwd=cwd)

        # First call writes the transcript so the second call's probe finds it.
        def _side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            _make_existing_jsonl(fake_home, cwd, expected_sid)
            return MagicMock(stdout="ok", stderr="", returncode=0)

        with patch(
            "hephaestus.automation.claude_invoke._run_tracked", side_effect=_side_effect
        ) as m:
            _, sid1 = invoke_claude_with_session(
                repo="Scylla",
                issue=1944,
                agent=AGENT_PLANNER,
                prompt="iter 0",
                model="sonnet",
                cwd=cwd,
            )
            _, sid2 = invoke_claude_with_session(
                repo="Scylla",
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

    def test_stdin_create_then_resume_same_uuid_distinct_prompts(self, fake_home: Path) -> None:
        """Stdin prompts preserve the create/resume session transition."""
        cwd = fake_home / "work"
        cwd.mkdir()
        expected_sid = session_uuid("Scylla", 1944, AGENT_PLANNER, "sonnet", cwd=cwd)

        def _side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            _make_existing_jsonl(fake_home, cwd, expected_sid)
            return MagicMock(stdout="ok", stderr="", returncode=0)

        with patch(
            "hephaestus.automation.claude_invoke._run_tracked", side_effect=_side_effect
        ) as m:
            _, sid1 = invoke_claude_with_session(
                repo="Scylla",
                issue=1944,
                agent=AGENT_PLANNER,
                prompt="iter 0",
                model="sonnet",
                cwd=cwd,
                input_via_stdin=True,
            )
            _, sid2 = invoke_claude_with_session(
                repo="Scylla",
                issue=1944,
                agent=AGENT_PLANNER,
                prompt="iter 1",
                model="sonnet",
                cwd=cwd,
                input_via_stdin=True,
            )

        assert sid1 == sid2 == expected_sid
        first_argv = _argv(m.call_args_list[0])
        second_argv = _argv(m.call_args_list[1])
        assert "--session-id" in first_argv
        assert "--resume" in second_argv
        assert m.call_args_list[0].kwargs["stdin_text"] == "iter 0"
        assert m.call_args_list[1].kwargs["stdin_text"] == "iter 1"
        assert all("iter 0" not in argument for argument in first_argv)
        assert all("iter 1" not in argument for argument in second_argv)

    def test_session_id_is_githash_invariant(self, fake_home: Path) -> None:
        """The session UUID omits the trunk SHA — #841/#1166.

        Regression for #841: the prior behavior fed ``current_trunk_githash``
        into the session-naming tuple, so every main-bump forked a new session
        family. The loop is PR/issue-scoped within one checkout: the same
        (repo, issue, agent, model) key must always resume the same transcript
        regardless of the trunk SHA.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        expected_sid = session_uuid("R", 1, AGENT_PLANNER, "sonnet", cwd=cwd)
        # Existing transcript → resume path.
        _make_existing_jsonl(fake_home, cwd, expected_sid)

        with patch("hephaestus.automation.claude_invoke._run_tracked") as m:
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
    """A model usage cap retries once with an explicit fallback model.

    Later calls can use the fallback when the caller supplies it again.
    Durable session operations keep their original model identity.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None]:
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
            "hephaestus.automation.claude_invoke._run_tracked",
            side_effect=[_cap_error(), ok],
        ) as m:
            with caplog.at_level(logging.WARNING, logger="hephaestus.automation.claude_invoke"):
                out, _sid = invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    prompt="hi",
                    model="claude-fable-5",
                    fallback_model_value="claude-opus-4-8:future-effort",
                    cwd=cwd,
                )
        assert out == "fallback-ok"
        assert m.call_count == 2
        first_argv = _argv(m.call_args_list[0])
        second_argv = _argv(m.call_args_list[1])
        assert first_argv[first_argv.index("--model") + 1] == "claude-fable-5"
        assert second_argv[second_argv.index("--model") + 1] == FALLBACK_MODEL
        # Same prompt on both attempts — the request is retried, not dropped.
        assert first_argv[-1] == second_argv[-1] == "hi"
        assert any(
            "claude-fable-5" in r.getMessage() and FALLBACK_MODEL in r.getMessage()
            for r in caplog.records
        )

    def test_stdin_prompt_is_preserved_across_model_cap_fallback(self, fake_home: Path) -> None:
        """A stdin request is retried with the same prompt and no argv leak."""
        cwd = fake_home / "work"
        cwd.mkdir()
        prompt = "sensitive fallback prompt"
        ok = MagicMock(stdout="fallback-ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke._run_tracked",
            side_effect=[_cap_error(), ok],
        ) as tracked:
            out, sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt=prompt,
                model="claude-fable-5",
                fallback_model_value=FALLBACK_MODEL,
                cwd=cwd,
                input_via_stdin=True,
            )

        assert out == "fallback-ok"
        assert sid == session_uuid("R", 1, AGENT_PLANNER, FALLBACK_MODEL, cwd=cwd)
        assert [attempt.kwargs["stdin_text"] for attempt in tracked.call_args_list] == [
            prompt,
            prompt,
        ]
        assert all(
            prompt not in argument
            for attempt in tracked.call_args_list
            for argument in _argv(attempt)
        )

    def test_error_envelope_falls_back_once(self, fake_home: Path) -> None:
        """An exit-0 is_error:true cap envelope (json format) also falls back."""
        cwd = fake_home / "work"
        cwd.mkdir()
        capped = MagicMock(stdout=_cap_envelope(), stderr="", returncode=0)
        ok = MagicMock(stdout='{"result": "ok"}', stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke._run_tracked",
            side_effect=[capped, ok],
        ) as m:
            out, _sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                fallback_model_value=FALLBACK_MODEL,
                cwd=cwd,
                output_format="json",
            )
        assert out == '{"result": "ok"}'
        assert m.call_count == 2
        second_argv = _argv(m.call_args_list[1])
        assert second_argv[second_argv.index("--model") + 1] == FALLBACK_MODEL

    def test_registry_is_sticky_across_calls(self, fake_home: Path) -> None:
        """After a cap, later calls go straight to the fallback (1 attempt)."""
        cwd = fake_home / "work"
        cwd.mkdir()
        ok = MagicMock(stdout="ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke._run_tracked",
            side_effect=[_cap_error(), ok],
        ):
            invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                fallback_model_value=FALLBACK_MODEL,
                cwd=cwd,
            )
        assert is_model_capped("claude-fable-5") is True
        with patch("hephaestus.automation.claude_invoke._run_tracked", return_value=ok) as m2:
            invoke_claude_with_session(
                repo="R",
                issue=2,
                agent=AGENT_PLANNER,
                prompt="next",
                model="claude-fable-5",
                fallback_model_value=FALLBACK_MODEL,
                cwd=cwd,
            )
        assert m2.call_count == 1
        argv = _argv(m2.call_args)
        assert argv[argv.index("--model") + 1] == FALLBACK_MODEL

    def test_no_fallback_loop_when_model_is_already_fallback(self, fake_home: Path) -> None:
        """A cap on the fallback model itself propagates — no retry loop."""
        cwd = fake_home / "work"
        cwd.mkdir()
        with patch(
            "hephaestus.automation.claude_invoke._run_tracked",
            side_effect=_cap_error(),
        ) as m:
            with pytest.raises(subprocess.CalledProcessError):
                invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    prompt="hi",
                    model=FALLBACK_MODEL,
                    fallback_model_value=FALLBACK_MODEL,
                    cwd=cwd,
                )
        assert m.call_count == 1
        assert is_model_capped(FALLBACK_MODEL) is False

    def test_fallback_also_capped_envelope_returned_verbatim(self, fake_home: Path) -> None:
        """If the fallback retry ALSO returns a cap envelope it is returned as-is.

        Callers' existing ``raise_for_error_envelope`` guards remain the
        backstop; the fallback model is never added to the registry.
        """
        cwd = fake_home / "work"
        cwd.mkdir()
        capped = MagicMock(stdout=_cap_envelope(), stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke._run_tracked",
            side_effect=[capped, capped],
        ) as m:
            out, _sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                fallback_model_value=FALLBACK_MODEL,
                cwd=cwd,
                output_format="json",
            )
        assert m.call_count == 2
        assert out == _cap_envelope()
        assert is_model_capped(FALLBACK_MODEL) is False

    def test_non_cap_failure_propagates_untouched(self, fake_home: Path) -> None:
        """A 529 overload (or any non-cap error) is NOT a fallback trigger."""
        cwd = fake_home / "work"
        cwd.mkdir()
        boom = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="API Error: 529 Overloaded"
        )
        with patch("hephaestus.automation.claude_invoke._run_tracked", side_effect=boom) as m:
            with pytest.raises(subprocess.CalledProcessError):
                invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    prompt="hi",
                    model="claude-fable-5",
                    fallback_model_value=FALLBACK_MODEL,
                    cwd=cwd,
                )
        assert m.call_count == 1

    def test_non_cap_error_envelope_returned_untouched(self, fake_home: Path) -> None:
        """A non-cap is_error envelope is returned verbatim (callers handle it)."""
        cwd = fake_home / "work"
        cwd.mkdir()
        envelope = json.dumps({"is_error": True, "result": "tool execution failed"})
        with patch(
            "hephaestus.automation.claude_invoke._run_tracked",
            return_value=MagicMock(stdout=envelope, stderr="", returncode=0),
        ) as m:
            out, _sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                fallback_model_value=FALLBACK_MODEL,
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
            "hephaestus.automation.claude_invoke._run_tracked",
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
                fallback_model_value=FALLBACK_MODEL,
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
            "hephaestus.automation.claude_invoke._run_tracked",
            side_effect=[_cap_error(), ok],
        ):
            _, sid = invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                fallback_model_value=FALLBACK_MODEL,
                cwd=cwd,
            )
        assert sid == session_uuid("R", 1, AGENT_PLANNER, FALLBACK_MODEL, cwd=cwd)

    def test_explicit_fallback_model_selection(self, fake_home: Path) -> None:
        """The explicit fallback string reaches the retry command unchanged."""
        cwd = fake_home / "work"
        cwd.mkdir()
        ok = MagicMock(stdout="ok", stderr="", returncode=0)
        with patch(
            "hephaestus.automation.claude_invoke._run_tracked",
            side_effect=[_cap_error(), ok],
        ) as tracked:
            invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="claude-fable-5",
                fallback_model_value="MyFallbackModel:future-effort",
                cwd=cwd,
            )
        second_argv = _argv(tracked.call_args_list[1])
        assert second_argv[second_argv.index("--model") + 1] == "MyFallbackModel"

    def test_cap_without_explicit_fallback_propagates(self, fake_home: Path) -> None:
        """A cap does not select a fallback model when none is supplied."""
        cwd = fake_home / "work"
        cwd.mkdir()
        with (
            patch(
                "hephaestus.automation.claude_invoke._run_tracked", side_effect=_cap_error()
            ) as tracked,
            pytest.raises(subprocess.CalledProcessError),
        ):
            invoke_claude_with_session(
                repo="R",
                issue=1,
                agent=AGENT_PLANNER,
                prompt="hi",
                model="MyModel",
                cwd=cwd,
            )
        assert tracked.call_count == 1
        assert not is_model_capped("MyModel")

    @pytest.mark.parametrize("lifecycle", ["start-new", "resume-required"])
    @pytest.mark.parametrize("error_envelope", [False, True])
    def test_durable_session_does_not_change_model_on_cap(
        self,
        fake_home: Path,
        lifecycle: str,
        error_envelope: bool,
    ) -> None:
        """A cap preserves the durable session model and session ID."""
        cwd = fake_home / "work"
        cwd.mkdir()
        model = "MyModel"
        sid = session_uuid("R", 1, AGENT_PLANNER, model, cwd=cwd)
        if lifecycle == "resume-required":
            _make_existing_jsonl(fake_home, cwd, sid)
        with patch("hephaestus.automation.claude_invoke._run_tracked") as tracked:
            if error_envelope:
                tracked.return_value = MagicMock(stdout=_cap_envelope(), stderr="", returncode=0)
                out, returned_sid = invoke_claude_with_session(
                    repo="R",
                    issue=1,
                    agent=AGENT_PLANNER,
                    prompt="hi",
                    model=model,
                    fallback_model_value=FALLBACK_MODEL,
                    cwd=cwd,
                    session_lifecycle=lifecycle,
                    output_format="json",
                )
                assert out == _cap_envelope()
                assert returned_sid == sid
            else:
                tracked.side_effect = _cap_error()
                with pytest.raises(subprocess.CalledProcessError):
                    invoke_claude_with_session(
                        repo="R",
                        issue=1,
                        agent=AGENT_PLANNER,
                        prompt="hi",
                        model=model,
                        fallback_model_value=FALLBACK_MODEL,
                        cwd=cwd,
                        session_lifecycle=lifecycle,
                    )
        assert tracked.call_count == 1
        argv = _argv(tracked.call_args)
        assert argv[argv.index("--model") + 1] == model
        assert sid in argv
        assert not is_model_capped(model)


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
        assert kwargs["stdin_text"] == "plan thisissue"
        assert "\x00" not in kwargs["stdin_text"]

    def test_real_subprocess_does_not_raise_with_null_byte(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end regression: the real subprocess.run path tolerates a NUL.

        Reproduces the #1509 crash. We point the invoked binary at a portable
        no-op (``sys.executable -c ""``, always present — unlike ``true``) so the
        call succeeds; WITHOUT the fix, argv marshaling raises
        ``ValueError: embedded null byte`` here and never reaches the child.

        The real :func:`~hephaestus.automation.claude_invoke._run_tracked` still
        runs (Popen + process-group tracking) — only the binary is swapped — so
        the actual argv/stdin marshaling and the #2059 spawn path are exercised.
        """
        cwd = fake_home / "work"
        cwd.mkdir()

        real_run_tracked = claude_invoke._run_tracked
        noop = [sys.executable, "-c", ""]

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            # Swap the "claude" binary for a guaranteed no-op while preserving the
            # rest of argv verbatim, then delegate to the REAL _run_tracked so the
            # argv/stdin marshaling (which raised the original ValueError) and the
            # Popen/process-group spawn are still exercised end-to-end.
            return real_run_tracked([*noop, *cmd[1:]], **kwargs)

        monkeypatch.setattr("hephaestus.automation.claude_invoke._run_tracked", fake_run)

        out, _sid = invoke_claude_with_session(
            repo="R",
            issue=1509,
            agent=AGENT_PLANNER,
            prompt="plan this\x00issue",
            model="sonnet",
            cwd=cwd,
        )
        assert out == ""
