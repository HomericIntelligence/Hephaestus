#!/usr/bin/env python3
"""Tests for GitHub utilities."""

from subprocess import CalledProcessError
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.github.pr_merge import (
    checks_success_and_print,
    detect_repo_from_remote,
    handle_merge_result,
    legacy_status_and_print,
    local_branch_exists,
    run_git_cmd,
    try_push_head_branch,
)


class TestDetectRepoFromRemote:
    """Tests for detect_repo_from_remote."""

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_ssh_url(self, mock_run):
        """Detects repo from SSH remote URL."""
        mock_result = MagicMock()
        mock_result.stdout = "git@github.com:owner/repo.git\n"
        mock_run.return_value = mock_result
        result = detect_repo_from_remote()
        assert result == "owner/repo"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_https_url(self, mock_run):
        """Detects repo from HTTPS remote URL."""
        mock_result = MagicMock()
        mock_result.stdout = "https://github.com/owner/repo.git\n"
        mock_run.return_value = mock_result
        result = detect_repo_from_remote()
        assert result == "owner/repo"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_without_git_suffix(self, mock_run):
        """Detects repo from URL without .git suffix."""
        mock_result = MagicMock()
        mock_result.stdout = "https://github.com/owner/repo\n"
        mock_run.return_value = mock_result
        result = detect_repo_from_remote()
        assert result == "owner/repo"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_failure_returns_none(self, mock_run):
        """Returns None when git command fails."""
        mock_run.side_effect = Exception("Git command failed")
        result = detect_repo_from_remote()
        assert result is None

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_non_github_url_returns_none(self, mock_run):
        """Returns None for non-GitHub remote URLs."""
        mock_result = MagicMock()
        mock_result.stdout = "https://gitlab.com/owner/repo.git\n"
        mock_run.return_value = mock_result
        result = detect_repo_from_remote()
        assert result is None


class TestLocalBranchExists:
    """Tests for local_branch_exists."""

    @patch("subprocess.check_output")
    def test_branch_exists(self, mock_check_output):
        """Returns True when branch exists."""
        mock_check_output.return_value = b"  feature-branch\n"
        result = local_branch_exists("feature-branch")
        assert result is True

    @patch("subprocess.check_output")
    def test_branch_not_exists(self, mock_check_output):
        """Returns False when branch doesn't exist (empty output)."""
        mock_check_output.return_value = b""
        result = local_branch_exists("non-existent-branch")
        assert result is False

    @patch("subprocess.check_output")
    def test_branch_check_error_returns_false(self, mock_check_output):
        """Returns False on CalledProcessError."""
        mock_check_output.side_effect = CalledProcessError(1, ["git", "branch"])
        result = local_branch_exists("any-branch")
        assert result is False

    @patch("subprocess.check_output")
    def test_branch_with_whitespace_output(self, mock_check_output):
        """Branch name with whitespace in output still returns True."""
        mock_check_output.return_value = b"  main  \n"
        result = local_branch_exists("main")
        assert result is True


class TestRunGitCmd:
    """Tests for run_git_cmd."""

    def test_dry_run_does_not_call_subprocess(self):
        """In dry-run mode, no subprocess is called."""
        with patch("subprocess.run") as mock_run:
            run_git_cmd(["git", "push", "origin", "main"], dry_run=True)
            mock_run.assert_not_called()

    def test_non_dry_run_calls_subprocess(self):
        """In non-dry-run mode, subprocess is called."""
        with patch("subprocess.run") as mock_run:
            run_git_cmd(["git", "status"], dry_run=False)
            mock_run.assert_called_once()


class TestChecksSuccessAndPrint:
    """Tests for checks_success_and_print."""

    def test_all_checks_successful(self):
        """Returns True when all checks succeed."""
        mock_commit = MagicMock()
        check1 = MagicMock(name="check1", status="completed", conclusion="success")
        check1.name = "test"
        mock_commit.get_check_runs.return_value = [check1]

        success, checks = checks_success_and_print(mock_commit)
        assert success is True
        assert len(checks) == 1

    def test_check_with_failure_conclusion(self):
        """Returns False when a check has 'failure' conclusion."""
        mock_commit = MagicMock()
        check1 = MagicMock(status="completed", conclusion="failure")
        check1.name = "test"
        mock_commit.get_check_runs.return_value = [check1]

        success, _ = checks_success_and_print(mock_commit)
        assert success is False

    def test_check_not_completed(self):
        """Returns False when a check is not yet completed."""
        mock_commit = MagicMock()
        check1 = MagicMock(status="in_progress", conclusion=None)
        check1.name = "test"
        mock_commit.get_check_runs.return_value = [check1]

        success, _ = checks_success_and_print(mock_commit)
        assert success is False

    def test_no_checks_returns_none(self):
        """Returns (None, []) when there are no check runs."""
        mock_commit = MagicMock()
        mock_commit.get_check_runs.return_value = []

        success, checks = checks_success_and_print(mock_commit)
        assert success is None
        assert checks == []

    def test_check_run_exception_returns_none(self):
        """Returns (None, []) when get_check_runs raises."""
        mock_commit = MagicMock()
        mock_commit.get_check_runs.side_effect = Exception("API error")

        success, checks = checks_success_and_print(mock_commit)
        assert success is None
        assert checks == []


class TestLegacyStatusAndPrint:
    """Tests for legacy_status_and_print."""

    def test_returns_state_string(self):
        """Returns the combined status state."""
        mock_commit = MagicMock()
        mock_commit.get_combined_status.return_value = MagicMock(
            statuses=[], state="success"
        )
        result = legacy_status_and_print(mock_commit)
        assert result == "success"

    def test_returns_unknown_on_exception(self):
        """Returns 'unknown' when API raises an exception."""
        mock_commit = MagicMock()
        mock_commit.get_combined_status.side_effect = Exception("API error")
        result = legacy_status_and_print(mock_commit)
        assert result == "unknown"

    def test_returns_unknown_when_state_is_none(self):
        """Returns 'unknown' when state is None."""
        mock_commit = MagicMock()
        mock_commit.get_combined_status.return_value = MagicMock(statuses=[], state=None)
        result = legacy_status_and_print(mock_commit)
        assert result == "unknown"


class TestHandleMergeResult:
    """Tests for handle_merge_result."""

    def test_successful_merge_logged(self):
        """Successful merge is logged."""
        result = MagicMock(merged=True, sha="abc123", message="Merged")
        # Should not raise
        handle_merge_result(result, pr_number=42, base_branch="main")

    def test_failed_merge_logged(self):
        """Failed merge is logged as error."""
        result = MagicMock(merged=False, sha=None, message="Merge conflict")
        # Should not raise
        handle_merge_result(result, pr_number=42, base_branch="main")

    def test_exception_during_result_parsing(self):
        """Handles exception during result attribute access."""

        class BadResult:
            @property
            def merged(self):
                raise AttributeError("no merged attr")

        # Should not raise
        handle_merge_result(BadResult(), pr_number=1, base_branch="main")


class TestTryPushHeadBranch:
    """Tests for try_push_head_branch."""

    def test_dry_run_does_not_push(self):
        """In dry-run mode, no push happens."""
        with patch("hephaestus.github.pr_merge.run_git_cmd") as mock_cmd:
            try_push_head_branch("feature", dry_run=True)
            mock_cmd.assert_not_called()

    @patch("hephaestus.github.pr_merge.local_branch_exists", return_value=True)
    def test_pushes_when_branch_exists(self, mock_exists):
        """Pushes branch when it exists locally."""
        with patch("hephaestus.github.pr_merge.run_git_cmd") as mock_cmd:
            try_push_head_branch("feature", dry_run=False)
            mock_cmd.assert_called_once()

    @patch("hephaestus.github.pr_merge.local_branch_exists", return_value=False)
    def test_skips_push_when_branch_missing(self, mock_exists):
        """Does not push when local branch doesn't exist."""
        with patch("hephaestus.github.pr_merge.run_git_cmd") as mock_cmd:
            try_push_head_branch("feature", dry_run=False)
            mock_cmd.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
