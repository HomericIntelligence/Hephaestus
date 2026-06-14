"""Tests for ImplementationPhaseRunner extracted helpers (issue #1180)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.implementer import IssueImplementer
from hephaestus.automation.models import ImplementerOptions, WorkerResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def impl(tmp_path: Path) -> IssueImplementer:
    """IssueImplementer with dry-run disabled, minimal config."""
    options = ImplementerOptions(
        epic_number=0,
        issues=[1],
        max_workers=1,
        skip_closed=False,
        auto_merge=False,
        dry_run=False,
        enable_learn=False,
        enable_follow_up=False,
        enable_ui=False,
    )
    with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
        return IssueImplementer(options)


# ---------------------------------------------------------------------------
# _dispatch_issue_work (issue #1180)
# ---------------------------------------------------------------------------


class TestDispatchIssueWork:
    """Tests for the extracted _dispatch_issue_work helper."""

    def test_dry_run_returns_success_without_worktree(self, impl: IssueImplementer) -> None:
        """In dry-run mode _dispatch_issue_work returns success without creating a worktree."""
        impl.options.dry_run = True
        runner = impl.phase_runner

        with (
            patch.object(runner.status_tracker, "update_slot"),
            patch.object(runner.worktree_manager, "create_worktree") as mock_create,
        ):
            result = runner._dispatch_issue_work(issue_number=1, slot_id=0, thread_id=None)

        mock_create.assert_not_called()
        assert result.success is True
        assert result.worktree_path is None
        assert result.branch_name == "1-auto-impl"

    def test_existing_pr_delegates_to_review_existing_pr(self, impl: IssueImplementer) -> None:
        """When an open PR exists, _dispatch_issue_work calls _review_existing_pr."""
        runner = impl.phase_runner
        expected = WorkerResult(issue_number=1, success=True, pr_number=99, already_has_pr=True)
        with (
            patch.object(runner.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=99,
            ),
            patch.object(impl, "_get_or_create_state", return_value=MagicMock()),
            patch.object(runner, "_review_existing_pr", return_value=expected) as mock_review,
        ):
            result = runner._dispatch_issue_work(issue_number=1, slot_id=0, thread_id=None)

        mock_review.assert_called_once()
        assert result.already_has_pr is True

    def test_no_existing_pr_creates_worktree(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """Without an existing PR, worktree is created and implementation runs."""
        runner = impl.phase_runner
        mock_state = MagicMock()
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()
        final = WorkerResult(issue_number=1, success=True, pr_number=42)
        with (
            patch.object(runner.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=None,
            ),
            patch.object(impl, "_get_or_create_state", return_value=mock_state),
            patch.object(impl, "_save_state"),
            patch.object(runner.worktree_manager, "create_worktree", return_value=fake_worktree),
            patch.object(runner, "_ensure_plan_ready", return_value=None),
            patch.object(runner, "_run_implementation_and_review", return_value=final),
        ):
            result = runner._dispatch_issue_work(issue_number=1, slot_id=0, thread_id=None)

        assert result.pr_number == 42


# ---------------------------------------------------------------------------
# _prepare_worktree_for_existing_pr (issue #1180)
# ---------------------------------------------------------------------------


class TestPrepareWorktreeForExistingPr:
    """Tests for the extracted _prepare_worktree_for_existing_pr helper."""

    def test_logs_real_branch_when_different_from_assumed(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """When the PR head branch differs from assumed name, the real branch is used."""
        runner = impl.phase_runner
        mock_state = MagicMock()
        real_branch = "999-other-issue"
        assumed_branch = "1-auto-impl"
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()

        with (
            patch.object(runner.status_tracker, "update_slot"),
            patch.object(impl, "_save_state"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value=real_branch,
            ),
            patch.object(runner.worktree_manager, "create_worktree", return_value=fake_worktree),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch("hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"),
        ):
            worktree_path, pr_branch = runner._prepare_worktree_for_existing_pr(
                issue_number=1,
                existing_pr=99,
                branch_name=assumed_branch,
                state=mock_state,
                slot_id=0,
                thread_id=None,
            )

        assert pr_branch == real_branch
        assert worktree_path == fake_worktree

    def test_falls_back_to_assumed_branch_on_lookup_failure(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """When get_pr_head_branch returns None, assumed branch name is used."""
        runner = impl.phase_runner
        mock_state = MagicMock()
        assumed_branch = "1-auto-impl"
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()

        with (
            patch.object(runner.status_tracker, "update_slot"),
            patch.object(impl, "_save_state"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value=None,
            ),
            patch.object(runner.worktree_manager, "create_worktree", return_value=fake_worktree),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch("hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"),
        ):
            _worktree_path, pr_branch = runner._prepare_worktree_for_existing_pr(
                issue_number=1,
                existing_pr=99,
                branch_name=assumed_branch,
                state=mock_state,
                slot_id=0,
                thread_id=None,
            )

        assert pr_branch == assumed_branch
