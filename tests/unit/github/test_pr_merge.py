#!/usr/bin/env python3
"""Tests for hephaestus.github.pr_merge module."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from hephaestus.github import pr_merge as pr_merge_module
from hephaestus.github.pr_merge import (
    checks_success_and_log,
    detect_repo_from_remote,
    handle_merge_result,
    legacy_status_and_log,
    local_branch_exists,
    run_git_cmd,
    try_push_head_branch,
)


def _gh_result(payload: object) -> MagicMock:
    """Build a gh_call result carrying JSON stdout."""
    result = MagicMock()
    result.stdout = json.dumps(payload)
    result.stderr = ""
    result.returncode = 0
    return result


class TestDetectRepoFromRemote:
    """Tests for detect_repo_from_remote."""

    @patch("hephaestus.github.pr_merge.git_remote_url")
    def test_detects_ssh_url(self, mock_remote_url) -> None:
        """Parses SSH-style github.com:owner/repo.git."""
        mock_remote_url.return_value = "git@github.com:HomericIntelligence/Hephaestus.git"
        result = detect_repo_from_remote()
        assert result == "HomericIntelligence/Hephaestus"

    @patch("hephaestus.github.pr_merge.git_remote_url")
    def test_detects_https_url(self, mock_remote_url) -> None:
        """Parses HTTPS github.com/owner/repo.git."""
        mock_remote_url.return_value = "https://github.com/HomericIntelligence/Scylla.git"
        result = detect_repo_from_remote()
        assert result == "HomericIntelligence/Scylla"

    @patch("hephaestus.github.pr_merge.git_remote_url")
    def test_detects_url_without_git_suffix(self, mock_remote_url) -> None:
        """Parses URL without .git suffix."""
        mock_remote_url.return_value = "https://github.com/owner/repo"
        result = detect_repo_from_remote()
        assert result == "owner/repo"

    @patch("hephaestus.github.pr_merge.git_remote_url")
    def test_returns_none_for_non_github_url(self, mock_remote_url) -> None:
        """Returns None when URL is not a GitHub URL."""
        mock_remote_url.return_value = "https://gitlab.com/owner/repo.git"
        result = detect_repo_from_remote()
        assert result is None

    @patch("hephaestus.github.pr_merge.git_remote_url")
    def test_returns_none_on_exception(self, mock_remote_url) -> None:
        """Returns None when git remote lookup raises."""
        mock_remote_url.side_effect = RuntimeError("git not found")
        result = detect_repo_from_remote()
        assert result is None


class TestRunGitCmd:
    """Tests for run_git_cmd."""

    @patch("hephaestus.github.pr_merge.run_git")
    def test_calls_run_git(self, mock_run) -> None:
        """Passes command to the shared git helper."""
        run_git_cmd(["git", "status"])
        mock_run.assert_called_once_with(["git", "status"], cwd=None, dry_run=False)

    @patch("hephaestus.github.pr_merge.run_git")
    def test_dry_run_passed_through(self, mock_run) -> None:
        """dry_run flag is forwarded to the shared git helper."""
        run_git_cmd(["git", "push"], dry_run=True)
        mock_run.assert_called_once_with(["git", "push"], cwd=None, dry_run=True)

    @patch("hephaestus.github.pr_merge.run_git")
    def test_cwd_passed_through(self, mock_run) -> None:
        """Cwd is forwarded to the shared git helper."""
        run_git_cmd(["git", "log"], cwd="/some/path")
        mock_run.assert_called_once_with(["git", "log"], cwd="/some/path", dry_run=False)

    @patch("hephaestus.github.pr_merge.logger")
    @patch("hephaestus.github.pr_merge.run_git")
    def test_log_includes_cwd_when_provided(self, _mock_run, mock_logger) -> None:
        """Log line includes (cwd=...) when cwd is passed."""
        run_git_cmd(["git", "log"], cwd="/some/path")
        mock_logger.info.assert_called_once_with("$ %s (cwd=%s)", "git log", "/some/path")

    @patch("hephaestus.github.pr_merge.logger")
    @patch("hephaestus.github.pr_merge.run_git")
    def test_log_omits_cwd_when_not_provided(self, _mock_run, mock_logger) -> None:
        """Log line omits cwd suffix when cwd is None (no noise added to existing traces)."""
        run_git_cmd(["git", "status"])
        mock_logger.info.assert_called_once_with("$ %s", "git status")


class TestChecksSuccessAndPrint:
    """Tests for checks_success_and_log."""

    def _make_check_run(self, name: str, status: str, conclusion: str | None) -> MagicMock:
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
        success, checks = checks_success_and_log(commit)
        assert success is True
        assert len(checks) == 2

    def test_failure_conclusion_returns_false(self) -> None:
        """Returns (False, checks) when any check has a bad conclusion."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("test", "completed", "success"),
            self._make_check_run("ci", "completed", "failure"),
        ]
        success, _checks = checks_success_and_log(commit)
        assert success is False

    def test_in_progress_check_returns_false(self) -> None:
        """Returns (False, checks) when a check is not completed."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("test", "in_progress", None),
        ]
        success, _checks = checks_success_and_log(commit)
        assert success is False

    def test_no_checks_returns_none(self) -> None:
        """Returns (None, []) when there are no check runs."""
        commit = MagicMock()
        commit.get_check_runs.return_value = []
        success, checks = checks_success_and_log(commit)
        assert success is None
        assert checks == []

    def test_exception_returns_none(self) -> None:
        """Returns (None, []) when get_check_runs raises."""
        commit = MagicMock()
        commit.get_check_runs.side_effect = Exception("API error")
        success, checks = checks_success_and_log(commit)
        assert success is None
        assert checks == []

    def test_skipped_conclusion_not_treated_as_bad(self) -> None:
        """'skipped' conclusion does not block success."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("test", "completed", "success"),
            self._make_check_run("optional", "completed", "skipped"),
        ]
        success, _ = checks_success_and_log(commit)
        # 'skipped' is not in the bad set, and 'success' was seen, so True
        assert success is True

    def test_all_skipped_no_success_returns_false(self) -> None:
        """Returns False when all checks are 'skipped' (no success seen)."""
        commit = MagicMock()
        commit.get_check_runs.return_value = [
            self._make_check_run("optional", "completed", "skipped"),
        ]
        success, _ = checks_success_and_log(commit)
        assert success is False


class TestLegacyStatusAndPrint:
    """Tests for legacy_status_and_log."""

    def test_returns_state_on_success(self) -> None:
        """Returns 'success' when combined status is success."""
        commit = MagicMock()
        combined = MagicMock()
        combined.state = "success"
        combined.statuses = []
        commit.get_combined_status.return_value = combined
        result = legacy_status_and_log(commit)
        assert result == "success"

    def test_returns_failure_state(self) -> None:
        """Returns 'failure' when combined status is failure."""
        commit = MagicMock()
        combined = MagicMock()
        combined.state = "failure"
        combined.statuses = []
        commit.get_combined_status.return_value = combined
        result = legacy_status_and_log(commit)
        assert result == "failure"

    def test_returns_unknown_on_exception(self) -> None:
        """Returns 'unknown' when get_combined_status raises."""
        commit = MagicMock()
        commit.get_combined_status.side_effect = Exception("API error")
        result = legacy_status_and_log(commit)
        assert result == "unknown"

    def test_returns_unknown_when_state_is_none(self) -> None:
        """Returns 'unknown' when combined state is None."""
        commit = MagicMock()
        combined = MagicMock()
        combined.state = None
        combined.statuses = []
        commit.get_combined_status.return_value = combined
        result = legacy_status_and_log(commit)
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
        result = legacy_status_and_log(commit)
        assert result == "pending"


class TestLocalBranchExists:
    """Tests for local_branch_exists."""

    @patch("hephaestus.github.pr_merge.git_branch_exists", return_value=True)
    def test_returns_true_when_branch_exists(self, mock_exists) -> None:
        """Returns True when the shared git helper finds the branch."""
        assert local_branch_exists("my-feature") is True
        mock_exists.assert_called_once_with("my-feature")

    @patch("hephaestus.github.pr_merge.git_branch_exists", return_value=False)
    def test_returns_false_when_branch_absent(self, mock_exists) -> None:
        """Returns False when the shared git helper does not find the branch."""
        assert local_branch_exists("nonexistent") is False
        mock_exists.assert_called_once_with("nonexistent")

    @patch("hephaestus.github.pr_merge.git_branch_exists")
    def test_returns_false_on_subprocess_error(self, mock_exists) -> None:
        """Returns False when git helper raises CalledProcessError."""
        mock_exists.side_effect = subprocess.CalledProcessError(1, "git")
        assert local_branch_exists("branch") is False

    @patch("hephaestus.github.pr_merge.git_branch_exists")
    def test_returns_false_on_timeout(self, mock_exists) -> None:
        """A hung branch lookup degrades to False instead of hanging (#684)."""
        mock_exists.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        assert local_branch_exists("branch") is False


class TestTryPushHeadBranch:
    """Tests for try_push_head_branch."""

    @patch("hephaestus.github.pr_merge.git_push")
    @patch("hephaestus.github.pr_merge.local_branch_exists", return_value=True)
    def test_pushes_when_branch_exists(self, mock_exists, mock_push) -> None:
        """Pushes the branch when it exists locally."""
        try_push_head_branch("feature-branch", dry_run=False)
        mock_push.assert_called_once_with(
            None,
            "origin",
            "feature-branch:feature-branch",
            dry_run=False,
        )

    @patch("hephaestus.github.pr_merge.git_push")
    @patch("hephaestus.github.pr_merge.local_branch_exists", return_value=False)
    def test_skips_push_when_branch_absent(self, mock_exists, mock_push) -> None:
        """Does not push when branch is not found locally."""
        try_push_head_branch("feature-branch", dry_run=False)
        mock_push.assert_not_called()

    @patch("hephaestus.github.pr_merge.git_push")
    @patch("hephaestus.github.pr_merge.local_branch_exists")
    def test_dry_run_skips_local_check_and_push(self, mock_exists, mock_push) -> None:
        """In dry-run mode, skips local branch check and push entirely."""
        try_push_head_branch("feature-branch", dry_run=True)
        mock_exists.assert_not_called()
        mock_push.assert_not_called()


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


class TestProcessPr:
    """Tests for extracted PR processing helpers."""

    def test_process_pr_pushes_and_merges_passed_pr(self, monkeypatch) -> None:
        """Successful PRs are pushed and delegated to the merge helper."""
        pushes: list[tuple[str, bool]] = []
        merges: list[tuple[str, int, str, str, bool]] = []

        monkeypatch.setattr(pr_merge_module, "_checks_pass_or_legacy", lambda *args: True)
        monkeypatch.setattr(
            pr_merge_module,
            "try_push_head_branch",
            lambda branch, dry_run: pushes.append((branch, dry_run)),
        )
        monkeypatch.setattr(
            pr_merge_module,
            "_attempt_pr_merge",
            lambda repo, number, sha, base, dry_run: merges.append(
                (repo, number, sha, base, dry_run)
            ),
        )

        pr_merge_module._process_pr(
            "owner/repo",
            {
                "number": 1,
                "headRefName": "feature",
                "headRefOid": "abc123",
                "baseRefName": "main",
            },
            push_all=False,
            dry_run=False,
        )

        assert pushes == [("feature", False)]
        assert merges == [("owner/repo", 1, "abc123", "main", False)]

    def test_process_pr_missing_head_sha_skips_checks_and_merge(self, monkeypatch) -> None:
        """PRs without a head SHA skip check and merge work."""
        check = MagicMock()
        merge = MagicMock()
        monkeypatch.setattr(pr_merge_module, "_checks_pass_or_legacy", check)
        monkeypatch.setattr(pr_merge_module, "_attempt_pr_merge", merge)

        pr_merge_module._process_pr(
            "owner/repo",
            {"number": 1, "headRefName": "feature", "baseRefName": "main"},
            push_all=False,
            dry_run=False,
        )

        check.assert_not_called()
        merge.assert_not_called()


class TestMain:
    """Tests for the main() entry point."""

    def _make_pr(self, number: int = 1, bucket: str = "pass") -> list[MagicMock]:
        """Return gh_call side effects for repo view, PR list, checks, and merge."""
        return [
            _gh_result({"nameWithOwner": "owner/repo"}),
            _gh_result(
                [
                    {
                        "number": number,
                        "headRefName": "feature",
                        "headRefOid": "abc123",
                        "baseRefName": "main",
                    }
                ]
            ),
            _gh_result([{"name": "ci", "state": "SUCCESS", "bucket": bucket, "workflow": "CI"}]),
            _gh_result({"merged": True, "sha": "def456", "message": "ok"}),
        ]

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_returns_1_when_no_repo_detected(self, _mock_git) -> None:
        """main() returns 1 when repo can't be detected."""
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value=None):
            with patch("sys.argv", ["prog"]):
                from hephaestus.github.pr_merge import main

                assert main() == 1

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.gh_call")
    def test_returns_1_when_repo_access_fails(self, mock_gh_call, _mock_git) -> None:
        """main() returns 1 when gh cannot read the repository."""
        mock_gh_call.side_effect = subprocess.CalledProcessError(1, ["gh"], stderr="Not found")
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"):
            with patch("sys.argv", ["prog"]):
                from hephaestus.github.pr_merge import main

                assert main() == 1

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.gh_call")
    def test_merges_pr_when_checks_pass(self, mock_gh_call, _mock_git, mock_push) -> None:
        """main() merges PR through gh_call when CI checks pass."""
        mock_gh_call.side_effect = self._make_pr()
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"):
            with patch("sys.argv", ["prog"]):
                from hephaestus.github.pr_merge import main

                assert main() == 0

        merge_args = mock_gh_call.call_args_list[-1].args[0]
        assert merge_args[:4] == ["api", "-X", "PUT", "/repos/owner/repo/pulls/1/merge"]
        assert "merge_method=squash" in merge_args
        assert "sha=abc123" in merge_args
        mock_push.assert_called_once_with("feature", False)

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.gh_call")
    def test_skips_pr_when_checks_fail(self, mock_gh_call, _mock_git, mock_push) -> None:
        """main() skips merge when CI checks fail."""
        mock_gh_call.side_effect = self._make_pr(bucket="fail")
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"):
            with patch("sys.argv", ["prog"]):
                from hephaestus.github.pr_merge import main

                assert main() == 0

        assert len(mock_gh_call.call_args_list) == 3
        mock_push.assert_not_called()

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.gh_call")
    def test_dry_run_does_not_merge(self, mock_gh_call, _mock_git, mock_push) -> None:
        """main() with --dry-run skips actual merge."""
        mock_gh_call.side_effect = self._make_pr()
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"):
            with patch("sys.argv", ["prog", "--dry-run"]):
                from hephaestus.github.pr_merge import main

                assert main() == 0

        assert len(mock_gh_call.call_args_list) == 3
        mock_push.assert_not_called()

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.gh_call")
    def test_falls_back_to_legacy_status_when_no_check_runs(
        self, mock_gh_call, _mock_git, _mock_push
    ) -> None:
        """main() uses legacy status when no check runs found."""
        mock_gh_call.side_effect = [
            _gh_result({"nameWithOwner": "owner/repo"}),
            _gh_result(
                [
                    {
                        "number": 1,
                        "headRefName": "feature",
                        "headRefOid": "abc123",
                        "baseRefName": "main",
                    }
                ]
            ),
            subprocess.CalledProcessError(
                1, ["gh", "pr", "checks"], stderr="no checks reported on branch"
            ),
            _gh_result({"state": "success", "statuses": []}),
            _gh_result({"merged": True, "sha": "def456", "message": "ok"}),
        ]
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"):
            with patch("sys.argv", ["prog"]):
                from hephaestus.github.pr_merge import main

                assert main() == 0

        legacy_status_args = mock_gh_call.call_args_list[3].args[0]
        assert legacy_status_args == ["api", "/repos/owner/repo/commits/abc123/status"]
        assert mock_gh_call.call_args_list[-1].args[0][:4] == [
            "api",
            "-X",
            "PUT",
            "/repos/owner/repo/pulls/1/merge",
        ]

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.gh_call")
    def test_push_all_pushes_every_pr(self, mock_gh_call, _mock_git, mock_push) -> None:
        """main() with --push-all calls try_push_head_branch for every PR."""
        mock_gh_call.side_effect = self._make_pr(bucket="fail")
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"):
            with patch("sys.argv", ["prog", "--push-all"]):
                from hephaestus.github.pr_merge import main

                assert main() == 0

        mock_push.assert_called_once_with("feature", False)

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.gh_call")
    def test_continues_when_head_sha_missing(self, mock_gh_call, _mock_git, mock_push) -> None:
        """main() continues when a PR list row lacks a head SHA."""
        mock_gh_call.side_effect = [
            _gh_result({"nameWithOwner": "owner/repo"}),
            _gh_result(
                [
                    {"number": 1, "headRefName": "bad", "baseRefName": "main"},
                    {
                        "number": 2,
                        "headRefName": "good",
                        "headRefOid": "goodsha",
                        "baseRefName": "main",
                    },
                ]
            ),
            _gh_result([{"name": "ci", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}]),
            _gh_result({"merged": True, "sha": "merged", "message": "ok"}),
        ]
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"):
            with patch("sys.argv", ["prog"]):
                from hephaestus.github.pr_merge import main

                assert main() == 0
        mock_push.assert_called_once_with("good", False)

    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    @patch("hephaestus.github.pr_merge.gh_call")
    def test_merge_exception_continues_to_next_pr(self, mock_gh_call, _mock_git, mock_push) -> None:
        """main() logs a merge API exception and still processes later PRs."""
        mock_gh_call.side_effect = [
            _gh_result({"nameWithOwner": "owner/repo"}),
            _gh_result(
                [
                    {
                        "number": 1,
                        "headRefName": "feature-one",
                        "headRefOid": "sha-one",
                        "baseRefName": "main",
                    },
                    {
                        "number": 2,
                        "headRefName": "feature-two",
                        "headRefOid": "sha-two",
                        "baseRefName": "main",
                    },
                ]
            ),
            _gh_result([{"name": "ci", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}]),
            subprocess.CalledProcessError(1, ["gh"], stderr="merge conflict"),
            _gh_result([{"name": "ci", "state": "SUCCESS", "bucket": "pass", "workflow": "CI"}]),
            _gh_result({"merged": True, "sha": "merged-two", "message": "ok"}),
        ]
        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value="owner/repo"):
            with patch("sys.argv", ["prog"]):
                from hephaestus.github.pr_merge import main

                assert main() == 0

        merge_paths = [
            call.args[0][3]
            for call in mock_gh_call.call_args_list
            if call.args[0][:3] == ["api", "-X", "PUT"]
        ]
        assert merge_paths == [
            "/repos/owner/repo/pulls/1/merge",
            "/repos/owner/repo/pulls/2/merge",
        ]
        assert mock_push.call_count == 2


class TestMainJson:
    """Smoke tests covering --json branches of pr_merge.main()."""

    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_no_repo_json(self, _mock_git, capsys) -> None:
        from hephaestus.github.pr_merge import main

        with patch("hephaestus.github.pr_merge.detect_repo_from_remote", return_value=None):
            with patch("sys.argv", ["prog", "--json"]):
                assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"

    @patch("hephaestus.github.pr_merge.gh_call")
    @patch("hephaestus.github.pr_merge.try_push_head_branch")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_success_json(self, _mock_git, _mock_push, mock_gh_call, capsys) -> None:
        """Full happy-path with no PRs returns 0 and emits ok envelope."""
        mock_gh_call.side_effect = [_gh_result({"nameWithOwner": "owner/repo"}), _gh_result([])]
        with patch(
            "hephaestus.github.pr_merge.detect_repo_from_remote",
            return_value="owner/repo",
        ):
            with patch("sys.argv", ["prog", "--json"]):
                from hephaestus.github.pr_merge import main

                assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["exit_code"] == 0

    @patch("hephaestus.github.pr_merge.gh_call")
    @patch("hephaestus.github.pr_merge.run_git_cmd")
    def test_repo_access_failure_json(self, _mock_git, mock_gh_call, capsys) -> None:
        """When gh repo view fails, --json emits an error envelope."""
        mock_gh_call.side_effect = RuntimeError("403")
        with patch(
            "hephaestus.github.pr_merge.detect_repo_from_remote",
            return_value="owner/repo",
        ):
            with patch("sys.argv", ["prog", "--json"]):
                from hephaestus.github.pr_merge import main

                assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"


class TestSquashOnlyInvariant:
    """The HomericIntelligence repos disable rebase merges in branch protection.

    `merge_method=rebase` fails with "Rebase merges are not allowed
    on this repository". Lock the squash-only contract at the source level so a
    future edit cannot silently reintroduce a rebase merge path.
    """

    def test_no_rebase_merge_method_in_source(self) -> None:
        import inspect

        from hephaestus.github import pr_merge

        source = inspect.getsource(pr_merge)
        assert "merge_method=rebase" not in source
        assert "merge_method=squash" in source


class TestCanonicalGitOps:
    """Source-level regressions for git helper consolidation."""

    def test_pr_merge_uses_canonical_git_helpers(self) -> None:
        import inspect

        from hephaestus.github import git_ops, pr_merge

        source = inspect.getsource(pr_merge)
        assert vars(pr_merge)["run_git"] is git_ops.run_git
        assert vars(pr_merge)["git_remote_url"] is git_ops.git_remote_url
        assert vars(pr_merge)["git_branch_exists"] is git_ops.git_branch_exists
        assert vars(pr_merge)["git_push"] is git_ops.git_push
        assert "def run_subprocess(" not in source
        assert "subprocess.check_output" not in source
