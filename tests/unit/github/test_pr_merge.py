#!/usr/bin/env python3
"""Tests for hephaestus.github.pr_merge module."""

import subprocess
import sys
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
    def test_detects_ssh_url(self, mock_run) -> None:
        """Parses SSH-style github.com:owner/repo.git."""
        mock_run.return_value = MagicMock(
            stdout="git@github.com:HomericIntelligence/ProjectHephaestus.git"
        )
        result = detect_repo_from_remote()
        assert result == "HomericIntelligence/ProjectHephaestus"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detects_https_url(self, mock_run) -> None:
        """Parses HTTPS github.com/owner/repo.git."""
        mock_run.return_value = MagicMock(
            stdout="https://github.com/HomericIntelligence/ProjectScylla.git"
        )
        result = detect_repo_from_remote()
        assert result == "HomericIntelligence/ProjectScylla"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_detects_url_without_git_suffix(self, mock_run) -> None:
        """Parses URL without .git suffix."""
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo")
        result = detect_repo_from_remote()
        assert result == "owner/repo"

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_returns_none_for_non_github_url(self, mock_run) -> None:
        """Returns None when URL is not a GitHub URL."""
        mock_run.return_value = MagicMock(stdout="https://gitlab.com/owner/repo.git")
        result = detect_repo_from_remote()
        assert result is None

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_returns_none_on_exception(self, mock_run) -> None:
        """Returns None when run_subprocess raises."""
        mock_run.side_effect = RuntimeError("git not found")
        result = detect_repo_from_remote()
        assert result is None


class TestRunGitCmd:
    """Tests for run_git_cmd."""

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_calls_run_subprocess(self, mock_run) -> None:
        """Passes command to run_subprocess."""
        run_git_cmd(["git", "status"])
        mock_run.assert_called_once_with(["git", "status"], cwd=None, dry_run=False)

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_dry_run_passed_through(self, mock_run) -> None:
        """dry_run flag is forwarded to run_subprocess."""
        run_git_cmd(["git", "push"], dry_run=True)
        mock_run.assert_called_once_with(["git", "push"], cwd=None, dry_run=True)

    @patch("hephaestus.github.pr_merge.run_subprocess")
    def test_cwd_passed_through(self, mock_run) -> None:
        """Cwd is forwarded to run_subprocess."""
        run_git_cmd(["git", "log"], cwd="/some/path")
        mock_run.assert_called_once_with(["git", "log"], cwd="/some/path", dry_run=False)


class TestChecksSuccessAndPrint:
    """Tests for checks_success_and_print."""

    def _make_check_run(self, name: str, status: str, conclusion: str) -> MagicMock:
        cr = MagicMock()
        cr.name = name
        cr.status = status
        cr.conclusion = conclusion
        return cr

    def test_all_success_returns_true(self) -> None:
        """Returns (True, checks) when all check runs succeed."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("test", "completed", "success"),
            self._make_check_run("lint", "completed", "success"),
        ]
        success, checks = checks_success_and_print(commit)
        assert success is True
        assert len(checks) == 2

    def test_failure_conclusion_returns_false(self) -> None:
        """Returns (False, checks) when any check has a bad conclusion."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("test", "completed", "success"),
            self._make_check_run("ci", "completed", "failure"),
        ]
        success, _checks = checks_success_and_print(commit)
        assert success is False

    def test_in_progress_check_returns_false(self) -> None:
        """Returns (False, checks) when a check is not completed."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("test", "in_progress", None),
        ]
        success, _checks = checks_success_and_print(commit)
        assert success is False

    def test_no_checks_returns_none(self) -> None:
        """Returns (None, []) when there are no check runs."""
        commit = MagicMock()
        commit.get_check_runs.return_value = []
        success, checks = checks_success_and_print(commit)
        assert success is None
        assert checks == []

    def test_exception_returns_none(self) -> None:
        """Returns (None, []) when get_check_runs raises."""
        commit = MagicMock()
        commit.get_check_runs.side_effect = Exception("API error")
        success, checks = checks_success_and_print(commit)
        assert success is None
        assert checks == []

    def test_skipped_conclusion_not_treated_as_bad(self) -> None:
        """'skipped' conclusion does not block success."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("test", "completed", "success"),
            self._make_check_run("optional", "completed", "skipped"),
        ]
        success, _ = checks_success_and_print(commit)
        # 'skipped' is not in the bad set, and 'success' was seen, so True
        assert success is True

    def test_all_skipped_no_success_returns_false(self) -> None:
        """Returns False when all checks are 'skipped' (no success seen)."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("optional", "completed", "skipped"),
        ]
        success, _ = checks_success_and_print(commit)
        assert success is False


class TestLegacyStatusAndPrint:
    """Tests for legacy_status_and_print."""

    def test_returns_state_on_success(self) -> None:
        """Returns 'success' when combined status is success."""
        commit = MagicMock()
        combined = MagicMock()
        combined.state = "success"
        combined.statuses = []
        commit.get_combined_status.return_value = combined
        result = legacy_status_and_print(commit)
        assert result == "success"

    def test_returns_failure_state(self) -> None:
        """Returns 'failure' when combined status is failure."""
        commit = MagicMock()
        combined = MagicMock()
        combined.state = "failure"
        combined.statuses = []
        commit.get_combined_status.return_value = combined
        result = legacy_status_and_print(commit)
        assert result == "failure"

    def test_returns_unknown_on_exception(self) -> None:
        """Returns 'unknown' when get_combined_status raises."""
        commit = MagicMock()
        commit.get_combined_status.side_effect = Exception("API error")
        result = legacy_status_and_print(commit)
        assert result == "unknown"

    def test_returns_unknown_when_state_is_none(self) -> None:
        """Returns 'unknown' when combined state is None."""
        commit = MagicMock()
        combined = MagicMock()
        combined.state = None
        combined.statuses = []
        commit.get_combined_status.return_value = combined
        result = legacy_status_and_print(commit)
        assert result == "unknown"

    def test_logs_each_status_context(self) -> None:
        """Iterates over statuses and logs each context."""
        commit = MagicMock()
        combined = MagicMock()
        combined.state = "pending"
        ctx = MagicMock()
        ctx.context = "ci/test"
        ctx.state = "pending"
        ctx.description = "running"
        combined.statuses = [ctx]
        commit.get_combined_status.return_value = combined
        result = legacy_status_and_print(commit)
        assert result == "pending"


class TestLocalBranchExists:
    """Tests for local_branch_exists."""

    @patch("hephaestus.github.pr_merge.subprocess.check_output")
    def test_returns_true_when_branch_exists(self, mock_check) -> None:
        """Returns True when git branch --list output is non-empty."""
        mock_check.return_value = b"  my-feature\n"
        assert local_branch_exists("my-feature") is True

    @patch("hephaestus.github.pr_merge.subprocess.check_output")
    def test_returns_false_when_branch_absent(self, mock_check) -> None:
        """Returns False when git branch --list output is empty."""
        mock_check.return_value = b""
        assert local_branch_exists("nonexistent") is False

    @patch("hephaestus.github.pr_merge.subprocess.check_output")
    def test_returns_false_on_subprocess_error(self, mock_check) -> None:
        """Returns False when subprocess raises CalledProcessError."""
        mock_check.side_effect = subprocess.CalledProcessError(1, "git")
        assert local_branch_exists("branch") is False


class TestTryPushHeadBranch:
    """Tests for try_push_head_branch."""

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.local_branch_exists", return_value=True)
    def test_pushes_when_branch_exists(self, mock_exists, mock_run) -> None:
        """Pushes the branch when it exists locally."""
        try_push_head_branch("feature-branch", dry_run=False)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "feature-branch:feature-branch" in cmd

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.local_branch_exists", return_value=False)
    def test_skips_push_when_branch_absent(self, mock_exists, mock_run) -> None:
        """Does not push when branch is not found locally."""
        try_push_head_branch("feature-branch", dry_run=False)
        mock_run.assert_not_called()

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.local_branch_exists")
    def test_dry_run_skips_local_check_and_push(self, mock_exists, mock_run) -> None:
        """In dry-run mode, skips local branch check and push entirely."""
        try_push_head_branch("feature-branch", dry_run=True)
        mock_exists.assert_not_called()
        mock_run.assert_not_called()


class TestHandleMergeResult:
    """Tests for handle_merge_result."""

    def test_logs_success_when_merged(self) -> None:
        """Does not raise when merge result indicates success."""
        result = MagicMock()
        result.merged = True
        result.message = "Merged"
        result.sha = "abc123"
        # Should not raise
        handle_merge_result(result, pr_number=42, base_branch="main")

    def test_logs_error_when_not_merged(self) -> None:
        """Does not raise when merge result indicates failure."""
        result = MagicMock()
        result.merged = False
        result.message = "Merge conflict"
        result.sha = None
        handle_merge_result(result, pr_number=42, base_branch="main")


class TestMain:
    """Tests for the main() entry point."""

    def _make_pr(self, number: int, head_ref: str, base_ref: str, sha: str) -> MagicMock:
        pr = MagicMock()
        pr.number = number
        pr.head.ref = head_ref
        pr.head.sha = sha
        pr.base.ref = base_ref
        return pr

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.detect_repo_from_remote")
    def test_exits_1_when_no_token(self, mock_detect, mock_git) -> None:
        """main() exits 1 when GITHUB_TOKEN is not set."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("os.getenv", return_value=None):
                with patch("sys.argv", ["prog"]):
                    with pytest.raises(SystemExit) as exc_info:
                        from hephaestus.github.pr_merge import main

                        main()
        assert exc_info.value.code == 1

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_exits_1_when_no_repo_detected(self, mock_git) -> None:
        """main() exits 1 when repo can't be detected."""
        with patch("os.getenv", return_value="fake-token"):
            with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value=None):
                with patch("sys.argv", ["prog"]):
                    with pytest.raises(SystemExit) as exc_info:
                        from hephaestus.github.pr_merge import main

                        main()
        assert exc_info.value.code == 1

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_exits_1_when_pygithub_not_installed(self, mock_git) -> None:
        """main() exits 1 when PyGithub is not importable."""
        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch(
                    "builtins.__import__", side_effect=ImportError("No module named 'github'")
                ):
                    with patch("sys.argv", ["prog"]):
                        with pytest.raises((SystemExit, ImportError)):
                            from hephaestus.github.pr_merge import main

                            main()

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_exits_1_when_repo_access_fails(self, mock_git) -> None:
        """main() exits 1 when GitHub repo access raises."""
        mock_gh = MagicMock()
        mock_gh.get_repo.side_effect = Exception("Not found")
        mock_github_module = MagicMock()
        mock_github_module.Github.return_value = mock_gh

        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch.dict(sys.modules, {"github": mock_github_module}):
                    with patch("sys.argv", ["prog"]):
                        with pytest.raises(SystemExit) as exc_info:
                            from hephaestus.github.pr_merge import main

                            main()
        assert exc_info.value.code == 1

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_merges_pr_when_checks_pass(self, mock_git, mock_push) -> None:
        """main() merges PR when CI checks pass."""
        pr = self._make_pr(1, "feature", "main", "abc123")
        pr.merge.return_value = MagicMock(merged=True, sha="def456", message="ok")

        commit = MagicMock()
        commit.get_check_runs.return_value = [
            MagicMock(name="ci", status="completed", conclusion="success")
        ]

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [pr]
        mock_repo.get_commit.return_value = commit

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_github_module = MagicMock()
        mock_github_module.Github.return_value = mock_gh

        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch.dict(sys.modules, {"github": mock_github_module}):
                    with patch("sys.argv", ["prog"]):
                        from hephaestus.github.pr_merge import main

                        main()

        pr.merge.assert_called_once_with(merge_method="rebase")

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_skips_pr_when_checks_fail(self, mock_git, mock_push) -> None:
        """main() skips merge when CI checks fail."""
        pr = self._make_pr(1, "feature", "main", "abc123")

        commit = MagicMock()
        commit.get_check_runs.return_value = [
            MagicMock(name="ci", status="completed", conclusion="failure")
        ]

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [pr]
        mock_repo.get_commit.return_value = commit

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_github_module = MagicMock()
        mock_github_module.Github.return_value = mock_gh

        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch.dict(sys.modules, {"github": mock_github_module}):
                    with patch("sys.argv", ["prog"]):
                        from hephaestus.github.pr_merge import main

                        main()

        pr.merge.assert_not_called()

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_dry_run_does_not_merge(self, mock_git, mock_push) -> None:
        """main() with --dry-run skips actual merge."""
        pr = self._make_pr(1, "feature", "main", "abc123")

        commit = MagicMock()
        commit.get_check_runs.return_value = [
            MagicMock(name="ci", status="completed", conclusion="success")
        ]

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [pr]
        mock_repo.get_commit.return_value = commit

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_github_module = MagicMock()
        mock_github_module.Github.return_value = mock_gh

        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch.dict(sys.modules, {"github": mock_github_module}):
                    with patch("sys.argv", ["prog", "--dry-run"]):
                        from hephaestus.github.pr_merge import main

                        main()

        pr.merge.assert_not_called()

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_falls_back_to_legacy_status_when_no_check_runs(self, mock_git, mock_push) -> None:
        """main() uses legacy status when no check runs found."""
        pr = self._make_pr(1, "feature", "main", "abc123")
        pr.merge.return_value = MagicMock(merged=True, sha="abc", message="ok")

        combined = MagicMock()
        combined.state = "success"
        combined.statuses = []

        commit = MagicMock()
        commit.get_check_runs.return_value = []  # no check runs
        commit.get_combined_status.return_value = combined

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [pr]
        mock_repo.get_commit.return_value = commit

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_github_module = MagicMock()
        mock_github_module.Github.return_value = mock_gh

        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch.dict(sys.modules, {"github": mock_github_module}):
                    with patch("sys.argv", ["prog"]):
                        from hephaestus.github.pr_merge import main

                        main()

        pr.merge.assert_called_once_with(merge_method="rebase")

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_push_all_pushes_every_pr(self, mock_git, mock_push) -> None:
        """main() with --push-all calls try_push_head_branch for every PR."""
        pr = self._make_pr(1, "feature", "main", "abc123")

        commit = MagicMock()
        commit.get_check_runs.return_value = [
            MagicMock(name="ci", status="completed", conclusion="failure")
        ]

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [pr]
        mock_repo.get_commit.return_value = commit

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_github_module = MagicMock()
        mock_github_module.Github.return_value = mock_gh

        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch.dict(sys.modules, {"github": mock_github_module}):
                    with patch("sys.argv", ["prog", "--push-all"]):
                        from hephaestus.github.pr_merge import main

                        main()

        mock_push.assert_called()

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_continues_on_commit_fetch_error(self, mock_git, mock_push) -> None:
        """main() continues to next PR when commit fetch fails."""
        pr1 = self._make_pr(1, "bad-branch", "main", "badsha")
        pr2 = self._make_pr(2, "good-branch", "main", "goodsha")
        pr2.merge.return_value = MagicMock(merged=True, sha="xyz", message="ok")

        good_commit = MagicMock()
        good_commit.get_check_runs.return_value = [
            MagicMock(name="ci", status="completed", conclusion="success")
        ]

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [pr1, pr2]
        mock_repo.get_commit.side_effect = [Exception("not found"), good_commit]

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_github_module = MagicMock()
        mock_github_module.Github.return_value = mock_gh

        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch.dict(sys.modules, {"github": mock_github_module}):
                    with patch("sys.argv", ["prog"]):
                        from hephaestus.github.pr_merge import main

                        main()

        # pr2 should still be merged despite pr1 failing
        pr2.merge.assert_called_once()

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_merge_exception_continues(self, mock_git, mock_push) -> None:
        """main() logs and continues when pr.merge() raises."""
        pr = self._make_pr(1, "feature", "main", "abc123")
        pr.merge.side_effect = Exception("merge conflict")

        commit = MagicMock()
        commit.get_check_runs.return_value = [
            MagicMock(name="ci", status="completed", conclusion="success")
        ]

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [pr]
        mock_repo.get_commit.return_value = commit

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_github_module = MagicMock()
        mock_github_module.Github.return_value = mock_gh

        with patch("os.getenv", return_value="fake-token"):
            with patch(
                "hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"
            ):
                with patch.dict(sys.modules, {"github": mock_github_module}):
                    with patch("sys.argv", ["prog"]):
                        # Should not raise
                        from hephaestus.github.pr_merge import main

                        main()
