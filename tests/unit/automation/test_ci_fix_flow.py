"""Behavioral tests for the high-level CI-fix flow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from hephaestus.automation.ci_fix_flow import CIFixFlow
from hephaestus.automation.models import CIDriverOptions


def _flow(tmp_path: Path, **overrides: Any) -> tuple[CIFixFlow, SimpleNamespace]:
    """Build a CI-fix flow with observable, hermetic collaborators."""
    options = SimpleNamespace(
        agent="claude",
        advise_timeout=30,
        dry_run=False,
        enable_advise=False,
        max_fix_iterations=2,
    )
    for name, value in overrides.items():
        setattr(options, name, value)
    collaborators = SimpleNamespace(
        options=options,
        status=mock.MagicMock(),
        orchestrator=mock.MagicMock(),
        markers=mock.MagicMock(),
        bot_mode=mock.MagicMock(return_value=False),
        issue_json=mock.MagicMock(return_value={"title": "Title", "body": "Body"}),
        logs=mock.MagicMock(return_value="failing logs"),
        session=mock.MagicMock(return_value="session-7"),
        worktree=mock.MagicMock(return_value=tmp_path),
        branch=mock.MagicMock(return_value="feature-branch"),
    )
    collaborators.markers.already_pushed_for_current_head.return_value = False
    flow = CIFixFlow(
        options_provider=lambda: cast(CIDriverOptions, options),
        repo_root_provider=lambda: tmp_path,
        status_tracker_provider=lambda: collaborators.status,
        orchestrator=collaborators.orchestrator,
        markers=collaborators.markers,
        is_bot_pr_mode=collaborators.bot_mode,
        gh_issue_json=collaborators.issue_json,
        get_failing_ci_logs=collaborators.logs,
        load_impl_session_id=collaborators.session,
        get_worktree_path=collaborators.worktree,
        get_pr_branch=collaborators.branch,
    )
    return flow, collaborators


def test_attempt_ci_fixes_skips_head_already_fixed(tmp_path: Path) -> None:
    """An unchanged head is not sent through the implementation agent twice."""
    flow, deps = _flow(tmp_path)
    deps.markers.already_pushed_for_current_head.return_value = True

    result = flow.attempt_ci_fixes(7, 42, 1)

    assert result is not None and result.success is True and result.pr_number == 42
    deps.orchestrator.run_ci_fix_session.assert_not_called()


def test_attempt_ci_fixes_dry_run_collects_context_without_mutating(tmp_path: Path) -> None:
    """Dry-run exercises discovery but never invokes the fixing agent."""
    flow, deps = _flow(tmp_path, dry_run=True)

    result = flow.attempt_ci_fixes(7, 42, 1, extra_context="host failure")

    assert result is not None and result.success is True
    deps.logs.assert_called_once_with(42)
    deps.orchestrator.run_ci_fix_session.assert_not_called()


def test_attempt_ci_fixes_retries_then_records_success(tmp_path: Path) -> None:
    """A later successful attempt records the resulting PR head exactly once."""
    flow, deps = _flow(tmp_path)
    deps.orchestrator.run_ci_fix_session.side_effect = [False, True]

    result = flow.attempt_ci_fixes(7, 42, 1, extra_context="host failure")

    assert result is not None and result.success is True
    assert deps.orchestrator.run_ci_fix_session.call_count == 2
    first = deps.orchestrator.run_ci_fix_session.call_args_list[0]
    assert first.args == (7, 42, tmp_path, "host failure\n\nfailing logs", "session-7", "")
    assert first.kwargs == {"pr_head_branch": "feature-branch"}
    deps.markers.record_head.assert_called_once_with(42)


def test_attempt_ci_fixes_returns_none_after_budget_exhaustion(tmp_path: Path) -> None:
    """Repeated agent failures consume the bounded attempt budget."""
    flow, deps = _flow(tmp_path, max_fix_iterations=3)
    deps.orchestrator.run_ci_fix_session.return_value = False

    assert flow.attempt_ci_fixes(7, 42, 1) is None
    assert deps.orchestrator.run_ci_fix_session.call_count == 3
    deps.markers.record_head.assert_not_called()


def test_attempt_ci_fixes_advises_only_for_non_bot_pr(tmp_path: Path) -> None:
    """Prior learnings enrich ordinary PR fixes but are skipped for bot PRs."""
    flow, deps = _flow(tmp_path, enable_advise=True, max_fix_iterations=1)
    deps.orchestrator.run_ci_fix_session.return_value = True
    with mock.patch.object(flow, "run_advise", return_value="prior learning") as advise:
        flow.attempt_ci_fixes(7, 42, 1)
    advise.assert_called_once_with(7)
    assert deps.orchestrator.run_ci_fix_session.call_args.args[5] == "prior learning"

    deps.bot_mode.return_value = True
    deps.markers.reset_mock()
    deps.orchestrator.reset_mock()
    deps.orchestrator.run_ci_fix_session.return_value = True
    with mock.patch.object(flow, "run_advise") as advise:
        flow.attempt_ci_fixes(7, 42, 1)
    advise.assert_not_called()


def test_run_advise_invokes_claude_read_only(tmp_path: Path) -> None:
    """Claude advice uses the bounded read-only session and strips output."""
    flow, _deps = _flow(tmp_path)
    with (
        mock.patch("hephaestus.automation.ci_fix_flow.run_advise") as run,
        mock.patch(
            "hephaestus.automation.ci_fix_flow.invoke_claude_with_session",
            return_value=("  finding  ", "session"),
        ) as invoke,
        mock.patch("hephaestus.automation.ci_fix_flow.get_repo_slug", return_value="o/r"),
    ):
        run.side_effect = lambda **kwargs: kwargs["invoke"]("prompt")
        assert flow.run_advise(7) == "finding"

    assert invoke.call_args.kwargs["allowed_tools"] == "Read,Glob,Grep,Bash"
    assert invoke.call_args.kwargs["cwd"] == tmp_path


def test_run_advise_invokes_direct_agent_read_only(tmp_path: Path) -> None:
    """Direct-agent advice retains the same read-only boundary."""
    flow, _deps = _flow(tmp_path, agent="codex")
    result = SimpleNamespace(stdout="  finding  ")
    with (
        mock.patch("hephaestus.automation.ci_fix_flow.run_advise") as run,
        mock.patch(
            "hephaestus.automation.ci_fix_flow.run_agent_session", return_value=result
        ) as invoke,
        mock.patch("hephaestus.automation.ci_fix_flow.direct_agent_model", return_value="model"),
    ):
        run.side_effect = lambda **kwargs: kwargs["invoke"]("prompt")
        assert flow.run_advise(7) == "finding"

    assert invoke.call_args.kwargs["sandbox"] == "read-only"
    assert invoke.call_args.kwargs["model"] == "model"
