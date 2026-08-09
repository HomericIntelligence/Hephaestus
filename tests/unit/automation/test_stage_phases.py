"""Isolated unit tests for the #712 phase decomposition.

Each phase is exercised against a lightweight :class:`StageContext` built from
a ``SimpleNamespace`` stub — no 30-collaborator mock setup required (issue #712
acceptance criterion). These tests pin the phase API surface and the
cross-phase dispatch contract that the pipeline stages rely on.
"""

from __future__ import annotations

import json
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
from hephaestus.automation.models import ImplementationPhase


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


def test_plan_phase_has_plan_true_on_plan_comment(tmp_path: Path) -> None:
    """_has_plan returns True when a plan comment is present."""
    phase = PlanPhase(_make_ctx(tmp_path))
    fake = SimpleNamespace(
        stdout=json.dumps({"comments": [{"body": "# Implementation Plan\n\nstep 1"}]})
    )
    with (
        mock.patch("hephaestus.automation._plan_phase.gh_call", return_value=fake),
        mock.patch(
            "hephaestus.automation._plan_phase._comments_contain_plan", return_value=True
        ) as mock_check,
    ):
        assert phase._has_plan(7) is True
    mock_check.assert_called_once()


def test_plan_phase_has_plan_false_on_subprocess_error(tmp_path: Path) -> None:
    """_has_plan swallows subprocess/JSON errors and returns False."""
    phase = PlanPhase(_make_ctx(tmp_path))
    with mock.patch("hephaestus.automation._plan_phase.gh_call", side_effect=OSError("boom")):
        assert phase._has_plan(7) is False


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
    assert phase._can_resume_state_session(cast(Any, state)) is True


def test_followup_cannot_resume_missing_provider_metadata(tmp_path: Path) -> None:
    """A session without explicit provider metadata cannot be resumed."""
    phase = FollowUpPhase(_make_ctx(tmp_path))
    state = SimpleNamespace(session_id="s", session_agent=None, issue_number=7)

    assert phase._can_resume_state_session(cast(Any, state)) is False


def _followup_state(**overrides: Any) -> SimpleNamespace:
    """Return mutable persisted-state data used by follow-up tests."""
    values = {
        "issue_number": 7,
        "session_id": "session-7",
        "session_agent": "claude",
        "learn_completed": False,
        "phase": ImplementationPhase.CREATING_PR,
        "completed_at": None,
        "worktree_path": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_followup_runs_learn_compaction_and_issue_discovery(tmp_path: Path) -> None:
    """A resumable Claude session completes every enabled post-PR action."""
    ctx = _make_ctx(tmp_path)
    ctx.impl._save_state = mock.MagicMock()
    ctx.runner._can_resume_state_session = mock.MagicMock(return_value=True)
    ctx.runner._run_learn = mock.MagicMock(return_value=True)
    ctx.runner._compact_implementer_session = mock.MagicMock()
    ctx.runner._run_follow_up_issues = mock.MagicMock()
    state = _followup_state()

    FollowUpPhase(ctx)._run_post_pr_followup(7, tmp_path, cast(Any, state), slot_id=2)

    ctx.runner._run_learn.assert_called_once_with(
        "session-7", tmp_path, 7, 2, session_agent="claude"
    )
    ctx.runner._compact_implementer_session.assert_called_once_with(7, tmp_path)
    ctx.runner._run_follow_up_issues.assert_called_once_with(
        "session-7", tmp_path, 7, 2, session_agent="claude"
    )
    assert state.learn_completed is True
    assert state.phase is ImplementationPhase.COMPLETED
    assert state.completed_at is not None


def test_followup_direct_agent_skips_claude_compaction(tmp_path: Path) -> None:
    """Direct-agent sessions learn successfully without Claude compaction."""
    ctx = _make_ctx(tmp_path, agent="codex", enable_follow_up=False)
    ctx.impl._save_state = mock.MagicMock()
    ctx.runner._can_resume_state_session = mock.MagicMock(return_value=True)
    ctx.runner._run_learn = mock.MagicMock(return_value=True)
    ctx.runner._compact_implementer_session = mock.MagicMock()
    state = _followup_state(session_agent="codex")

    FollowUpPhase(ctx)._run_post_pr_followup(7, tmp_path, cast(Any, state), slot_id=None)

    ctx.runner._compact_implementer_session.assert_not_called()
    assert state.phase is ImplementationPhase.COMPLETED


def test_followup_completes_when_session_cannot_resume(tmp_path: Path) -> None:
    """Missing resume authority skips optional agent calls but persists completion."""
    ctx = _make_ctx(tmp_path)
    ctx.impl._save_state = mock.MagicMock()
    ctx.runner._can_resume_state_session = mock.MagicMock(return_value=False)
    ctx.runner._run_learn = mock.MagicMock()
    ctx.runner._run_follow_up_issues = mock.MagicMock()
    state = _followup_state()

    FollowUpPhase(ctx)._run_post_pr_followup(7, tmp_path, cast(Any, state), slot_id=None)

    ctx.runner._run_learn.assert_not_called()
    ctx.runner._run_follow_up_issues.assert_not_called()
    assert state.phase is ImplementationPhase.COMPLETED


def test_followup_helpers_delegate_configuration(tmp_path: Path) -> None:
    """Follow-up wrappers forward provider, timeout, state, and dry-run settings."""
    ctx = _make_ctx(tmp_path, dry_run=True)
    ctx.options.follow_up_timeout = 41
    ctx.options.learn_timeout = 43
    phase = FollowUpPhase(ctx)
    with (
        mock.patch(
            "hephaestus.automation._followup_phase.parse_follow_up_items", return_value=[{"x": 1}]
        ) as parse,
        mock.patch("hephaestus.automation._followup_phase.run_follow_up_issues") as follow,
        mock.patch(
            "hephaestus.automation._followup_phase.learn_needs_rerun", return_value=True
        ) as needs,
        mock.patch("hephaestus.automation._followup_phase.run_learn", return_value=True) as learn,
    ):
        assert phase._parse_follow_up_items("payload") == [{"x": 1}]
        phase._run_follow_up_issues("s", tmp_path, 7, 2, session_agent="claude")
        assert phase._learn_needs_rerun(7) is True
        assert phase._run_learn("s", tmp_path, 7, 2, session_agent="claude") is True

    parse.assert_called_once_with("payload")
    assert follow.call_args.kwargs["dry_run"] is True
    assert follow.call_args.kwargs["timeout"] == 41
    needs.assert_called_once_with(7, tmp_path)
    assert learn.call_args.kwargs["timeout"] == 43


def test_followup_reruns_only_eligible_failed_learns(tmp_path: Path) -> None:
    """The recovery sweep ignores ineligible states and persists attempted learns."""
    existing = tmp_path / "worktree"
    existing.mkdir()
    eligible = _followup_state(
        phase=ImplementationPhase.COMPLETED,
        worktree_path=str(existing),
    )
    ctx = _make_ctx(tmp_path)
    ctx.impl.state_mgr.states = {
        7: eligible,
        8: _followup_state(issue_number=8, phase=ImplementationPhase.IMPLEMENTING),
        9: _followup_state(
            issue_number=9, phase=ImplementationPhase.COMPLETED, learn_completed=True
        ),
        10: _followup_state(issue_number=10, phase=ImplementationPhase.COMPLETED),
        11: _followup_state(
            issue_number=11,
            phase=ImplementationPhase.COMPLETED,
            worktree_path=str(tmp_path / "missing"),
        ),
    }
    ctx.impl._learn_needs_rerun = mock.MagicMock(return_value=True)
    ctx.impl._run_learn = mock.MagicMock(return_value=False)
    ctx.impl._save_state = mock.MagicMock()
    phase = FollowUpPhase(ctx)

    results = phase._rerun_failed_learns()

    assert results == {7: False}
    assert eligible.learn_completed is False
    ctx.impl._run_learn.assert_called_once_with(
        "session-7", existing, 7, slot_id=None, session_agent="claude"
    )
    ctx.impl._save_state.assert_called_once_with(eligible)
