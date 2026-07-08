"""Isolated unit tests for the #712 phase decomposition.

Each phase is exercised against a lightweight :class:`StageContext` built from
a ``SimpleNamespace`` stub — no 30-collaborator mock setup required (issue #712
acceptance criterion). These tests pin the phase API surface and the
cross-phase dispatch contract that the pipeline stages rely on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest

from hephaestus.automation._followup_phase import FollowUpPhase
from hephaestus.automation._implement_phase import ImplementPhase, _prepend_advise
from hephaestus.automation._plan_phase import PlanPhase, _phase_env
from hephaestus.automation._pr_create_phase import PRCreatePhase
from hephaestus.automation._review_phase import ReviewPhase, _is_automation_owned_thread
from hephaestus.automation._stage_context import StageContext, StageMixin


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
    """_generate prefers the installed hephaestus-plan-issues entry point."""
    phase = PlanPhase(_make_ctx(tmp_path))
    with (
        mock.patch("shutil.which", return_value="/usr/bin/hpi"),
        mock.patch("hephaestus.automation._plan_phase.run") as mock_run,
    ):
        phase._generate(7)
    args = mock_run.call_args[0][0]
    assert args[0] == "/usr/bin/hpi"
    assert "--issues" in args and "7" in args


def test_plan_phase_generate_sanitizes_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_generate passes a repo-root-only PYTHONPATH to child subprocesses."""
    monkeypatch.setenv("PYTHONPATH", f"/opt/site-packages{os.pathsep}/tmp/elsewhere")
    phase = PlanPhase(_make_ctx(tmp_path))
    with (
        mock.patch("shutil.which", return_value="/usr/bin/hpi"),
        mock.patch("hephaestus.automation._plan_phase.run") as mock_run,
    ):
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


# ---------------------------------------------------------------------------
# ReviewPhase
# ---------------------------------------------------------------------------


def test_is_automation_owned_thread_recognizes_bot() -> None:
    """A github-actions[bot] thread is automation-owned."""
    thread = {"comments": [{"author": "github-actions[bot]"}]}
    assert _is_automation_owned_thread(thread, current_login=None) is True


def test_is_automation_owned_thread_human_not_owned() -> None:
    """A human-authored thread is not automation-owned."""
    thread = {"comments": [{"author": "mvillmow"}]}
    assert _is_automation_owned_thread(thread, current_login="hephaestus-bot") is False


def test_review_phase_apply_verdict_go_arms_auto_merge(tmp_path: Path) -> None:
    """GO labels the PR and arms auto-merge when auto_merge is enabled."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with (
        mock.patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mark_go,
        mock.patch(
            "hephaestus.automation._review_phase.enable_auto_merge_after_implementation_go"
        ) as arm,
    ):
        phase._apply_impl_review_verdict(
            issue_number=7, pr_number=12, last_verdict="GO", slot_id=None, thread_id=None
        )
    mark_go.assert_called_once_with(12)
    arm.assert_called_once_with(12)


def test_review_phase_apply_verdict_error_applies_no_label(tmp_path: Path) -> None:
    """An ERROR verdict applies neither GO nor NO-GO labels."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with (
        mock.patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mark_go,
        mock.patch(
            "hephaestus.automation._review_phase.mark_pr_implementation_no_go"
        ) as mark_no_go,
    ):
        phase._apply_impl_review_verdict(
            issue_number=7, pr_number=12, last_verdict="ERROR", slot_id=None, thread_id=None
        )
    mark_go.assert_not_called()
    mark_no_go.assert_not_called()


@pytest.mark.parametrize(
    "verdict, calls_go, calls_no_go",
    [
        ("NOGO", False, True),
        ("AMBIGUOUS", False, True),
        ("HUMAN_BLOCKED", False, False),
    ],
)
def test_review_phase_apply_verdict_mapping(
    tmp_path: Path, verdict: str, calls_go: bool, calls_no_go: bool
) -> None:
    """Verdict→label mapping is centralized and consistent."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with (
        mock.patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mark_go,
        mock.patch(
            "hephaestus.automation._review_phase.mark_pr_implementation_no_go"
        ) as mark_no_go,
        mock.patch("hephaestus.automation._review_phase.enable_auto_merge_after_implementation_go"),
    ):
        phase._apply_impl_review_verdict(
            issue_number=7, pr_number=12, last_verdict=verdict, slot_id=None, thread_id=None
        )
    assert mark_go.called is calls_go
    assert mark_no_go.called is calls_no_go


def test_review_phase_push_branch_delegates(tmp_path: Path) -> None:
    """_push_branch delegates to the canonical git helper."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with mock.patch("hephaestus.automation._review_phase.push_branch") as mock_push:
        phase._push_branch("b", tmp_path)

    mock_push.assert_called_once_with("b", tmp_path)


def test_review_phase_commit_if_changes_delegates_to_git_utils(tmp_path: Path) -> None:
    """_commit_if_changes delegates to the canonical git helper."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with mock.patch(
        "hephaestus.automation._review_phase.commit_if_changes",
        return_value=True,
    ) as mock_commit:
        assert phase._commit_if_changes(7, tmp_path) is True

    mock_commit.assert_called_once_with(
        7,
        tmp_path,
        phase.options.agent,
        committed_log_message="Committed in-loop address changes for issue #%s",
    )


def test_review_phase_commit_if_changes_clean_returns_false(tmp_path: Path) -> None:
    """_commit_if_changes returns False (no commit) when the worktree is clean."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with mock.patch(
        "hephaestus.automation._review_phase.commit_if_changes",
        return_value=False,
    ) as mock_commit:
        assert phase._commit_if_changes(7, tmp_path) is False

    mock_commit.assert_called_once_with(
        7,
        tmp_path,
        phase.options.agent,
        committed_log_message="Committed in-loop address changes for issue #%s",
    )


# ---------------------------------------------------------------------------
# #1328: pre-review conflict gate
# ---------------------------------------------------------------------------


def _gh_json(payload: dict[str, Any]) -> SimpleNamespace:
    """Stub a gh result carrying JSON stdout."""
    return SimpleNamespace(stdout=json.dumps(payload), stderr="")


def test_review_phase_merge_state_uses_owner_repo_slug(tmp_path: Path) -> None:
    """``gh pr view --repo`` requires OWNER/REPO, not the short repo slug."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with (
        mock.patch("hephaestus.automation._review_phase.get_repo_info", return_value=("o", "r")),
        mock.patch(
            "hephaestus.automation._review_phase.gh_call",
            return_value=_gh_json({"mergeStateStatus": "dirty", "mergeable": "conflicting"}),
        ) as gh_call,
    ):
        assert phase._pr_merge_state(12) == ("DIRTY", "CONFLICTING")
    args = gh_call.call_args.args[0]
    assert args[args.index("--repo") + 1] == "o/r"


def test_conflict_gate_clean_pr_proceeds_without_resolution(tmp_path: Path) -> None:
    """#1328: a non-conflicting PR returns True with no rebase/agent spend."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with (
        mock.patch.object(
            phase, "_pr_merge_state", return_value=("CLEAN", "MERGEABLE")
        ) as merge_state,
        mock.patch("hephaestus.automation._review_phase.rebase_worktree_onto") as rebase,
        mock.patch.object(phase, "_resume_impl_with_feedback") as resume,
    ):
        result = phase._resolve_conflict_before_review(
            issue_number=7,
            pr_number=12,
            worktree_path=tmp_path,
            branch_name="b",
            session_id="sess",
            slot_id=None,
            thread_id=None,
            state=None,
        )
    assert result is True
    merge_state.assert_called_once()
    rebase.assert_not_called()
    resume.assert_not_called()


def test_conflict_gate_mechanical_rebase_clears_conflict(tmp_path: Path) -> None:
    """#1328: a clean mechanical rebase clears the conflict without the agent."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with (
        mock.patch.object(
            phase,
            "_pr_merge_state",
            side_effect=[("DIRTY", "CONFLICTING"), ("CLEAN", "MERGEABLE")],
        ),
        mock.patch(
            "hephaestus.automation._review_phase.gh_call",
            return_value=_gh_json({"baseRefName": "main"}),
        ),
        mock.patch("hephaestus.automation._review_phase.sync_worktree_to_remote_branch"),
        mock.patch(
            "hephaestus.automation._review_phase.rebase_worktree_onto", return_value=True
        ) as rebase,
        mock.patch(
            "hephaestus.automation._review_phase.push_current_branch_with_lease_on_divergence"
        ) as push,
        mock.patch.object(phase, "_resume_impl_with_feedback") as resume,
    ):
        result = phase._resolve_conflict_before_review(
            issue_number=7,
            pr_number=12,
            worktree_path=tmp_path,
            branch_name="b",
            session_id="sess",
            slot_id=None,
            thread_id=None,
            state=None,
        )
    assert result is True
    rebase.assert_called_once()
    push.assert_called_once()
    resume.assert_not_called()  # agent never needed — rebase cleared it


def test_conflict_gate_dispatches_agent_when_rebase_conflicts(tmp_path: Path) -> None:
    """#1328: a still-conflicting rebase hands off to the implementation agent."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    runner = cast(Any, phase.ctx.runner)
    runner._commit_if_changes = mock.Mock(return_value=True)
    runner._push_branch = mock.Mock()
    with (
        mock.patch.object(
            phase,
            "_pr_merge_state",
            # initial=DIRTY → rebase fails → agent resolves → final=CLEAN
            side_effect=[("DIRTY", "CONFLICTING"), ("CLEAN", "MERGEABLE")],
        ),
        mock.patch(
            "hephaestus.automation._review_phase.gh_call",
            return_value=_gh_json({"baseRefName": "main"}),
        ),
        mock.patch("hephaestus.automation._review_phase.sync_worktree_to_remote_branch"),
        mock.patch("hephaestus.automation._review_phase.rebase_worktree_onto", return_value=False),
        mock.patch.object(phase, "_resume_impl_with_feedback", return_value=True) as resume,
    ):
        result = phase._resolve_conflict_before_review(
            issue_number=7,
            pr_number=12,
            worktree_path=tmp_path,
            branch_name="b",
            session_id="sess",
            slot_id=None,
            thread_id=None,
            state=None,
        )
    assert result is True
    resume.assert_called_once()
    runner._commit_if_changes.assert_called_once()
    runner._push_branch.assert_called_once()


def test_conflict_gate_unresolved_returns_false(tmp_path: Path) -> None:
    """#1328: an unresolved conflict after the agent returns False (not-GO)."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    runner = cast(Any, phase.ctx.runner)
    runner._commit_if_changes = mock.Mock(return_value=True)
    runner._push_branch = mock.Mock()
    with (
        mock.patch.object(
            phase,
            "_pr_merge_state",
            side_effect=[("DIRTY", "CONFLICTING"), ("DIRTY", "CONFLICTING")],
        ),
        mock.patch(
            "hephaestus.automation._review_phase.gh_call",
            return_value=_gh_json({"baseRefName": "main"}),
        ),
        mock.patch("hephaestus.automation._review_phase.sync_worktree_to_remote_branch"),
        mock.patch("hephaestus.automation._review_phase.rebase_worktree_onto", return_value=False),
        mock.patch.object(phase, "_resume_impl_with_feedback", return_value=True),
    ):
        result = phase._resolve_conflict_before_review(
            issue_number=7,
            pr_number=12,
            worktree_path=tmp_path,
            branch_name="b",
            session_id="sess",
            slot_id=None,
            thread_id=None,
            state=None,
        )
    assert result is False


def test_review_loop_resolves_conflict_before_first_review(tmp_path: Path) -> None:
    """#1328: the conflict gate runs BEFORE the first review iteration."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    call_order: list[str] = []

    def _gate(**_kwargs: Any) -> bool:
        call_order.append("gate")
        return True

    def _iter(**_kwargs: Any) -> tuple[Any, ...]:
        call_order.append("review")
        # verdict, grade, review_text, posted_thread_ids, go_blocked, reopened, should_break,
        # prior_reopened_keys, validator_clean
        return "GO", "A", "ok", [], False, [], True, set(), True

    with (
        mock.patch.object(phase, "_resolve_conflict_before_review", side_effect=_gate) as gate,
        mock.patch.object(phase, "_process_review_iteration", side_effect=_iter),
        mock.patch.object(phase, "_finalize_review_outcome"),
    ):
        phase._run_impl_review_loop(
            issue_number=7,
            worktree_path=tmp_path,
            branch_name="b",
            issue_title="t",
            issue_body="body",
            session_id="sess",
            slot_id=None,
            thread_id=None,
            pr_number=12,
        )
    gate.assert_called_once()
    assert call_order[0] == "gate"
    assert "review" in call_order


def test_review_loop_skips_reviewer_when_conflict_unresolved(tmp_path: Path) -> None:
    """#1328: an unresolved conflict skips the reviewer entirely (not-GO)."""
    phase = ReviewPhase(_make_ctx(tmp_path))
    with (
        mock.patch.object(phase, "_resolve_conflict_before_review", return_value=False),
        mock.patch.object(phase, "_process_review_iteration") as review,
        mock.patch.object(phase, "_finalize_review_outcome") as finalize,
    ):
        iterations, verdict, grade = phase._run_impl_review_loop(
            issue_number=7,
            worktree_path=tmp_path,
            branch_name="b",
            issue_title="t",
            issue_body="body",
            session_id="sess",
            slot_id=None,
            thread_id=None,
            pr_number=12,
        )
    review.assert_not_called()  # reviewer never runs on a conflicted PR
    assert (iterations, verdict, grade) == (0, "NOGO", None)
    finalize.assert_called_once()
