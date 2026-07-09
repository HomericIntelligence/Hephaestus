"""Tests for shared git utilities."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import hephaestus.utils as utils_pkg
import hephaestus.utils.git as shared_git
from hephaestus.utils.helpers import NETWORK_TIMEOUT


def test_run_git_routes_through_standard_subprocess_helper() -> None:
    """run_git normalizes git commands and uses the shared subprocess adapter."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.utils.git.run_subprocess", return_value=completed) as mock_run:
        assert shared_git.run_git(["git", "status"], cwd=Path("/repo")) is completed

    mock_run.assert_called_once_with(
        ["git", "status"],
        cwd="/repo",
        check=True,
        timeout=NETWORK_TIMEOUT,
        dry_run=False,
        log_on_error=True,
    )


def test_run_git_retries_network_commands() -> None:
    """Network git operations retry transient subprocess failures."""
    failure = subprocess.CalledProcessError(128, ["git", "push"], stderr="network timeout")
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with (
        patch("hephaestus.utils.git.run_subprocess", side_effect=[failure, completed]) as mock_run,
        patch("hephaestus.utils.retry.time.sleep"),
    ):
        assert shared_git.run_git(["push", "origin", "feature"]) is completed

    assert mock_run.call_count == 2


def test_git_ls_remote_contains_matches_exact_branch_refs_only() -> None:
    """Branch shorthand must not match similarly named remote refs."""
    found = subprocess.CompletedProcess(["git"], 0, stdout="abc\trefs/heads/feature\n", stderr="")
    partial = subprocess.CompletedProcess(
        ["git"], 0, stdout="abc\trefs/heads/feature-old\n", stderr=""
    )
    with patch("hephaestus.utils.git.run_git", side_effect=[found, partial]):
        assert shared_git.git_ls_remote_contains(Path("/repo"), "origin", "feature") is True
        assert shared_git.git_ls_remote_contains(Path("/repo"), "origin", "feature") is False


def test_run_git_is_available_from_utils_package() -> None:
    """The package-level utils surface exposes the shared git runner."""
    assert utils_pkg.run_git is shared_git.run_git
