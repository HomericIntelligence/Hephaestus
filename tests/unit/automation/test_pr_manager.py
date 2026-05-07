"""Tests for hephaestus.automation.pr_manager.

Covers commit_changes filtering of secret files, ensure_pr_created
fallback paths, and create_pr argument shape — all via mocked subprocess
and GitHub-API calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import pr_manager


def _status(stdout: str = "", returncode: int = 0) -> MagicMock:
    return MagicMock(stdout=stdout, returncode=returncode)


class TestCommitChanges:
    """Tests for commit changes."""

    def test_no_changes_raises(self) -> None:
        with patch.object(pr_manager, "run", return_value=_status("")):
            with pytest.raises(RuntimeError, match="No changes to commit"):
                pr_manager.commit_changes(1, Path("/tmp/wt"))

    def test_only_secret_files_raises(self) -> None:
        porcelain = "?? .env\n?? id_rsa\n M secrets/foo.key\n"
        with patch.object(pr_manager, "run", return_value=_status(porcelain)):
            with pytest.raises(RuntimeError, match="All changes appear to be secret"):
                pr_manager.commit_changes(2, Path("/tmp/wt"))

    def test_filters_secrets_and_commits(self) -> None:
        porcelain = " M src/foo.py\n?? .env\n?? data.key\n M src/bar.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Add foo")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
        ):
            pr_manager.commit_changes(3, Path("/tmp/wt"))

        # git add must include the .py files but not .env or .key
        add_call = run_mock.call_args_list[1].args[0]
        assert "src/foo.py" in add_call
        assert "src/bar.py" in add_call
        assert ".env" not in add_call
        assert "data.key" not in add_call

    def test_handles_renamed_files(self) -> None:
        porcelain = "R  old.py -> new.py\n"
        run_mock = MagicMock(side_effect=[_status(porcelain), _status(""), _status("")])
        issue = MagicMock(title="Rename")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
        ):
            pr_manager.commit_changes(4, Path("/tmp/wt"))
        add_call = run_mock.call_args_list[1].args[0]
        assert "new.py" in add_call


class TestEnsurePRCreated:
    """Tests for ensure p r created."""

    def test_no_commit_raises(self) -> None:
        with patch.object(pr_manager, "run", return_value=_status("")):
            with pytest.raises(RuntimeError, match="No commit found"):
                pr_manager.ensure_pr_created(1, "branch", Path("/tmp/wt"))

    def test_returns_existing_pr(self) -> None:
        run_mock = MagicMock(
            side_effect=[
                _status("abc1234 commit msg"),  # git log
                _status("refs/heads/branch"),  # ls-remote (already pushed)
            ]
        )
        gh_mock = _status('[{"number": 99}]')
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "_gh_call", return_value=gh_mock),
        ):
            assert pr_manager.ensure_pr_created(1, "branch", Path("/tmp/wt")) == 99

    def test_creates_pr_when_missing(self) -> None:
        run_mock = MagicMock(
            side_effect=[
                _status("abc1234 commit msg"),  # git log
                _status(""),  # ls-remote (not pushed)
                _status(""),  # git push
            ]
        )
        gh_mock = _status("[]")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "_gh_call", return_value=gh_mock),
            patch.object(pr_manager, "create_pr", return_value=42) as create_mock,
        ):
            assert pr_manager.ensure_pr_created(1, "branch", Path("/tmp/wt")) == 42
            create_mock.assert_called_once_with(1, "branch", False)


class TestCreatePR:
    """Tests for create p r."""

    def test_invokes_gh_pr_create(self) -> None:
        issue = MagicMock(title="Add feature X")
        with (
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "gh_pr_create", return_value=7) as gh_mock,
        ):
            assert pr_manager.create_pr(5, "branch", auto_merge=True) == 7
        kwargs = gh_mock.call_args.kwargs
        assert kwargs["branch"] == "branch"
        assert "Add feature X" in kwargs["title"]
        assert kwargs["auto_merge"] is True
        assert "Closes #5" in kwargs["body"]
