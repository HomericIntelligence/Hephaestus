"""Tests for shared Git subprocess helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from hephaestus.github.git_ops import in_git_repo, repo_root, run_git, working_tree_clean
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT


def test_run_git_uses_standard_defaults() -> None:
    """run_git prepends git and applies the repository timeout defaults."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed) as mock_run:
        assert run_git(["status"]) is completed

    mock_run.assert_called_once_with(
        ["git", "status"],
        cwd=None,
        capture_output=True,
        text=True,
        check=True,
        timeout=NETWORK_TIMEOUT,
    )


def test_working_tree_clean_uses_git_status_porcelain() -> None:
    """A clean porcelain status means the working tree is clean."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed) as mock_run:
        assert working_tree_clean() is True

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "status", "--porcelain"]
    assert mock_run.call_args.kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args.kwargs["check"] is False


def test_working_tree_clean_rejects_dirty_or_failed_status() -> None:
    """Dirty output or a failed git status is not clean."""
    dirty = subprocess.CompletedProcess(["git"], 0, stdout=" M file.py\n", stderr="")
    failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal")

    with patch("subprocess.run", return_value=dirty):
        assert working_tree_clean() is False
    with patch("subprocess.run", return_value=failed):
        assert working_tree_clean() is False


def test_in_git_repo_uses_rev_parse_git_dir() -> None:
    """in_git_repo delegates to git rev-parse --git-dir."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout=".git\n", stderr="")
    with patch("subprocess.run", return_value=completed) as mock_run:
        assert in_git_repo() is True

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "rev-parse", "--git-dir"]
    assert mock_run.call_args.kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args.kwargs["check"] is False


def test_repo_root_parses_rev_parse_stdout() -> None:
    """repo_root returns the stripped git toplevel path."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="/repo\n", stderr="")
    with patch("subprocess.run", return_value=completed) as mock_run:
        assert repo_root() == Path("/repo")

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "rev-parse", "--show-toplevel"]
    assert mock_run.call_args.kwargs["timeout"] == METADATA_TIMEOUT
