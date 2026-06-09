"""Unit tests for CI driver failing-PR discovery (#819)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation.ci_driver import (
    CIDriver,
    _pr_is_failing,
)
from hephaestus.automation.models import CIDriverOptions


@pytest.fixture
def ci_driver(tmp_path: Any) -> CIDriver:
    """Create a CI driver instance with mocked options."""
    options = CIDriverOptions(
        issues=[],
        agent="claude-opus-4-8",
        max_workers=3,
        dry_run=False,
        include_bot_prs=True,
    )
    with patch("hephaestus.automation.ci_driver.get_repo_root") as mock_root:
        mock_root.return_value = tmp_path
        return CIDriver(options)


class TestPrIsFailingPredicate:
    """Tests for the _pr_is_failing predicate filter."""

    def test_pr_is_failing_returns_true_for_failure_conclusion(self) -> None:
        """A PR with FAILURE conclusion is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
            "mergeStateStatus": "CLEAN",
        }
        assert _pr_is_failing(pr)

    def test_pr_is_failing_returns_true_for_cancelled_conclusion(self) -> None:
        """A PR with CANCELLED conclusion is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "CANCELLED"}],
            "mergeStateStatus": "CLEAN",
        }
        assert _pr_is_failing(pr)

    def test_pr_is_failing_returns_true_for_timed_out_conclusion(self) -> None:
        """A PR with TIMED_OUT conclusion is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "TIMED_OUT"}],
            "mergeStateStatus": "CLEAN",
        }
        assert _pr_is_failing(pr)

    def test_pr_is_failing_returns_true_for_blocked_merge_state(self) -> None:
        """A PR with BLOCKED merge state is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [],
            "mergeStateStatus": "BLOCKED",
        }
        assert _pr_is_failing(pr)

    def test_pr_is_failing_returns_false_for_draft_pr(self) -> None:
        """Draft PRs are excluded."""
        pr = {
            "isDraft": True,
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_returns_false_for_success_conclusion(self) -> None:
        """SUCCESS conclusion is not failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_returns_false_for_pending_conclusion(self) -> None:
        """PENDING conclusion is not considered failing (waiting for terminal state)."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "PENDING"}],
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_returns_false_for_clean_merge_state_no_failures(self) -> None:
        """CLEAN merge state with no failing conclusions is not failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_handles_missing_rollup(self) -> None:
        """Missing statusCheckRollup is treated as empty."""
        pr = {
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_handles_mixed_conclusions(self) -> None:
        """One FAILURE among SUCCESS checks means PR is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [
                {"conclusion": "SUCCESS"},
                {"conclusion": "FAILURE"},
                {"conclusion": "SUCCESS"},
            ],
            "mergeStateStatus": "CLEAN",
        }
        assert _pr_is_failing(pr)


class TestDiscoverFailingPrs:
    """Tests for _discover_failing_prs method."""

    def test_discover_failing_prs_includes_failure_checks(self, ci_driver: CIDriver) -> None:
        """PRs with FAILURE checks are discovered."""
        mock_output = [
            {
                "number": 1,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "mergeStateStatus": "CLEAN",
            },
            {
                "number": 2,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "mergeStateStatus": "CLEAN",
            },
        ]
        with patch("hephaestus.automation.ci_driver.get_repo_info") as mock_repo_info:
            mock_repo_info.return_value = ("MyOrg", "MyRepo")
            with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
                mock_gh_call.return_value = Mock(stdout=json.dumps(mock_output))
                result = ci_driver._discover_failing_prs()
        assert result == {1: 1}
        assert 2 not in result

    def test_discover_failing_prs_includes_blocked_merge_state(self, ci_driver: CIDriver) -> None:
        """PRs with BLOCKED merge state are discovered."""
        mock_output = [
            {
                "number": 3,
                "isDraft": False,
                "statusCheckRollup": [],
                "mergeStateStatus": "BLOCKED",
            },
        ]
        with patch("hephaestus.automation.ci_driver.get_repo_info") as mock_repo_info:
            mock_repo_info.return_value = ("MyOrg", "MyRepo")
            with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
                mock_gh_call.return_value = Mock(stdout=json.dumps(mock_output))
                result = ci_driver._discover_failing_prs()
        assert result == {3: 3}

    def test_discover_failing_prs_excludes_draft_prs(self, ci_driver: CIDriver) -> None:
        """Draft PRs are excluded."""
        mock_output = [
            {
                "number": 4,
                "isDraft": True,
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "mergeStateStatus": "CLEAN",
            },
        ]
        with patch("hephaestus.automation.ci_driver.get_repo_info") as mock_repo_info:
            mock_repo_info.return_value = ("MyOrg", "MyRepo")
            with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
                mock_gh_call.return_value = Mock(stdout=json.dumps(mock_output))
                result = ci_driver._discover_failing_prs()
        assert result == {}

    def test_discover_failing_prs_returns_empty_on_gh_error(self, ci_driver: CIDriver) -> None:
        """Discovery returns empty dict on gh command failure."""
        import subprocess

        with patch("hephaestus.automation.ci_driver.get_repo_info") as mock_repo_info:
            mock_repo_info.return_value = ("MyOrg", "MyRepo")
            with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
                mock_gh_call.side_effect = subprocess.CalledProcessError(1, "gh")
                result = ci_driver._discover_failing_prs()
        assert result == {}

    def test_discover_failing_prs_returns_empty_on_gh_timeout(self, ci_driver: CIDriver) -> None:
        """Discovery returns empty dict when gh pr list times out (docstring contract)."""
        import subprocess

        with patch("hephaestus.automation.ci_driver.get_repo_info") as mock_repo_info:
            mock_repo_info.return_value = ("MyOrg", "MyRepo")
            with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
                mock_gh_call.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
                result = ci_driver._discover_failing_prs()
        assert result == {}

    def test_discover_failing_prs_returns_empty_on_missing_gh_binary(
        self, ci_driver: CIDriver
    ) -> None:
        """Discovery returns empty dict when the gh binary is missing/unexecutable."""
        with patch("hephaestus.automation.ci_driver.get_repo_info") as mock_repo_info:
            mock_repo_info.return_value = ("MyOrg", "MyRepo")
            with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
                mock_gh_call.side_effect = FileNotFoundError(2, "No such file or directory", "gh")
                result = ci_driver._discover_failing_prs()
        assert result == {}

    def test_discover_failing_prs_logs_warning_on_cap_hit(self, ci_driver: CIDriver) -> None:
        """A warning is logged when the 1000-PR cap is hit."""
        mock_output = [
            {
                "number": i,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "mergeStateStatus": "CLEAN",
            }
            for i in range(1, 1001)
        ]
        with patch("hephaestus.automation.ci_driver.get_repo_info") as mock_repo_info:
            mock_repo_info.return_value = ("MyOrg", "MyRepo")
            with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
                mock_gh_call.return_value = Mock(stdout=json.dumps(mock_output))
                with patch("hephaestus.automation.ci_driver.logger") as mock_logger:
                    result = ci_driver._discover_failing_prs()
        assert len(result) == 1000
        mock_logger.warning.assert_called()

    def test_discover_failing_prs_returns_empty_on_invalid_json(self, ci_driver: CIDriver) -> None:
        """Discovery returns empty dict on invalid JSON."""
        with patch("hephaestus.automation.ci_driver.get_repo_info") as mock_repo_info:
            mock_repo_info.return_value = ("MyOrg", "MyRepo")
            with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
                mock_gh_call.return_value = Mock(stdout="not-json")
                result = ci_driver._discover_failing_prs()
        assert result == {}


class TestDiscoverPrsUnion:
    """Tests for the union of failing-PR discovery in _discover_prs."""

    def test_discover_prs_unions_failing_and_bot_paths_when_no_issues_scoped(
        self, ci_driver: CIDriver
    ) -> None:
        """Both bot and failing PRs are discovered when --issues is empty."""
        ci_driver.options.issues = []
        ci_driver.options.include_bot_prs = True

        with patch.object(ci_driver, "_discover_bot_prs") as mock_bot:
            mock_bot.return_value = {10: 10}
            with patch.object(ci_driver, "_discover_failing_prs") as mock_failing:
                mock_failing.return_value = {20: 20}
                with patch.object(ci_driver, "_find_pr_for_issue") as mock_find:
                    mock_find.return_value = None
                    result = ci_driver._discover_prs([])
        assert result == {10: 10, 20: 20}

    def test_discover_prs_skips_failing_and_bot_paths_when_issues_scoped(
        self, ci_driver: CIDriver
    ) -> None:
        """Neither failing-PR NOR bot-PR discovery is invoked when --issues is set.

        A scoped run must touch ONLY the selected issues' PRs — not unrelated
        Dependabot PRs or every failing PR on the repo (POLA, #819).
        """
        ci_driver.options.issues = [100]
        ci_driver.options.include_bot_prs = True  # default-on, must be suppressed when scoped

        with patch.object(ci_driver, "_discover_failing_prs") as mock_failing:
            with patch.object(ci_driver, "_find_pr_for_issue") as mock_find:
                mock_find.return_value = 200
                with patch.object(ci_driver, "_discover_bot_prs") as mock_bot:
                    mock_bot.return_value = {999: 999}  # would leak in if not suppressed
                    result = ci_driver._discover_prs([100])
        mock_failing.assert_not_called()
        mock_bot.assert_not_called()
        assert result == {100: 200}

    def test_discover_prs_dedupes_across_bot_and_failing(self, ci_driver: CIDriver) -> None:
        """A PR already discovered via bot path is not re-added from failing path."""
        ci_driver.options.issues = []
        ci_driver.options.include_bot_prs = True

        with patch.object(ci_driver, "_discover_bot_prs") as mock_bot:
            mock_bot.return_value = {15: 15}
            with patch.object(ci_driver, "_discover_failing_prs") as mock_failing:
                mock_failing.return_value = {15: 15, 25: 25}
                with patch.object(ci_driver, "_find_pr_for_issue") as mock_find:
                    mock_find.return_value = None
                    result = ci_driver._discover_prs([])
        assert result == {15: 15, 25: 25}


class TestIsBotPrModeForSyntheticKey:
    """Tests for _is_bot_pr_mode with failing PR synthetic keys."""

    def test_is_bot_pr_mode_holds_for_synthetic_failing_pr_key(self, ci_driver: CIDriver) -> None:
        """Failing PRs use synthetic-key invariant: pr_num == issue_num."""
        pr_num = 50
        assert ci_driver._is_bot_pr_mode(pr_num, pr_num)

    def test_is_bot_pr_mode_false_for_real_issue(self, ci_driver: CIDriver) -> None:
        """Real issue-PR mappings return False."""
        assert not ci_driver._is_bot_pr_mode(100, 200)
