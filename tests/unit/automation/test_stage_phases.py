"""Isolated unit tests for the #712 phase decomposition.

Each phase is exercised against a lightweight :class:`StageContext` built from
a ``SimpleNamespace`` stub — no 30-collaborator mock setup required (issue #712
acceptance criterion). These tests pin the phase API surface and the
cross-phase dispatch contract that the pipeline stages rely on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest

from hephaestus.automation._followup_phase import FollowUpPhase
from hephaestus.automation._implement_phase import ImplementPhase, _prepend_advise
from hephaestus.automation._plan_phase import PlanPhase, _phase_env
from hephaestus.automation._pr_create_phase import PRCreatePhase
from hephaestus.automation._stage_context import StageContext, StageMixin
from hephaestus.automation.review_journal import PlanDiscoveryStatus


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
            return_value=[
                {"body": "# Implementation Plan\n\nstep 1", "user": {"login": "bot"}}
            ],
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


def test_plan_phase_generate_timeout_respects_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HEPH_PLAN_STAGE_TIMEOUT override flows through to the subprocess."""
    monkeypatch.setenv("HEPH_PLAN_STAGE_TIMEOUT", "9000")
    monkeypatch.setenv("HEPH_AGENT_PLAN_TIMEOUT", "300")
    phase = PlanPhase(_make_ctx(tmp_path))
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


def test_pr_create_run_tests_uses_env_configured_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-PR test subprocess timeout is centralized and env-tunable."""
    monkeypatch.setenv("HEPH_PRE_PR_TEST_TIMEOUT", "777")
    phase = PRCreatePhase(_make_ctx(tmp_path))
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


# ---------------------------------------------------------------------------
# FollowUpPhase
# ---------------------------------------------------------------------------


def test_followup_can_resume_requires_session(tmp_path: Path) -> None:
    """_can_resume_state_session is False without a saved session id."""
    phase = FollowUpPhase(_make_ctx(tmp_path))
    state = SimpleNamespace(session_id=None, session_agent=None, issue_number=7)
    assert phase._can_resume_state_session(cast(Any, state)) is False


def test_followup_can_resume_matches_agent(tmp_path: Path) -> None:
    """_can_resume_state_session is True when the saved agent matches."""
    phase = FollowUpPhase(_make_ctx(tmp_path))
    state = SimpleNamespace(session_id="s", session_agent="claude", issue_number=7)
    with mock.patch(
        "hephaestus.automation._followup_phase.session_agent_matches", return_value=True
    ):
        assert phase._can_resume_state_session(cast(Any, state)) is True
