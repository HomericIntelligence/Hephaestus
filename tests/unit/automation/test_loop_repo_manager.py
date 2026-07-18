"""Unit tests for hephaestus.automation.loop_repo_manager pure-function helpers.

Focuses on the parsing/logic helpers that do NOT require a live gh/git CLI.
Live-CLI functions (_gh_list_repos, _list_open_issue_numbers, etc.) are
covered by the existing tests in test_loop_runner.py which patch at the
loop_runner namespace (preserved via explicit re-exports).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import loop_repo_manager
from hephaestus.automation.loop_repo_manager import (
    _count_open_issues,
    _detect_cwd_repo,
    _detect_remote_base_ref,
    _ensure_clone,
    _local_ahead_count,
    _resolve_repo_dir,
    _sort_repos_by_open_count,
)
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT


class TestListOpenPrMeta:
    """Tests for open pull-request metadata discovery."""

    def test_returns_sorted_author_metadata_from_paginated_rows(self) -> None:
        pages = [
            [{"number": 9, "user": {"login": "depbot", "type": "Bot"}}],
            [{"number": 3, "user": {"login": "alice", "type": "User"}}],
        ]
        with patch(
            "hephaestus.automation.loop_repo_manager.gh_call",
            return_value=MagicMock(stdout=json.dumps(pages)),
        ) as mock_gh:
            result = loop_repo_manager._list_open_pr_meta("acme", "widget")

        assert result == [
            {"number": 3, "user": {"login": "alice", "type": "User"}},
            {"number": 9, "user": {"login": "depbot", "type": "Bot"}},
        ]
        assert mock_gh.call_args.args[0] == [
            "api",
            "/repos/acme/widget/pulls?state=open&per_page=100",
            "--paginate",
            "--slurp",
        ]
        assert mock_gh.call_args.kwargs["timeout"] == NETWORK_TIMEOUT

    def test_normalizes_malformed_user_metadata(self) -> None:
        pages = [[{"number": 7, "user": "unexpected"}, {"number": 8, "user": None}]]
        with patch(
            "hephaestus.automation.loop_repo_manager.gh_call",
            return_value=MagicMock(stdout=json.dumps(pages)),
        ):
            result = loop_repo_manager._list_open_pr_meta("acme", "widget")

        assert result == [
            {"number": 7, "user": {"login": None, "type": None}},
            {"number": 8, "user": {"login": None, "type": None}},
        ]

    @pytest.mark.parametrize("stdout", ["not-json", "{}"])
    def test_rejects_malformed_response(self, stdout: str) -> None:
        with (
            patch(
                "hephaestus.automation.loop_repo_manager.gh_call",
                return_value=MagicMock(stdout=stdout),
            ),
            pytest.raises(RuntimeError, match="failed to list open PRs"),
        ):
            loop_repo_manager._list_open_pr_meta("acme", "widget")


class TestDetectCwdRepo:
    """Tests for _detect_cwd_repo URL parsing logic."""

    def test_returns_none_tuple_when_not_in_git_repo(self) -> None:
        with patch(
            "hephaestus.automation.loop_repo_manager.subprocess.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            result = _detect_cwd_repo()
        assert result == (None, None)

    def test_parses_https_url(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/MyRepo\n"
            else:
                m.stdout = "https://github.com/MyOrg/MyRepo.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org == "MyOrg"
        assert repo == "MyRepo"

    def test_git_probes_use_metadata_timeout(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/MyRepo\n"
            else:
                m.stdout = "https://github.com/MyOrg/MyRepo.git\n"
            return m

        with patch(
            "hephaestus.automation.loop_repo_manager.subprocess.run",
            side_effect=fake_run,
        ) as mock_run:
            _detect_cwd_repo()

        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            assert call.kwargs["timeout"] == METADATA_TIMEOUT

    def test_parses_repo_name_from_https_remote_not_worktree_dir(self) -> None:
        """GitHub remote path is authoritative when worktree dir is issue-named."""

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/Hephaestus/build/.worktrees/issue-1442\n"
            else:
                m.stdout = "https://github.com/HomericIntelligence/Hephaestus.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org == "HomericIntelligence"
        assert repo == "Hephaestus"

    def test_parses_ssh_scp_url(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/ProjectFoo\n"
            else:
                m.stdout = "git@github.com:MyOrg/ProjectFoo.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org == "MyOrg"
        assert repo == "ProjectFoo"

    def test_parses_repo_name_from_ssh_remote_not_worktree_dir(self) -> None:
        """SCP-style GitHub remotes also override local worktree basenames."""

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/Hephaestus/build/.worktrees/issue-1442\n"
            else:
                m.stdout = "git@github.com:HomericIntelligence/Hephaestus.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org == "HomericIntelligence"
        assert repo == "Hephaestus"

    def test_returns_none_org_for_non_github_remote(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/SomeRepo\n"
            else:
                m.stdout = "https://gitlab.com/org/repo.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org is None
        assert repo == "SomeRepo"

    def test_returns_none_org_when_remote_url_fetch_fails(self) -> None:
        import subprocess

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            if "rev-parse" in cmd:
                m = MagicMock()
                m.stdout = "/home/user/repos/SomeRepo\n"
                return m
            raise subprocess.CalledProcessError(128, cmd)

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org is None
        assert repo == "SomeRepo"


class TestResolveRepoDir:
    """Tests for _resolve_repo_dir."""

    def test_returns_projects_dir_slash_repo(self, tmp_path: Path) -> None:
        result = _resolve_repo_dir(tmp_path, "MyRepo")
        assert result == tmp_path / "MyRepo"

    def test_does_not_create_directory(self, tmp_path: Path) -> None:
        result = _resolve_repo_dir(tmp_path, "NonExistent")
        assert not result.exists()


class TestDetectRemoteBaseRef:
    """Tests for _detect_remote_base_ref."""

    def test_returns_symbolic_ref_when_available(self, tmp_path: Path) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "symbolic-ref" in cmd:
                m.returncode = 0
                m.stdout = "origin/main\n"
            else:
                m.returncode = 0
                m.stdout = ""
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            ref = _detect_remote_base_ref("MyRepo", tmp_path)
        assert ref == "origin/main"

    def test_falls_back_to_origin_main_when_symbolic_ref_fails(self, tmp_path: Path) -> None:
        call_count = [0]

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "symbolic-ref" in cmd:
                m.returncode = 1
                m.stdout = ""
            elif "rev-parse" in cmd and "origin/main" in cmd:
                call_count[0] += 1
                m.returncode = 0
                m.stdout = "abc1234\n"
            else:
                m.returncode = 1
                m.stdout = ""
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            ref = _detect_remote_base_ref("MyRepo", tmp_path)
        assert ref == "origin/main"

    def test_falls_back_to_hardcoded_when_all_fail(self, tmp_path: Path) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            m.returncode = 1
            m.stdout = ""
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            ref = _detect_remote_base_ref("MyRepo", tmp_path)
        assert ref == "origin/main"


class TestLocalAheadCount:
    """Tests for _local_ahead_count."""

    def test_returns_count_when_ahead(self, tmp_path: Path) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "3\n"

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", return_value=m):
            count = _local_ahead_count("MyRepo", tmp_path, "origin/main")
        assert count == 3

    def test_returns_zero_on_timeout(self, tmp_path: Path) -> None:
        import subprocess

        with patch(
            "hephaestus.automation.loop_repo_manager.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 30),
        ):
            count = _local_ahead_count("MyRepo", tmp_path, "origin/main")
        assert count == 0

    def test_returns_zero_on_nonzero_rc(self, tmp_path: Path) -> None:
        m = MagicMock()
        m.returncode = 128
        m.stdout = ""

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", return_value=m):
            count = _local_ahead_count("MyRepo", tmp_path, "origin/main")
        assert count == 0

    def test_returns_zero_on_empty_stdout(self, tmp_path: Path) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", return_value=m):
            count = _local_ahead_count("MyRepo", tmp_path, "origin/main")
        assert count == 0


class TestRebaseMain:
    """Tests for loop repo preparation rebase behavior."""

    def test_local_ahead_rebase_resigns_and_signs_off_commits(self, tmp_path: Path) -> None:
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            del kwargs
            commands.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            if "symbolic-ref" in cmd:
                m.stdout = "origin/main\n"
            elif "rev-list" in cmd:
                m.stdout = "2\n"
            elif "rev-parse" in cmd:
                m.stdout = "abc1234\n"
            return m

        fetch_result = MagicMock(returncode=0, stderr="")
        with (
            patch(
                "hephaestus.automation.loop_repo_manager.resilient_call", return_value=fetch_result
            ),
            patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run),
        ):
            sha, fetch_ok = loop_repo_manager._rebase_main("MyRepo", tmp_path)

        assert (sha, fetch_ok) == ("abc1234", True)
        assert [
            "git",
            "-C",
            str(tmp_path),
            "rebase",
            "--empty=drop",
            "origin/main",
            "--exec",
            "git commit --amend --no-edit -S -s --allow-empty",
            "--quiet",
        ] in commands


class TestEnsureClone:
    """Tests for _ensure_clone."""

    def test_skips_clone_when_git_dir_exists(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
            _ensure_clone("MyOrg", "MyRepo", tmp_path)
        mock_gh_call.assert_not_called()

    def test_raises_on_failed_clone(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
            mock_gh_call.side_effect = subprocess.CalledProcessError(1, ["gh"])
            with pytest.raises(RuntimeError, match=r"gh repo clone.*failed"):
                _ensure_clone("MyOrg", "MyRepo", tmp_path)

    def test_succeeds_on_successful_clone(self, tmp_path: Path) -> None:
        m = MagicMock()
        m.returncode = 0

        with patch("hephaestus.automation.loop_repo_manager.gh_call", return_value=m):
            _ensure_clone("MyOrg", "MyRepo", tmp_path)


class TestListOpenIssueNumbersEpicTagging:
    """Epic skip-tagging must target the owning repo, never the ambient cwd (#2245)."""

    def test_skip_epics_receives_owning_repo(self) -> None:
        """The (org, repo) being discovered is threaded to the label write."""
        meta = [
            {"number": 81, "title": "Epic: roadmap", "labels": ["epic"]},
            {"number": 82, "title": "Fix bug", "labels": ["bug"]},
        ]
        with (
            patch.object(loop_repo_manager, "_list_open_issue_meta", return_value=meta),
            patch.object(loop_repo_manager, "skip_epics") as mock_skip,
        ):
            kept = loop_repo_manager._list_open_issue_numbers("MyOrg", "Proteus")
        assert kept == [82]
        mock_skip.assert_called_once_with({81: ["epic"]}, repo=("MyOrg", "Proteus"))

    def test_no_epics_means_no_label_write(self) -> None:
        meta = [{"number": 82, "title": "Fix bug", "labels": ["bug"]}]
        with (
            patch.object(loop_repo_manager, "_list_open_issue_meta", return_value=meta),
            patch.object(loop_repo_manager, "skip_epics") as mock_skip,
        ):
            kept = loop_repo_manager._list_open_issue_numbers("MyOrg", "Proteus")
        assert kept == [82]
        mock_skip.assert_not_called()


class TestCountOpenIssues:
    """Tests for _count_open_issues (delegates to _list_open_issue_numbers)."""

    def test_returns_count_of_issue_numbers(self) -> None:
        with patch.object(loop_repo_manager, "_list_open_issue_numbers", return_value=[1, 2, 3]):
            count = _count_open_issues("MyOrg", "MyRepo")
        assert count == 3

    def test_returns_zero_on_empty_list(self) -> None:
        with patch.object(loop_repo_manager, "_list_open_issue_numbers", return_value=[]):
            count = _count_open_issues("MyOrg", "MyRepo")
        assert count == 0


class TestSortReposByOpenCount:
    """Tests for _sort_repos_by_open_count."""

    def test_sorts_ascending_by_issue_count(self) -> None:
        counts = {"alpha": 5, "beta": 1, "gamma": 3}

        def fake_count(org: str, repo: str) -> int:
            return counts[repo]

        with patch.object(loop_repo_manager, "_count_open_issues", side_effect=fake_count):
            result = _sort_repos_by_open_count("MyOrg", ["alpha", "beta", "gamma"])
        assert result == ["beta", "gamma", "alpha"]

    def test_preserves_stable_order_on_equal_counts(self) -> None:
        def fake_count(org: str, repo: str) -> int:
            return 0

        with patch.object(loop_repo_manager, "_count_open_issues", side_effect=fake_count):
            result = _sort_repos_by_open_count("MyOrg", ["alpha", "beta", "gamma"])
        assert result == ["alpha", "beta", "gamma"]


class TestCountFailingPrs:
    """Tests for _count_failing_prs gate function.

    Re-homed from the deleted test_drive_green_pr_discovery.py: _count_failing_prs
    lives in loop_repo_manager, so its coverage belongs here rather than behind a
    loop_runner re-export that no longer exists.
    """

    def test_count_failing_prs_counts_open_non_draft_prs_without_check_data(self) -> None:
        """Open non-draft PRs are counted without consulting CI/CD data."""
        import json

        from hephaestus.automation.loop_repo_manager import _count_failing_prs

        mock_output = [
            {
                "number": 1,
                "isDraft": False,
                "state": "OPEN",
            },
            {
                "number": 2,
                "isDraft": False,
                "state": "OPEN",
            },
            {
                "number": 3,
                "isDraft": False,
                "state": "CLOSED",
            },
        ]
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(mock_output))
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 2

    def test_count_failing_prs_excludes_draft_prs(self) -> None:
        """Draft PRs are excluded from the count."""
        import json

        from hephaestus.automation.loop_repo_manager import _count_failing_prs

        mock_output = [
            {
                "number": 1,
                "isDraft": True,
                "state": "OPEN",
            },
        ]
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(mock_output))
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 0

    def test_count_failing_prs_returns_zero_on_gh_error(self) -> None:
        """Returns 0 on gh command failure (fail-closed)."""
        from hephaestus.automation.loop_repo_manager import _count_failing_prs

        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.side_effect = subprocess.CalledProcessError(1, ["gh"])
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 0

    def test_count_failing_prs_returns_zero_on_timeout(self) -> None:
        """Returns 0 on timeout (fail-closed)."""
        from hephaestus.automation.loop_repo_manager import _count_failing_prs

        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 0

    def test_count_failing_prs_returns_zero_on_invalid_json(self) -> None:
        """Returns 0 on invalid JSON (fail-closed)."""
        from hephaestus.automation.loop_repo_manager import _count_failing_prs

        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="not-json")
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 0

    def test_count_failing_prs_uses_network_timeout(self) -> None:
        """PR discovery gh call uses the shared network timeout."""
        from hephaestus.automation.loop_repo_manager import _count_failing_prs
        from hephaestus.utils.helpers import NETWORK_TIMEOUT

        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="[]")
            _count_failing_prs("MyOrg", "MyRepo")

        assert mock_gh.call_args.kwargs["timeout"] == NETWORK_TIMEOUT

    def test_count_failing_prs_logs_warning_on_limit_hit(self) -> None:
        """A warning is logged when the 1000-PR cap is hit."""
        import json

        from hephaestus.automation.loop_repo_manager import _count_failing_prs

        mock_output = [
            {
                "number": i,
                "isDraft": False,
                "state": "OPEN",
            }
            for i in range(1, 1001)
        ]
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(mock_output))
            with patch("hephaestus.automation.loop_repo_manager.LOG") as mock_logger:
                result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 1000
        mock_logger.warning.assert_called()
