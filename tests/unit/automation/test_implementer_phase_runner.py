"""Tests for ImplementationPhaseRunner extracted helpers (issue #1180)."""

from __future__ import annotations

import subprocess
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


# ---------------------------------------------------------------------------
# _commit_dirty_reused_worktree (FM2 — git add -A → git add -u)
# ---------------------------------------------------------------------------


class TestCommitDirtyReusedWorktree:
    """Tests for the ``_commit_dirty_reused_worktree`` salvage helper."""

    def test_uses_git_add_u_not_git_add_a(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """``git add -A`` MUST NOT appear; ``git add -u`` must be called instead.

        Regression guard for FM2: using ``git add -A`` swept unrelated untracked
        leftover state into the salvage commit which then conflicted on cherry-pick.
        """
        runner = impl.phase_runner
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()

        # Simulate the series of ``run()`` calls: add, commit, rev-parse
        rev_parse_result = MagicMock()
        rev_parse_result.stdout = "abc1234\n"

        with patch(
            "hephaestus.automation.implementer_phase_runner.run",
            side_effect=[
                MagicMock(),  # git add -u
                MagicMock(),  # git commit
                rev_parse_result,  # git rev-parse HEAD
            ],
        ) as mock_run:
            sha = runner._commit_dirty_reused_worktree(
                issue_number=1,
                worktree_path=fake_worktree,
                branch_name="1-auto-impl",
                thread_id=None,
            )

        # First call MUST be ``git add -u``, never ``git add -A``
        first_call_args = mock_run.call_args_list[0]
        cmd = first_call_args[0][0]  # positional list arg
        assert cmd[1] == "add", "first git subcommand must be 'add'"
        assert "-u" in cmd, "'git add -u' must be in the first run() call"
        assert "-A" not in cmd, "'git add -A' must NOT appear in any run() call"

        # Verify no other call sneaks in ``-A``
        for c in mock_run.call_args_list:
            assert "-A" not in c[0][0], "'git add -A' must not appear in any git call"

        assert sha == "abc1234"

    def test_git_add_a_never_called_with_untracked_leftover(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """Even with an untracked leftover file present, ``git add -A`` is not used."""
        runner = impl.phase_runner
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()
        # Create a realistic untracked leftover to simulate the FM2 scenario
        (fake_worktree / "leftover_unrelated.py").write_text("unrelated\n")

        rev_parse_result = MagicMock()
        rev_parse_result.stdout = "deadbeef\n"

        with patch(
            "hephaestus.automation.implementer_phase_runner.run",
            side_effect=[
                MagicMock(),
                MagicMock(),
                rev_parse_result,
            ],
        ) as mock_run:
            runner._commit_dirty_reused_worktree(
                issue_number=1,
                worktree_path=fake_worktree,
                branch_name="1-auto-impl",
                thread_id=None,
            )

        for c in mock_run.call_args_list:
            assert "-A" not in c[0][0], "'git add -A' must not appear even with untracked files"


# ---------------------------------------------------------------------------
# _restore_dirty_reused_worktree_commit_after_sync (FM2 — non-fatal cherry-pick)
# ---------------------------------------------------------------------------


class TestRestoreDirtyReusedWorktreeCommitAfterSync:
    """Tests for the cherry-pick restore helper — conflict must be non-fatal."""

    def test_successful_cherry_pick_returns_normally(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """When cherry-pick succeeds, the method returns without logging a warning."""
        runner = impl.phase_runner
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()

        with patch(
            "hephaestus.automation.implementer_phase_runner.run",
            return_value=MagicMock(),
        ) as mock_run:
            runner._restore_dirty_reused_worktree_commit_after_sync(
                issue_number=1,
                worktree_path=fake_worktree,
                branch_name="1-auto-impl",
                commit_sha="abc1234",
                thread_id=None,
            )

        # Only the cherry-pick call; no abort call
        assert mock_run.call_count == 1
        cmd = mock_run.call_args_list[0][0][0]
        assert "cherry-pick" in cmd

    def test_cherry_pick_conflict_does_not_raise(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """A conflicting cherry-pick MUST NOT propagate an exception.

        Regression guard for FM2: a fatal cherry-pick conflict killed the entire
        issue.  The fix aborts the pick and logs a warning instead of raising.
        """
        runner = impl.phase_runner
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()

        conflict_error = subprocess.CalledProcessError(
            1, ["git", "cherry-pick", "-S", "-s", "abc1234"]
        )

        with patch(
            "hephaestus.automation.implementer_phase_runner.run",
            side_effect=[
                conflict_error,  # cherry-pick fails
                MagicMock(),  # cherry-pick --abort succeeds
            ],
        ) as mock_run:
            # Must NOT raise — the issue must survive a cherry-pick conflict
            runner._restore_dirty_reused_worktree_commit_after_sync(
                issue_number=1,
                worktree_path=fake_worktree,
                branch_name="1-auto-impl",
                commit_sha="abc1234",
                thread_id=None,
            )

        # Abort must have been called after the conflict
        abort_call = mock_run.call_args_list[1]
        assert "--abort" in abort_call[0][0]

    def test_cherry_pick_conflict_triggers_abort(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """On conflict, ``git cherry-pick --abort`` is called to clean up the worktree."""
        runner = impl.phase_runner
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()

        conflict_error = subprocess.CalledProcessError(
            1, ["git", "cherry-pick", "-S", "-s", "deadbeef"]
        )

        with patch(
            "hephaestus.automation.implementer_phase_runner.run",
            side_effect=[conflict_error, MagicMock()],
        ) as mock_run:
            runner._restore_dirty_reused_worktree_commit_after_sync(
                issue_number=1,
                worktree_path=fake_worktree,
                branch_name="1-auto-impl",
                commit_sha="deadbeef",
                thread_id=None,
            )

        assert mock_run.call_count == 2
        abort_cmd = mock_run.call_args_list[1][0][0]
        assert "cherry-pick" in abort_cmd
        assert "--abort" in abort_cmd

    def test_reused_worktree_with_unrelated_dirty_file_does_not_fail_issue(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """End-to-end: unrelated dirty leftover causes cherry-pick conflict → issue continues.

        This is the exact FM2 failure scenario: git add -A → unrelated file →
        cherry-pick conflict → issue #1289 killed.  After the fix, the conflict
        is caught and the issue proceeds normally.
        """
        runner = impl.phase_runner
        fake_worktree = tmp_path / "wt"
        fake_worktree.mkdir()

        # Simulate: commit captured some unrelated file (old behaviour), then
        # cherry-pick conflicts because the synced branch diverged.
        rev_parse_result = MagicMock()
        rev_parse_result.stdout = "cafebabe\n"
        conflict_error = subprocess.CalledProcessError(
            1, ["git", "cherry-pick", "-S", "-s", "cafebabe"]
        )

        with patch(
            "hephaestus.automation.implementer_phase_runner.run",
            side_effect=[
                MagicMock(),  # git add -u (commit path)
                MagicMock(),  # git commit
                rev_parse_result,  # git rev-parse HEAD
            ],
        ):
            sha = runner._commit_dirty_reused_worktree(
                issue_number=1,
                worktree_path=fake_worktree,
                branch_name="1-auto-impl",
                thread_id=None,
            )

        assert sha == "cafebabe"

        with patch(
            "hephaestus.automation.implementer_phase_runner.run",
            side_effect=[conflict_error, MagicMock()],
        ):
            # Must not raise — the issue must survive the cherry-pick conflict
            runner._restore_dirty_reused_worktree_commit_after_sync(
                issue_number=1,
                worktree_path=fake_worktree,
                branch_name="1-auto-impl",
                commit_sha=sha,
                thread_id=None,
            )
