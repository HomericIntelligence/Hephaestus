"""Unit tests for hephaestus.automation.loop_repo_manager pure-function helpers.

Focuses on the parsing/logic helpers that do NOT require a live gh/git CLI.
Live-CLI functions (_gh_list_repos, _list_open_issue_numbers, etc.) are
covered by the existing tests in test_loop_runner.py which patch at the
loop_runner namespace (preserved via explicit re-exports).
"""

from __future__ import annotations

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


class TestDetectCwdRepo:
    """Tests for _detect_cwd_repo URL parsing logic."""

    def test_returns_none_tuple_when_not_in_git_repo(self) -> None:
        import subprocess

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


class TestEnsureClone:
    """Tests for _ensure_clone."""

    def test_skips_clone_when_git_dir_exists(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as mock_run:
            _ensure_clone("MyOrg", "MyRepo", tmp_path)
        mock_run.assert_not_called()

    def test_raises_on_failed_clone(self, tmp_path: Path) -> None:
        m = MagicMock()
        m.returncode = 1

        with (
            patch("hephaestus.automation.loop_repo_manager.resilient_call", return_value=m),
        ):
            with pytest.raises(RuntimeError, match=r"gh repo clone.*failed"):
                _ensure_clone("MyOrg", "MyRepo", tmp_path)

    def test_succeeds_on_successful_clone(self, tmp_path: Path) -> None:
        m = MagicMock()
        m.returncode = 0

        with patch("hephaestus.automation.loop_repo_manager.resilient_call", return_value=m):
            _ensure_clone("MyOrg", "MyRepo", tmp_path)


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
