#!/usr/bin/env python3
"""Tests for GitHub utilities."""

from unittest.mock import MagicMock, patch

import pytest

from hephaestus.github.pr_merge import (
    detect_repo_from_remote,
    local_branch_exists,
)


class TestGitHubUtils:
    """Test GitHub utility functions."""

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_from_remote_ssh(self, mock_run):
        """Test detecting repo from SSH remote URL."""
        mock_result = MagicMock()
        mock_result.stdout = "git@github.com:owner/repo.git\n"
        mock_run.return_value = mock_result

        result = detect_repo_from_remote()
        assert result == "owner/repo"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_from_remote_https(self, mock_run):
        """Test detecting repo from HTTPS remote URL."""
        mock_result = MagicMock()
        mock_result.stdout = "https://github.com/owner/repo.git\n"
        mock_run.return_value = mock_result

        result = detect_repo_from_remote()
        assert result == "owner/repo"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_from_remote_without_git_suffix(self, mock_run):
        """Test detecting repo without .git suffix."""
        mock_result = MagicMock()
        mock_result.stdout = "https://github.com/owner/repo\n"
        mock_run.return_value = mock_result

        result = detect_repo_from_remote()
        assert result == "owner/repo"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detect_repo_from_remote_failure(self, mock_run):
        """Test detecting repo when git command fails."""
        mock_run.side_effect = Exception("Git command failed")

        result = detect_repo_from_remote()
        assert result is None

    @patch("subprocess.check_output")
    def test_local_branch_exists_true(self, mock_check_output):
        """Test checking if local branch exists (positive case)."""
        mock_check_output.return_value = b"  feature-branch\n"

        result = local_branch_exists("feature-branch")
        assert result is True

    @patch("subprocess.check_output")
    def test_local_branch_exists_false(self, mock_check_output):
        """Test checking if local branch exists (negative case)."""
        mock_check_output.return_value = b""

        result = local_branch_exists("non-existent-branch")
        assert result is False

    @patch("subprocess.check_output")
    def test_local_branch_exists_error(self, mock_check_output):
        """Test checking if local branch exists with error."""
        from subprocess import CalledProcessError
        mock_check_output.side_effect = CalledProcessError(1, ["git", "branch"])

        result = local_branch_exists("any-branch")
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
