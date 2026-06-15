"""Unit tests for the CIFixOrchestrator collaborator (refs #1179, #1289).

Covers the pure / lightly-mocked methods extracted from CIDriver: prompt
builders, the forensics marker writer, and the mechanical-rebase skip/clean
decision branches. The full agent-session paths are exercised through
``CIDriver`` delegation in ``test_ci_driver.py``.
"""

from __future__ import annotations

import json
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
    """Only BEHIND/DIRTY/CONFLICTING PRs are rebased; clean ones are skipped."""

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
