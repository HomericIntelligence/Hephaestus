"""Isolated unit tests for the #712 phase decomposition.

Each phase is exercised against a lightweight :class:`StageContext` built from
a ``SimpleNamespace`` stub — no 30-collaborator mock setup required (issue #712
acceptance criterion). These tests pin the phase API surface and the
cross-phase dispatch contract that the pipeline stages rely on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest

from hephaestus.automation._implement_phase import ImplementPhase, _prepend_advise
from hephaestus.automation._plan_phase import PlanPhase, _phase_env
from hephaestus.automation._pr_create_phase import PRCreatePhase
from hephaestus.automation._stage_context import StageContext, StageMixin
from hephaestus.automation.review_journal import PlanDiscoveryStatus, render_current_plan


def _make_ctx(tmp_path: Path, **option_overrides: Any) -> StageContext:
    """Build a StageContext over a stub impl + runner with no live collaborators."""
    option_values: dict[str, Any] = {
        "agent": "claude",
        "dry_run": False,
        "auto_merge": True,
        "enable_advise": True,
        "enable_learn": True,
        "enable_follow_up": True,
        "run_pre_pr_tests": False,
        "include_nitpicks": False,
    }
    option_values.update(option_overrides)
    options = SimpleNamespace(**option_values)
    impl = cast(
        Any,
        SimpleNamespace(
            options=options,
            state_dir=tmp_path,
            repo_root=tmp_path,
            status_tracker=SimpleNamespace(update_slot=lambda *a, **k: None),
            worktree_manager=SimpleNamespace(),
            state_mgr=SimpleNamespace(lock=mock.MagicMock(), states={}),
            _log=lambda *a, **k: None,
            _save_state=lambda *a, **k: None,
        ),
    )
    runner = cast(Any, SimpleNamespace())
    ctx = StageContext(impl=impl, runner=runner)
    return ctx


def test_stage_context_accessors_delegate_to_impl(tmp_path: Path) -> None:
    """StageContext re-exposes the impl's shared references."""
    ctx = _make_ctx(tmp_path)
    assert ctx.options.agent == "claude"
    assert ctx.state_dir == tmp_path
    assert ctx.repo_root == tmp_path
    assert ctx.state_lock is ctx.impl.state_mgr.lock


def test_stage_mixin_exposes_runner_and_impl(tmp_path: Path) -> None:
    """A phase reads impl/runner/options through the mixin accessors."""
    ctx = _make_ctx(tmp_path)
    phase = PlanPhase(ctx)
    assert isinstance(phase, StageMixin)
    assert phase.impl is ctx.impl
    assert phase.runner is ctx.runner
    assert phase.options is ctx.options
    assert phase.state_dir == tmp_path


# ---------------------------------------------------------------------------
# PlanPhase
# ---------------------------------------------------------------------------


def test_plan_phase_discover_plan_found_on_plan_comment(tmp_path: Path) -> None:
    """_discover_plan returns FOUND when an actor-owned plan is present."""
    phase = PlanPhase(_make_ctx(tmp_path))
    with (
        mock.patch(
            "hephaestus.automation._plan_phase.fetch_issue_comments_metadata",
            return_value=[{"body": render_current_plan("step 1"), "user": {"login": "bot"}}],
        ),
        mock.patch("hephaestus.automation._plan_phase.gh_current_login", return_value="bot"),
    ):
        result = phase._discover_plan(7)

    assert result.status is PlanDiscoveryStatus.FOUND


def test_plan_phase_read_failure_is_explicit(tmp_path: Path) -> None:
    """_discover_plan reports API failures instead of inventing absence."""
    phase = PlanPhase(_make_ctx(tmp_path))
    with mock.patch(
        "hephaestus.automation._plan_phase.fetch_issue_comments_metadata",
        side_effect=OSError("boom"),
    ):
        result = phase._discover_plan(7)

    assert result.status is PlanDiscoveryStatus.READ_ERROR


def test_phase_env_keeps_only_repo_root_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child phase env drops inherited site-packages contamination."""
    monkeypatch.setenv("PYTHONPATH", f"/opt/site-packages{os.pathsep}/tmp/elsewhere")

    env = _phase_env(tmp_path)

    assert env["PYTHONPATH"] == str(tmp_path)


def test_plan_phase_generate_uses_entry_point(tmp_path: Path) -> None:
    """_generate runs the planner through the active interpreter, not PATH."""
    phase = PlanPhase(_make_ctx(tmp_path))
    with mock.patch("hephaestus.automation._plan_phase.run") as mock_run:
        phase._generate(7)
    args = mock_run.call_args[0][0]
    assert args[:3] == [sys.executable, "-m", "hephaestus.automation.planner"]
    assert "--issues" in args and "7" in args


def test_plan_phase_generate_sanitizes_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_generate passes a repo-root-only PYTHONPATH to child subprocesses."""
    monkeypatch.setenv("PYTHONPATH", f"/opt/site-packages{os.pathsep}/tmp/elsewhere")
    phase = PlanPhase(_make_ctx(tmp_path))
    with mock.patch("hephaestus.automation._plan_phase.run") as mock_run:
        phase._generate(7)
    assert mock_run.call_args.kwargs["env"]["PYTHONPATH"] == str(tmp_path)


def test_plan_phase_generate_uses_long_stage_timeout(tmp_path: Path) -> None:
    """_generate bounds the subprocess by the long stage timeout (#1374).

    output.log L834 showed ``Command timed out after 600s:
    hephaestus-plan-issues --issues 1357`` — the heavy issue exhausted a
    hard-coded 600s wrapper while the planner's stage budget is 7200s. The call
    must now route through the distinct stage-level helper.
    """
    phase = PlanPhase(_make_ctx(tmp_path))
    with (
        mock.patch("shutil.which", return_value="/usr/bin/hpi"),
        mock.patch("hephaestus.automation._plan_phase.run") as mock_run,
        mock.patch(
            "hephaestus.automation._plan_phase.plan_stage_timeout",
            return_value=7200,
        ),
    ):
        phase._generate(1357)
    assert mock_run.call_args.kwargs["timeout"] == 7200


def test_plan_phase_generate_timeout_respects_explicit_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The typed plan-stage timeout flows through while removed env is inert."""
    monkeypatch.setenv("HEPH_PLAN_STAGE_TIMEOUT", "9000")
    monkeypatch.setenv("HEPH_AGENT_PLAN_TIMEOUT", "300")
    phase = PlanPhase(_make_ctx(tmp_path, plan_stage_timeout=9000))
    with (
        mock.patch("shutil.which", return_value="/usr/bin/hpi"),
        mock.patch("hephaestus.automation._plan_phase.run") as mock_run,
    ):
        phase._generate(1357)
    assert mock_run.call_args.kwargs["timeout"] == 9000


def test_plan_phase_generate_ignores_inner_agent_plan_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEPH_AGENT_PLAN_TIMEOUT must not shorten the outer plan-stage wrapper."""
    monkeypatch.delenv("HEPH_PLAN_STAGE_TIMEOUT", raising=False)
    monkeypatch.delenv("HEPH_PLANNER_AGENT_TIMEOUT", raising=False)
    monkeypatch.setenv("HEPH_AGENT_PLAN_TIMEOUT", "333")
    phase = PlanPhase(_make_ctx(tmp_path))
    with (
        mock.patch("shutil.which", return_value="/usr/bin/hpi"),
        mock.patch("hephaestus.automation._plan_phase.run") as mock_run,
    ):
        phase._generate(1357)
    assert mock_run.call_args.kwargs["timeout"] == 7200


# ---------------------------------------------------------------------------
# ImplementPhase
# ---------------------------------------------------------------------------


def test_prepend_advise_injects_block() -> None:
    """_prepend_advise prepends a learnings block for real findings."""
    out = _prepend_advise("use the cached resolver", "DO THE WORK")
    assert "Prior Learnings" in out and out.endswith("DO THE WORK")


def test_prepend_advise_skips_marker() -> None:
    """_prepend_advise returns the prompt unchanged for a skipped-marker."""
    assert _prepend_advise("<!-- advise step skipped: x -->", "P") == "P"
    assert _prepend_advise("   ", "P") == "P"


def test_implement_phase_run_claude_code_dry_run(tmp_path: Path) -> None:
    """_run_claude_code is a no-op returning None under dry-run."""
    phase = ImplementPhase(_make_ctx(tmp_path, dry_run=True))
    assert phase._run_claude_code(7, tmp_path, "prompt") is None


def test_implement_phase_run_claude_code_dispatches_claude(tmp_path: Path) -> None:
    """_run_claude_code routes to the Claude session for non-direct agents."""
    ctx = _make_ctx(tmp_path)
    ctx.impl._run_claude_impl_session = mock.MagicMock(return_value="sess-1")
    phase = ImplementPhase(ctx)
    assert phase._run_claude_code(7, tmp_path, "prompt") == "sess-1"
    ctx.impl._run_claude_impl_session.assert_called_once()


def test_implement_phase_run_advise_uses_direct_implementer(tmp_path: Path) -> None:
    """The advise pass uses the selected direct implementer and read-only sandbox."""
    phase = ImplementPhase(
        _make_ctx(
            tmp_path,
            implementer_agent="codex",
            implementer_model="model-a",
            advise_timeout=17,
            git_timeout=3,
            clone_timeout=5,
        )
    )

    def invoke_advise(**kwargs: Any) -> str:
        return cast(str, kwargs["invoke"]("advice prompt"))

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.run_advise",
            side_effect=invoke_advise,
        ) as run_advise,
        mock.patch(
            "hephaestus.automation._implement_phase.uses_direct_agent_runner",
            return_value=True,
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.direct_agent_model",
            return_value="resolved-model",
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.run_agent_text",
            return_value=SimpleNamespace(stdout="  use a seam  "),
        ) as run_agent,
    ):
        result = phase._run_advise(7, "Title", "Body")

    assert result == "use a seam"
    assert run_agent.call_args.kwargs == {
        "agent": "codex",
        "prompt": "advice prompt",
        "cwd": tmp_path,
        "timeout": 17,
        "execution_request": mock.ANY,
        "model": "resolved-model",
        "sandbox": "read-only",
    }
    assert run_advise.call_args.kwargs["git_timeout_s"] == 3
    assert run_advise.call_args.kwargs["clone_timeout_s"] == 5


def test_implement_phase_run_advise_uses_claude_session(tmp_path: Path) -> None:
    """The Claude advise pass binds the repository and selected model."""
    phase = ImplementPhase(
        _make_ctx(tmp_path, model="fallback", advise_timeout=19, git_timeout=None)
    )

    def invoke_advise(**kwargs: Any) -> str:
        return cast(str, kwargs["invoke"]("advice prompt"))

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.run_advise",
            side_effect=invoke_advise,
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.uses_direct_agent_runner",
            return_value=False,
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.get_repo_slug",
            return_value="org/repo",
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.invoke_claude_with_session",
            return_value=("  finding  ", None),
        ) as invoke,
    ):
        result = phase._run_advise(8, "Title", "Body")

    assert result == "finding"
    assert invoke.call_args.kwargs == {
        "repo": "org/repo",
        "issue": 8,
        "agent": "advise",
        "prompt": "advice prompt",
        "model": "fallback",
        "cwd": tmp_path,
        "timeout": 19,
        "output_format": "text",
    }


def test_implement_phase_advise_wrapper_and_compaction(tmp_path: Path) -> None:
    """Compatibility helpers delegate advise and compaction with stable arguments."""
    phase = ImplementPhase(_make_ctx(tmp_path, model="fallback"))
    phase._run_advise = mock.MagicMock(return_value="finding")  # type: ignore[method-assign]

    assert phase._run_advise_as_implementer_turn(9, "Title", "Body", tmp_path / "wt") == "finding"

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.get_repo_slug",
            return_value="org/repo",
        ),
        mock.patch("hephaestus.automation._implement_phase.compact_session") as compact,
    ):
        phase._compact_implementer_session(9, tmp_path / "wt")

    phase._run_advise.assert_called_once_with(9, "Title", "Body")
    compact.assert_called_once_with(
        repo="org/repo",
        issue=9,
        agent="implementer",
        cwd=tmp_path / "wt",
        model="fallback",
    )


def test_implement_phase_run_claude_code_dispatches_direct_agent(tmp_path: Path) -> None:
    """A direct provider uses the direct session path after state setup."""
    state_dir = tmp_path / "state"
    ctx = _make_ctx(tmp_path, implementer_agent="codex")
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)

    with mock.patch(
        "hephaestus.automation._implement_phase.uses_direct_agent_runner",
        return_value=True,
    ):
        phase._run_direct_agent_code = mock.MagicMock(return_value="session")  # type: ignore[method-assign]
        result = phase._run_claude_code(7, tmp_path, "prompt")

    assert result == "session"
    assert state_dir.is_dir()
    phase._run_direct_agent_code.assert_called_once_with(7, tmp_path, "prompt")


def test_claude_impl_session_writes_log_and_removes_prompt(tmp_path: Path) -> None:
    """A successful Claude result persists its receipt and removes the prompt file."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = _make_ctx(tmp_path, agent_timeout=23, implementer_model="model-b")
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)
    payload = json.dumps({"session_id": "session-7"})

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.get_repo_slug",
            return_value="org/repo",
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.invoke_claude_with_session",
            return_value=(payload, None),
        ) as invoke,
    ):
        assert phase._run_claude_impl_session(7, tmp_path, "prompt") == "session-7"

    assert invoke.call_args.kwargs["permission_mode"] == "dontAsk"
    assert invoke.call_args.kwargs["allowed_tools"] == "Read,Write,Edit,Glob,Grep,Bash"
    assert not (tmp_path / ".claude-prompt-7.md").exists()
    assert (state_dir / "claude-7.log").read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("payload", ["not-json", "[]"])
def test_claude_impl_session_keeps_unparseable_output(tmp_path: Path, payload: str) -> None:
    """Malformed or non-object output returns no session and remains available for diagnosis."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = _make_ctx(tmp_path, agent_timeout=23, model="")
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.get_repo_slug",
            return_value="org/repo",
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.invoke_claude_with_session",
            return_value=(payload, None),
        ),
    ):
        assert phase._run_claude_impl_session(7, tmp_path, "prompt") is None

    assert (state_dir / "claude-7.log").read_text(encoding="utf-8") == payload
    assert not (tmp_path / ".claude-prompt-7.md").exists()


@pytest.mark.parametrize("reset_epoch", [None, 123])
def test_claude_impl_session_rejects_error_payload(tmp_path: Path, reset_epoch: int | None) -> None:
    """An error-shaped zero exit is a failure and waits only for a valid reset."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = _make_ctx(tmp_path, agent_timeout=23)
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)
    payload = json.dumps({"is_error": True, "result": "limit reached"})

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.get_repo_slug",
            return_value="org/repo",
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.invoke_claude_with_session",
            return_value=(payload, None),
        ),
        mock.patch(
            "hephaestus.automation._implement_phase._claude_quota_reset_epoch",
            return_value=reset_epoch,
        ),
        mock.patch("hephaestus.automation._implement_phase.wait_until") as wait,
    ):
        with pytest.raises(RuntimeError, match="limit reached"):
            phase._run_claude_impl_session(7, tmp_path, "prompt")

    if reset_epoch is None:
        wait.assert_not_called()
    else:
        wait.assert_called_once_with(reset_epoch)
    assert (state_dir / "claude-7.log").read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("reset_epoch", [None, 456])
def test_claude_impl_session_translates_process_failure(
    tmp_path: Path, reset_epoch: int | None
) -> None:
    """A Claude process failure is logged, optionally delayed, and translated."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = _make_ctx(tmp_path, agent_timeout=23)
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)
    failure = subprocess.CalledProcessError(9, ["claude"], output="out", stderr="err")

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.get_repo_slug",
            return_value="org/repo",
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.invoke_claude_with_session",
            side_effect=failure,
        ),
        mock.patch(
            "hephaestus.automation._implement_phase._claude_quota_reset_epoch",
            return_value=reset_epoch,
        ),
        mock.patch("hephaestus.automation._implement_phase.wait_until") as wait,
    ):
        with pytest.raises(RuntimeError, match="Claude Code failed: err"):
            phase._run_claude_impl_session(7, tmp_path, "prompt")

    if reset_epoch is None:
        wait.assert_not_called()
    else:
        wait.assert_called_once_with(reset_epoch)
    log = (state_dir / "claude-7.log").read_text(encoding="utf-8")
    assert "EXIT CODE: 9" in log and "STDOUT:\nout" in log and "STDERR:\nerr" in log


def test_claude_impl_session_translates_timeout(tmp_path: Path) -> None:
    """A Claude timeout persists partial output and keeps its causal exception."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = _make_ctx(tmp_path, agent_timeout=23)
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)
    timeout = subprocess.TimeoutExpired(["claude"], 23, output="partial")

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.get_repo_slug",
            return_value="org/repo",
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.invoke_claude_with_session",
            side_effect=timeout,
        ),
    ):
        with pytest.raises(RuntimeError, match="Claude Code timed out") as raised:
            phase._run_claude_impl_session(7, tmp_path, "prompt")

    assert raised.value.__cause__ is timeout
    assert "TIMEOUT after 23s" in (state_dir / "claude-7.log").read_text()
    assert not (tmp_path / ".claude-prompt-7.md").exists()


def test_direct_agent_session_persists_output_and_session(tmp_path: Path) -> None:
    """A direct implementer result persists output and returns the session identity."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = _make_ctx(
        tmp_path,
        implementer_agent="codex",
        implementer_model="model-c",
        agent_timeout=29,
    )
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.direct_agent_model",
            return_value="resolved-model",
        ),
        mock.patch(
            "hephaestus.automation._implement_phase.run_agent_session",
            return_value=SimpleNamespace(stdout="receipt", session_id="session-9"),
        ) as run_agent,
    ):
        assert phase._run_codex_code(9, tmp_path, "prompt") == "session-9"

    assert run_agent.call_args.kwargs["agent"] == "codex"
    assert run_agent.call_args.kwargs["model"] == "resolved-model"
    assert run_agent.call_args.kwargs["sandbox"] == "workspace-write"
    assert (state_dir / "codex-9.log").read_text(encoding="utf-8") == "receipt"


@pytest.mark.parametrize("reset_epoch", [None, 789])
def test_direct_agent_session_translates_process_failure(
    tmp_path: Path, reset_epoch: int | None
) -> None:
    """A direct provider failure is logged and waits only when a reset exists."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = _make_ctx(tmp_path, agent="codex", agent_timeout=29)
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)
    failure = subprocess.CalledProcessError(4, ["codex"], output="out", stderr="err")

    with (
        mock.patch(
            "hephaestus.automation._implement_phase.run_agent_session",
            side_effect=failure,
        ),
        mock.patch(
            "hephaestus.automation._implement_phase._claude_quota_reset_epoch",
            return_value=reset_epoch,
        ),
        mock.patch("hephaestus.automation._implement_phase.wait_until") as wait,
    ):
        with pytest.raises(RuntimeError, match="codex failed: err") as raised:
            phase._run_direct_agent_code(9, tmp_path, "prompt")

    assert raised.value.__cause__ is failure
    if reset_epoch is None:
        wait.assert_not_called()
    else:
        wait.assert_called_once_with(reset_epoch)
    assert "EXIT CODE: 4" in (state_dir / "codex-9.log").read_text()


def test_direct_agent_session_translates_timeout(tmp_path: Path) -> None:
    """A direct provider timeout persists partial output and preserves its cause."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = _make_ctx(tmp_path, agent="codex", agent_timeout=29)
    ctx.impl.state_dir = state_dir
    phase = ImplementPhase(ctx)
    timeout = subprocess.TimeoutExpired(["codex"], 29, output="partial")

    with mock.patch(
        "hephaestus.automation._implement_phase.run_agent_session",
        side_effect=timeout,
    ):
        with pytest.raises(RuntimeError, match="codex timed out") as raised:
            phase._run_direct_agent_code(9, tmp_path, "prompt")

    assert raised.value.__cause__ is timeout
    assert "TIMEOUT after 29s" in (state_dir / "codex-9.log").read_text()


# ---------------------------------------------------------------------------
# PRCreatePhase
# ---------------------------------------------------------------------------


def test_pr_create_finalize_persists_pr_number(tmp_path: Path) -> None:
    """_finalize_pr ensures the PR exists and persists its number on state."""
    ctx = _make_ctx(tmp_path)
    ctx.impl._ensure_pr_created = mock.MagicMock(return_value=321)
    ctx.impl._commit_changes = mock.MagicMock()
    ctx.impl._run_tests_in_worktree = mock.MagicMock(return_value=True)
    phase = PRCreatePhase(ctx)
    state = SimpleNamespace(phase=None, pr_number=None)
    with mock.patch(
        "hephaestus.automation._pr_create_phase._has_uncommitted_changes",
        return_value=False,
    ):
        pr = phase._finalize_pr(7, "7-auto-impl", tmp_path, cast(Any, state), slot_id=None)
    assert pr == 321
    assert state.pr_number == 321
    ctx.impl._commit_changes.assert_not_called()
    # Pre-PR tests are off by default, so the gate must not have run.
    ctx.impl._run_tests_in_worktree.assert_not_called()


def test_pr_create_finalize_commits_dirty_worktree_before_pr(tmp_path: Path) -> None:
    """_finalize_pr commits agent edits before push/PR creation."""
    ctx = _make_ctx(tmp_path)
    ctx.impl._commit_changes = mock.MagicMock()
    ctx.impl._ensure_pr_created = mock.MagicMock(return_value=321)
    ctx.impl._run_tests_in_worktree = mock.MagicMock(return_value=True)
    parent = mock.MagicMock()
    parent.attach_mock(ctx.impl._commit_changes, "commit")
    parent.attach_mock(ctx.impl._ensure_pr_created, "ensure")
    phase = PRCreatePhase(ctx)
    state = SimpleNamespace(phase=None, pr_number=None)

    with mock.patch(
        "hephaestus.automation._pr_create_phase._has_uncommitted_changes",
        return_value=True,
    ):
        pr = phase._finalize_pr(7, "7-auto-impl", tmp_path, cast(Any, state), slot_id=None)

    assert pr == 321
    parent.assert_has_calls(
        [
            mock.call.commit(7, tmp_path),
            mock.call.ensure(7, "7-auto-impl", tmp_path, None),
        ]
    )


def test_pr_create_finalize_runs_pre_pr_tests_when_enabled(tmp_path: Path) -> None:
    """_finalize_pr runs the opt-in pre-PR test gate before creating the PR."""
    ctx = _make_ctx(tmp_path, run_pre_pr_tests=True)
    ctx.impl._ensure_pr_created = mock.MagicMock(return_value=9)
    ctx.impl._commit_changes = mock.MagicMock()
    ctx.impl._run_tests_in_worktree = mock.MagicMock(return_value=False)
    phase = PRCreatePhase(ctx)
    state = SimpleNamespace(phase=None, pr_number=None)
    with mock.patch(
        "hephaestus.automation._pr_create_phase._has_uncommitted_changes",
        return_value=False,
    ):
        phase._finalize_pr(7, "b", tmp_path, cast(Any, state), slot_id=None)
    ctx.impl._run_tests_in_worktree.assert_called_once()


def test_pr_create_run_tests_uses_explicit_timeout_and_ignores_legacy_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-PR subprocess uses typed configuration, not the removed env knob."""
    monkeypatch.setenv("HEPH_PRE_PR_TEST_TIMEOUT", "999")
    phase = PRCreatePhase(_make_ctx(tmp_path, pre_pr_test_timeout=777))
    with mock.patch("hephaestus.automation._pr_create_phase.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        assert phase._run_tests_in_worktree(tmp_path, 7) is True

    assert mock_run.call_args.args[0] == [
        "uv",
        "run",
        "pytest",
        "tests",
        "-q",
        "--tb=short",
    ]
    assert mock_run.call_args.kwargs["timeout"] == 777
