"""Unit tests for the CIFixOrchestrator collaborator (refs #1179, #1289).

Covers the pure / lightly-mocked methods extracted from CIDriver: prompt
builders, the forensics marker writer, and the mechanical-rebase skip/clean
decision branches. The full agent-session paths are exercised through
``CIDriver`` delegation in ``test_ci_driver.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.ci_fix_orchestrator import CIFixOrchestrator


@pytest.fixture()
def orchestrator(tmp_path: Path) -> CIFixOrchestrator:
    """Return a CIFixOrchestrator wired with simple test doubles."""
    options = MagicMock()
    options.agent = "claude"
    options.dry_run = False
    status = MagicMock()
    return CIFixOrchestrator(
        options_provider=lambda: options,
        repo_root_provider=lambda: tmp_path,
        state_dir_provider=lambda: tmp_path,
        status_tracker_provider=lambda: status,
        get_pr_branch=lambda pr: f"{pr}-impl",
        get_worktree_path=lambda issue, pr: tmp_path,
        format_review_threads_block=lambda pr: "",
        failing_required_check_names=lambda pr: [],
    )


def _orchestrator_with_failing_checks(
    tmp_path: Path, failing_checks: list[str]
) -> CIFixOrchestrator:
    options = MagicMock()
    options.agent = "claude"
    options.dry_run = False
    status = MagicMock()
    return CIFixOrchestrator(
        options_provider=lambda: options,
        repo_root_provider=lambda: tmp_path,
        state_dir_provider=lambda: tmp_path,
        status_tracker_provider=lambda: status,
        get_pr_branch=lambda pr: f"{pr}-impl",
        get_worktree_path=lambda issue, pr: tmp_path,
        format_review_threads_block=lambda pr: "",
        failing_required_check_names=lambda pr: failing_checks,
    )


class TestForceEngagementPrompt:
    """The retry prompt must name failing checks/dirty files verbatim."""

    def test_names_failing_checks_and_branch(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        prompt = orchestrator.force_engagement_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            pr_head_branch="1-fix",
            failing_check_names=["lint", "test-py310"],
            review_threads_block="",
        )
        assert "- lint" in prompt
        assert "- test-py310" in prompt
        assert "1-fix" in prompt
        assert "BLOCKED:" in prompt

    def test_dirty_changes_block_rendered(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        prompt = orchestrator.force_engagement_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            pr_head_branch="1-fix",
            failing_check_names=[],
            review_threads_block="",
            dirty_tracked_changes=[" M src/a.py"],
        )
        assert "uncommitted tracked changes" in prompt
        assert "M src/a.py" in prompt

    def test_review_threads_block_prepended(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        prompt = orchestrator.force_engagement_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            pr_head_branch="1-fix",
            failing_check_names=["lint"],
            review_threads_block="## Unresolved PR Review Threads\n\nSee below.\n",
        )
        assert prompt.startswith("## Unresolved PR Review Threads")


class TestBuildCiFixPrompt:
    """The fix prompt folds advise findings + review threads + CI logs."""

    def test_includes_advise_and_logs(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        prompt = orchestrator.build_ci_fix_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            ci_logs="boom: import error",
            pr_head_branch="1-fix",
            advise_findings="Prior lesson: pin deps.",
        )
        assert "Prior Learnings from Team Knowledge Base" in prompt
        assert "Prior lesson: pin deps." in prompt
        assert "boom: import error" in prompt
        assert "1-fix" in prompt

    def test_includes_failing_check_names(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator_with_failing_checks(
            tmp_path, ["pr-policy", "required-checks-gate"]
        )
        prompt = orchestrator.build_ci_fix_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            ci_logs="python3: can't open file 'scripts/check_conventional_commit.py'",
            pr_head_branch="1-fix",
            advise_findings="",
        )
        assert "Failing checks reported by GitHub" in prompt
        assert "- pr-policy" in prompt
        assert "- required-checks-gate" in prompt
        assert "aggregate" in prompt

    def test_hard_scope_limits_forbid_doc_rewrites(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        """The prompt must forbid README/doc rewrites and delete-to-silence.

        Live CI-fix sessions repeatedly replaced the entire ProjectOdyssey
        README with a placeholder ('Description here.') when they could not
        fix a compile error (#1780 shakedown, three occurrences).
        """
        prompt = orchestrator.build_ci_fix_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            ci_logs="error: whatever",
            pr_head_branch="1-fix",
            advise_findings="",
        )
        assert "HARD SCOPE LIMITS" in prompt
        assert "never replace the" in prompt
        assert "say so and stop" in prompt

    def test_skip_marker_advise_contributes_nothing(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        prompt = orchestrator.build_ci_fix_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            ci_logs="",
            pr_head_branch="1-fix",
            advise_findings="<!-- advise step skipped -->",
        )
        assert "Prior Learnings from Team Knowledge Base" not in prompt


class TestRetryWorktreeChanges:
    """No-commit retries should notice relevant new files without sweeping junk."""

    def test_relevant_untracked_files_are_actionable(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        status = MagicMock(
            stdout=(
                " M hephaestus/automation/ci_driver.py\n"
                "?? scripts/check_conventional_commit.py\n"
                "?? tests/unit/scripts/test_check_conventional_commit.py\n"
                "?? uv.lock\n"
                "?? .pytest_cache/v/cache/nodeids\n"
            ),
            stderr="",
            returncode=0,
        )
        with patch("hephaestus.automation.ci_fix_orchestrator.run", return_value=status):
            changes = orchestrator._tracked_worktree_changes(tmp_path, 1515)
        assert " M hephaestus/automation/ci_driver.py" in changes
        assert "?? scripts/check_conventional_commit.py" in changes
        assert "?? tests/unit/scripts/test_check_conventional_commit.py" in changes
        assert all("uv.lock" not in line for line in changes)
        assert all(".pytest_cache" not in line for line in changes)


class TestPushCiFix:
    """The post-agent push path normalizes dirty but resolved tracked changes."""

    def test_commits_resolved_dirty_tracked_changes_before_push(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        with (
            patch.object(orchestrator, "_head_advanced", return_value=True),
            patch.object(
                orchestrator,
                "_ci_fix_head_is_pushable",
                side_effect=[False, True],
            ),
            patch.object(orchestrator, "_ci_fix_residual_commit_is_safe", return_value=True),
            patch.object(
                orchestrator,
                "_tracked_worktree_changes",
                return_value=["MM hephaestus/automation/ci_driver.py"],
            ),
            patch(
                "hephaestus.automation.ci_fix_orchestrator.commit_if_changes",
                return_value=True,
            ) as commit,
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ) as push,
        ):
            pushed = orchestrator.push_ci_fix(
                worktree_path=tmp_path,
                pre_agent_sha="abc123",
                issue_number=1405,
                pr_number=1633,
                pr_head_branch="1405-auto-impl",
                session_id=None,
            )

        assert pushed is True
        commit.assert_called_once_with(
            1405,
            tmp_path,
            "claude",
            committed_log_message="Committed CI-fix residual changes for issue #%s",
            allowed_paths=("hephaestus/automation/ci_driver.py",),
        )
        push.assert_called_once()

    def test_ignores_untracked_residual_artifacts_when_committing(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        with (
            patch.object(orchestrator, "_head_advanced", return_value=True),
            patch.object(
                orchestrator,
                "_ci_fix_head_is_pushable",
                side_effect=[False, True],
            ),
            patch.object(orchestrator, "_ci_fix_residual_commit_is_safe", return_value=True),
            patch.object(
                orchestrator,
                "_tracked_worktree_changes",
                return_value=[
                    "MM hephaestus/automation/ci_driver.py",
                    "?? output.log",
                ],
            ),
            patch(
                "hephaestus.automation.ci_fix_orchestrator.commit_if_changes",
                return_value=True,
            ) as commit,
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ),
        ):
            pushed = orchestrator.push_ci_fix(
                worktree_path=tmp_path,
                pre_agent_sha="abc123",
                issue_number=1405,
                pr_number=1633,
                pr_head_branch="1405-auto-impl",
                session_id=None,
            )

        assert pushed is True
        commit.assert_called_once_with(
            1405,
            tmp_path,
            "claude",
            committed_log_message="Committed CI-fix residual changes for issue #%s",
            allowed_paths=("hephaestus/automation/ci_driver.py",),
        )

    def test_returns_false_when_residual_commit_fails(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        commit_error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["git", "commit"],
            stderr="signing failed",
        )
        with (
            patch.object(orchestrator, "_head_advanced", return_value=True),
            patch.object(orchestrator, "_ci_fix_head_is_pushable", return_value=False),
            patch.object(orchestrator, "_ci_fix_residual_commit_is_safe", return_value=True),
            patch.object(
                orchestrator,
                "_tracked_worktree_changes",
                return_value=["MM hephaestus/automation/ci_driver.py"],
            ),
            patch(
                "hephaestus.automation.ci_fix_orchestrator.commit_if_changes",
                side_effect=commit_error,
            ),
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ) as push,
        ):
            pushed = orchestrator.push_ci_fix(
                worktree_path=tmp_path,
                pre_agent_sha="abc123",
                issue_number=1405,
                pr_number=1633,
                pr_head_branch="1405-auto-impl",
                session_id=None,
            )

        assert pushed is False
        push.assert_not_called()

    def test_returns_false_when_residual_commit_does_not_restore_pushability(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        with (
            patch.object(orchestrator, "_head_advanced", return_value=True),
            patch.object(
                orchestrator,
                "_ci_fix_head_is_pushable",
                side_effect=[False, False],
            ),
            patch.object(orchestrator, "_ci_fix_residual_commit_is_safe", return_value=True),
            patch.object(
                orchestrator,
                "_tracked_worktree_changes",
                return_value=["MM hephaestus/automation/ci_driver.py"],
            ),
            patch(
                "hephaestus.automation.ci_fix_orchestrator.commit_if_changes",
                return_value=True,
            ),
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ) as push,
        ):
            pushed = orchestrator.push_ci_fix(
                worktree_path=tmp_path,
                pre_agent_sha="abc123",
                issue_number=1405,
                pr_number=1633,
                pr_head_branch="1405-auto-impl",
                session_id=None,
            )

        assert pushed is False
        push.assert_not_called()

    def test_does_not_commit_residuals_when_head_is_not_ahead(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        responses = [
            MagicMock(stdout="", stderr="", returncode=0),  # no unmerged paths
            MagicMock(stdout="0\n", stderr="", returncode=0),  # no commits ahead of origin/main
        ]
        with (
            patch.object(orchestrator, "_head_advanced", return_value=True),
            patch.object(orchestrator, "_ci_fix_head_is_pushable", return_value=False),
            patch(
                "hephaestus.automation.ci_fix_orchestrator.run",
                side_effect=responses,
            ),
            patch.object(
                orchestrator,
                "_tracked_worktree_changes",
                return_value=["MM hephaestus/automation/ci_driver.py"],
            ) as tracked,
            patch("hephaestus.automation.ci_fix_orchestrator.commit_if_changes") as commit,
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ) as push,
        ):
            pushed = orchestrator.push_ci_fix(
                worktree_path=tmp_path,
                pre_agent_sha="abc123",
                issue_number=1405,
                pr_number=1633,
                pr_head_branch="1405-auto-impl",
                session_id=None,
            )

        assert pushed is False
        tracked.assert_not_called()
        commit.assert_not_called()
        push.assert_not_called()

    def test_does_not_commit_residuals_when_push_guard_cannot_inspect_ahead(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        responses = [
            MagicMock(stdout="", stderr="", returncode=0),  # no unmerged paths
            MagicMock(stdout="fatal: bad revision\n", stderr="", returncode=128),
        ]
        with (
            patch.object(orchestrator, "_head_advanced", return_value=True),
            patch.object(orchestrator, "_ci_fix_head_is_pushable", return_value=False),
            patch(
                "hephaestus.automation.ci_fix_orchestrator.run",
                side_effect=responses,
            ),
            patch.object(
                orchestrator,
                "_tracked_worktree_changes",
                return_value=["MM hephaestus/automation/ci_driver.py"],
            ) as tracked,
            patch("hephaestus.automation.ci_fix_orchestrator.commit_if_changes") as commit,
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ) as push,
        ):
            pushed = orchestrator.push_ci_fix(
                worktree_path=tmp_path,
                pre_agent_sha="abc123",
                issue_number=1405,
                pr_number=1633,
                pr_head_branch="1405-auto-impl",
                session_id=None,
            )

        assert pushed is False
        tracked.assert_not_called()
        commit.assert_not_called()
        push.assert_not_called()

    def test_does_not_commit_unresolved_merge_residuals(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        with (
            patch.object(orchestrator, "_head_advanced", return_value=True),
            patch.object(orchestrator, "_ci_fix_head_is_pushable", return_value=False),
            patch.object(orchestrator, "_ci_fix_residual_commit_is_safe", return_value=True),
            patch.object(
                orchestrator,
                "_tracked_worktree_changes",
                return_value=["AA hephaestus/automation/ci_driver.py"],
            ),
            patch("hephaestus.automation.ci_fix_orchestrator.commit_if_changes") as commit,
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ) as push,
        ):
            pushed = orchestrator.push_ci_fix(
                worktree_path=tmp_path,
                pre_agent_sha="abc123",
                issue_number=1405,
                pr_number=1633,
                pr_head_branch="1405-auto-impl",
                session_id=None,
            )

        assert pushed is False
        commit.assert_not_called()
        push.assert_not_called()


class TestRecordRepeatedNoCommit:
    """The forensics marker is written into the state dir."""

    def test_writes_marker_with_payload(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        orchestrator.record_repeated_no_commit(
            issue_number=1,
            pr_number=2,
            pr_head_branch="1-fix",
            failing_check_names=["lint"],
        )
        marker = tmp_path / "repeated-no-commit-2.json"
        assert marker.exists()
        payload = json.loads(marker.read_text())
        assert payload["pr_number"] == 2
        assert payload["pr_head_branch"] == "1-fix"
        assert payload["failing_required_checks"] == ["lint"]


class TestAttemptMechanicalRebase:
    """Only stale/conflicting or failing-check BLOCKED PRs are rebased."""

    @staticmethod
    def _pr_state(merge_state: str, head: str = "5-impl", base: str = "main") -> MagicMock:
        return MagicMock(
            stdout=json.dumps(
                {
                    "mergeStateStatus": merge_state,
                    "mergeable": "MERGEABLE",
                    "headRefName": head,
                    "baseRefName": base,
                }
            )
        )

    def test_clean_pr_skips_rebase(self, orchestrator: CIFixOrchestrator) -> None:
        with (
            patch(
                "hephaestus.automation.ci_fix_orchestrator._gh_call",
                return_value=self._pr_state("CLEAN"),
            ),
            patch("hephaestus.automation.ci_fix_orchestrator.rebase_worktree_onto") as mock_rebase,
        ):
            assert orchestrator.attempt_mechanical_rebase(5, 50, 0) is False
        mock_rebase.assert_not_called()

    def test_blocked_pr_without_failing_checks_skips_rebase(
        self, orchestrator: CIFixOrchestrator
    ) -> None:
        with (
            patch(
                "hephaestus.automation.ci_fix_orchestrator._gh_call",
                return_value=self._pr_state("BLOCKED"),
            ),
            patch("hephaestus.automation.ci_fix_orchestrator.rebase_worktree_onto") as mock_rebase,
        ):
            assert orchestrator.attempt_mechanical_rebase(5, 50, 0) is False
        mock_rebase.assert_not_called()

    def test_blocked_pr_with_failing_checks_rebases_clean_and_pushes(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator_with_failing_checks(tmp_path, ["pr-policy"])
        with (
            patch(
                "hephaestus.automation.ci_fix_orchestrator._gh_call",
                return_value=self._pr_state("BLOCKED"),
            ),
            patch("hephaestus.automation.ci_fix_orchestrator.sync_worktree_to_remote_branch"),
            patch(
                "hephaestus.automation.ci_fix_orchestrator.rebase_worktree_onto",
                return_value=True,
            ) as mock_rebase,
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ) as mock_push,
        ):
            assert orchestrator.attempt_mechanical_rebase(5, 50, 0) is True
        mock_rebase.assert_called_once_with(tmp_path, "main")
        mock_push.assert_called_once()

    def test_behind_pr_rebases_clean_and_pushes(
        self, orchestrator: CIFixOrchestrator, tmp_path: Path
    ) -> None:
        with (
            patch(
                "hephaestus.automation.ci_fix_orchestrator._gh_call",
                return_value=self._pr_state("BEHIND"),
            ),
            patch("hephaestus.automation.ci_fix_orchestrator.sync_worktree_to_remote_branch"),
            patch(
                "hephaestus.automation.ci_fix_orchestrator.rebase_worktree_onto",
                return_value=True,
            ) as mock_rebase,
            patch(
                "hephaestus.automation.ci_fix_orchestrator."
                "push_current_branch_with_lease_on_divergence"
            ) as mock_push,
        ):
            assert orchestrator.attempt_mechanical_rebase(5, 50, 0) is True
        mock_rebase.assert_called_once_with(tmp_path, "main")
        mock_push.assert_called_once()

    def test_gh_query_failure_swallowed(self, orchestrator: CIFixOrchestrator) -> None:
        with patch(
            "hephaestus.automation.ci_fix_orchestrator._gh_call",
            return_value=MagicMock(stdout="not json"),
        ):
            assert orchestrator.attempt_mechanical_rebase(5, 50, 0) is False
