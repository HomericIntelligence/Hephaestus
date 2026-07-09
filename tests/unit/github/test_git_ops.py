"""Tests for shared Git subprocess helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from hephaestus.github.git_ops import (
    git_branch_exists,
    git_config_get,
    git_ls_remote_contains,
    git_push,
    git_remote_url,
    git_rev_list_count,
    git_unmerged_files,
    in_git_repo,
    repo_root,
    run_git,
    working_tree_clean,
)
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT


def test_run_git_uses_shared_subprocess_helper() -> None:
    """run_git centralizes git execution through the standard subprocess helper."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.github.git_ops.run_subprocess", return_value=completed) as mock_run:
        assert run_git(["status"]) is completed

    mock_run.assert_called_once_with(
        ["git", "status"],
        cwd=None,
        check=True,
        timeout=NETWORK_TIMEOUT,
        dry_run=False,
        log_on_error=True,
    )


def test_run_git_accepts_git_prefixed_commands_and_dry_run() -> None:
    """Compatibility wrappers may pass a full git command without duplicating git."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.github.git_ops.run_subprocess", return_value=completed) as mock_run:
        assert run_git(["git", "push"], cwd=Path("/repo"), dry_run=True) is completed

    mock_run.assert_called_once_with(
        ["git", "push"],
        cwd="/repo",
        check=True,
        timeout=NETWORK_TIMEOUT,
        dry_run=True,
        log_on_error=True,
    )


def test_working_tree_clean_uses_git_status_porcelain() -> None:
    """A clean porcelain status means the working tree is clean."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.github.git_ops.run_subprocess", return_value=completed) as mock_run:
        assert working_tree_clean() is True

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "status", "--porcelain"]
    assert mock_run.call_args.kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args.kwargs["check"] is False


def test_working_tree_clean_rejects_dirty_or_failed_status() -> None:
    """Dirty output or a failed git status is not clean."""
    dirty = subprocess.CompletedProcess(["git"], 0, stdout=" M file.py\n", stderr="")
    failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal")

    with patch("hephaestus.github.git_ops.run_subprocess", return_value=dirty):
        assert working_tree_clean() is False
    with patch("hephaestus.github.git_ops.run_subprocess", return_value=failed):
        assert working_tree_clean() is False


def test_in_git_repo_uses_rev_parse_git_dir() -> None:
    """in_git_repo delegates to git rev-parse --git-dir."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout=".git\n", stderr="")
    with patch("hephaestus.github.git_ops.run_subprocess", return_value=completed) as mock_run:
        assert in_git_repo() is True

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "rev-parse", "--git-dir"]
    assert mock_run.call_args.kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args.kwargs["check"] is False


def test_repo_root_parses_rev_parse_stdout() -> None:
    """repo_root returns the stripped git toplevel path."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="/repo\n", stderr="")
    with patch("hephaestus.github.git_ops.run_subprocess", return_value=completed) as mock_run:
        assert repo_root() == Path("/repo")

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "rev-parse", "--show-toplevel"]
    assert mock_run.call_args.kwargs["timeout"] == METADATA_TIMEOUT


def test_git_config_get_reads_local_and_global_values() -> None:
    """git_config_get returns stripped config values from local or global config."""
    local = subprocess.CompletedProcess(["git"], 0, stdout="local@example.com\n", stderr="")
    global_result = subprocess.CompletedProcess(
        ["git"], 0, stdout="global@example.com\n", stderr=""
    )
    with patch("hephaestus.github.git_ops.run_git", side_effect=[local, global_result]) as mock_run:
        assert git_config_get("user.email") == "local@example.com"
        assert git_config_get("user.email", global_=True) == "global@example.com"

    assert mock_run.call_args_list[0].args[0] == ["config", "--get", "user.email"]
    assert mock_run.call_args_list[1].args[0] == ["config", "--global", "--get", "user.email"]
    assert mock_run.call_args_list[0].kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args_list[0].kwargs["check"] is False
    assert mock_run.call_args_list[0].kwargs["log_on_error"] is False


def test_git_config_get_returns_none_for_missing_values() -> None:
    """Missing git config keys produce None instead of raising."""
    missing = subprocess.CompletedProcess(["git"], 1, stdout="", stderr="missing")
    with patch("hephaestus.github.git_ops.run_git", return_value=missing):
        assert git_config_get("user.signingkey") is None


def test_git_remote_url_returns_origin_url() -> None:
    """git_remote_url wraps git remote get-url."""
    completed = subprocess.CompletedProcess(
        ["git"], 0, stdout="git@github.com:o/r.git\n", stderr=""
    )
    with patch("hephaestus.github.git_ops.run_git", return_value=completed) as mock_run:
        assert git_remote_url() == "git@github.com:o/r.git"

    mock_run.assert_called_once_with(
        ["remote", "get-url", "origin"],
        cwd=None,
        timeout=METADATA_TIMEOUT,
        check=False,
        log_on_error=False,
    )


def test_git_branch_exists_checks_local_branch_list() -> None:
    """git_branch_exists returns whether a local branch is listed."""
    present = subprocess.CompletedProcess(["git"], 0, stdout="  feature\n", stderr="")
    absent = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.github.git_ops.run_git", side_effect=[present, absent]) as mock_run:
        assert git_branch_exists("feature") is True
        assert git_branch_exists("missing") is False

    assert mock_run.call_args_list[0].args[0] == ["branch", "--list", "feature"]
    assert mock_run.call_args_list[0].kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args_list[0].kwargs["check"] is False
    assert mock_run.call_args_list[0].kwargs["log_on_error"] is False


def test_git_push_builds_remote_refspec_command() -> None:
    """git_push wraps the standard push form used by PR merge helpers."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.github.git_ops.run_git", return_value=completed) as mock_run:
        assert git_push(Path("/repo"), "origin", "feature:feature", dry_run=True) is completed

    mock_run.assert_called_once_with(
        ["push", "origin", "feature:feature"],
        cwd=Path("/repo"),
        dry_run=True,
        timeout=NETWORK_TIMEOUT,
    )


def test_git_unmerged_files_splits_conflict_output() -> None:
    """git_unmerged_files returns stripped non-empty conflicted file paths."""
    completed = subprocess.CompletedProcess(
        ["git"],
        0,
        stdout=" a.py\n\nsubdir/b.py \n",
        stderr="",
    )
    with patch("hephaestus.github.git_ops.run_git", return_value=completed) as mock_run:
        assert git_unmerged_files(Path("/repo")) == ["a.py", "subdir/b.py"]

    mock_run.assert_called_once_with(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=Path("/repo"),
        timeout=METADATA_TIMEOUT,
    )


def test_git_rev_list_count_parses_integer_count() -> None:
    """git_rev_list_count parses git rev-list --count output."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="3\n", stderr="")
    with patch("hephaestus.github.git_ops.run_git", return_value=completed) as mock_run:
        assert git_rev_list_count(Path("/repo"), "origin/main..HEAD") == 3

    mock_run.assert_called_once_with(
        ["rev-list", "--count", "origin/main..HEAD"],
        cwd=Path("/repo"),
        timeout=METADATA_TIMEOUT,
    )


def test_git_ls_remote_contains_checks_ref_stdout() -> None:
    """git_ls_remote_contains returns True when the requested ref appears remotely."""
    found = subprocess.CompletedProcess(["git"], 0, stdout="abc\trefs/heads/feature\n", stderr="")
    missing = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.github.git_ops.run_git", side_effect=[found, missing]):
        assert git_ls_remote_contains(Path("/repo"), "origin", "feature") is True
        assert git_ls_remote_contains(Path("/repo"), "origin", "missing") is False
