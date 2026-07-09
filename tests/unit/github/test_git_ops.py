"""Tests for shared Git subprocess helpers."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

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
    with patch("hephaestus.utils.git.run_subprocess", return_value=completed) as mock_run:
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
    with patch("hephaestus.utils.git.run_subprocess", return_value=completed) as mock_run:
        assert run_git(["git", "push"], cwd=Path("/repo"), dry_run=True) is completed

    mock_run.assert_called_once_with(
        ["git", "push"],
        cwd="/repo",
        check=True,
        timeout=NETWORK_TIMEOUT,
        dry_run=True,
        log_on_error=False,
    )


def test_run_git_retries_network_commands_by_default() -> None:
    """Network git commands get bounded retry protection by default."""
    failure = subprocess.CalledProcessError(128, ["git", "fetch"], stderr="network timeout")
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with (
        patch("hephaestus.utils.git.run_subprocess", side_effect=[failure, completed]) as mock_run,
        patch("hephaestus.utils.retry.time.sleep"),
    ):
        assert run_git(["fetch", "origin"]) is completed

    assert mock_run.call_count == 2


def test_run_git_suppresses_error_log_noise_during_retries() -> None:
    """Retry attempts use retry warnings instead of per-attempt ERROR logs."""
    failure = subprocess.CalledProcessError(128, ["git", "fetch"], stderr="network timeout")
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with (
        patch("hephaestus.utils.git.run_subprocess", side_effect=[failure, completed]) as mock_run,
        patch("hephaestus.utils.retry.time.sleep"),
    ):
        assert run_git(["fetch", "origin"]) is completed

    assert [call.kwargs["log_on_error"] for call in mock_run.call_args_list] == [False, False]


def test_run_git_does_not_retry_local_commands_by_default() -> None:
    """Local metadata commands stay single-shot unless the caller asks for retries."""
    failure = subprocess.CalledProcessError(128, ["git", "status"], stderr="fatal")
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.utils.git.run_subprocess", side_effect=[failure, completed]) as mock_run:
        with pytest.raises(subprocess.CalledProcessError):
            run_git(["status"])

    assert mock_run.call_count == 1


def test_run_git_does_not_retry_deterministic_network_command_errors() -> None:
    """Network commands fail fast when Git reports a deterministic error."""
    failure = subprocess.CalledProcessError(
        128, ["git", "fetch"], stderr="fatal: Authentication failed"
    )
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.utils.git.run_subprocess", side_effect=[failure, completed]) as mock_run:
        with pytest.raises(subprocess.CalledProcessError):
            run_git(["fetch", "origin"])

    assert mock_run.call_count == 1


def test_run_git_honors_explicit_retry_budget_for_local_commands() -> None:
    """Callers can request retries even for local git commands."""
    failure = subprocess.TimeoutExpired(["git", "status"], timeout=1)
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with (
        patch("hephaestus.utils.git.run_subprocess", side_effect=[failure, completed]) as mock_run,
        patch("hephaestus.utils.retry.time.sleep"),
    ):
        assert run_git(["status"], retries=1) is completed

    assert mock_run.call_count == 2


def test_working_tree_clean_uses_git_status_porcelain() -> None:
    """A clean porcelain status means the working tree is clean."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.utils.git.run_subprocess", return_value=completed) as mock_run:
        assert working_tree_clean() is True

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "status", "--porcelain"]
    assert mock_run.call_args.kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args.kwargs["check"] is False


def test_working_tree_clean_rejects_dirty_or_failed_status() -> None:
    """Dirty output or a failed git status is not clean."""
    dirty = subprocess.CompletedProcess(["git"], 0, stdout=" M file.py\n", stderr="")
    failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal")

    with patch("hephaestus.utils.git.run_subprocess", return_value=dirty):
        assert working_tree_clean() is False
    with patch("hephaestus.utils.git.run_subprocess", return_value=failed):
        assert working_tree_clean() is False


def test_in_git_repo_uses_rev_parse_git_dir() -> None:
    """in_git_repo delegates to git rev-parse --git-dir."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout=".git\n", stderr="")
    with patch("hephaestus.utils.git.run_subprocess", return_value=completed) as mock_run:
        assert in_git_repo() is True

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "rev-parse", "--git-dir"]
    assert mock_run.call_args.kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args.kwargs["check"] is False


def test_repo_root_parses_rev_parse_stdout() -> None:
    """repo_root returns the stripped git toplevel path."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="/repo\n", stderr="")
    with patch("hephaestus.utils.git.run_subprocess", return_value=completed) as mock_run:
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
    with patch("hephaestus.utils.git.run_git", side_effect=[local, global_result]) as mock_run:
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
    with patch("hephaestus.utils.git.run_git", return_value=missing):
        assert git_config_get("user.signingkey") is None


def test_git_remote_url_returns_origin_url() -> None:
    """git_remote_url wraps git remote get-url."""
    completed = subprocess.CompletedProcess(
        ["git"], 0, stdout="git@github.com:o/r.git\n", stderr=""
    )
    with patch("hephaestus.utils.git.run_git", return_value=completed) as mock_run:
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
    with patch("hephaestus.utils.git.run_git", side_effect=[present, absent]) as mock_run:
        assert git_branch_exists("feature") is True
        assert git_branch_exists("missing") is False

    assert mock_run.call_args_list[0].args[0] == ["branch", "--list", "feature"]
    assert mock_run.call_args_list[0].kwargs["timeout"] == METADATA_TIMEOUT
    assert mock_run.call_args_list[0].kwargs["check"] is False
    assert mock_run.call_args_list[0].kwargs["log_on_error"] is False


def test_git_push_builds_remote_refspec_command() -> None:
    """git_push wraps the standard push form used by PR merge helpers."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.utils.git.run_git", return_value=completed) as mock_run:
        assert git_push(Path("/repo"), "origin", "feature:feature", dry_run=True) is completed

    mock_run.assert_called_once_with(
        ["push", "origin", "feature:feature"],
        cwd=Path("/repo"),
        dry_run=True,
        timeout=NETWORK_TIMEOUT,
        retries=None,
    )


def test_git_unmerged_files_splits_conflict_output() -> None:
    """git_unmerged_files returns stripped non-empty conflicted file paths."""
    completed = subprocess.CompletedProcess(
        ["git"],
        0,
        stdout=" a.py\n\nsubdir/b.py \n",
        stderr="",
    )
    with patch("hephaestus.utils.git.run_git", return_value=completed) as mock_run:
        assert git_unmerged_files(Path("/repo")) == ["a.py", "subdir/b.py"]

    mock_run.assert_called_once_with(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=Path("/repo"),
        timeout=METADATA_TIMEOUT,
    )


def test_git_rev_list_count_parses_integer_count() -> None:
    """git_rev_list_count parses git rev-list --count output."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="3\n", stderr="")
    with patch("hephaestus.utils.git.run_git", return_value=completed) as mock_run:
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
    with patch("hephaestus.utils.git.run_git", side_effect=[found, missing]):
        assert git_ls_remote_contains(Path("/repo"), "origin", "feature") is True
        assert git_ls_remote_contains(Path("/repo"), "origin", "missing") is False


def test_git_ls_remote_contains_rejects_partial_ref_matches() -> None:
    """A similarly named remote branch must not satisfy the requested branch."""
    partial = subprocess.CompletedProcess(
        ["git"],
        0,
        stdout="abc\trefs/heads/feature-old\n",
        stderr="",
    )
    with patch("hephaestus.utils.git.run_git", return_value=partial):
        assert git_ls_remote_contains(Path("/repo"), "origin", "feature") is False


def test_git_ls_remote_contains_can_raise_probe_errors() -> None:
    """Callers can distinguish remote probe failures from absent refs."""
    probe_error = subprocess.TimeoutExpired(["git", "ls-remote"], timeout=1)
    with patch("hephaestus.utils.git.run_git", side_effect=probe_error):
        assert git_ls_remote_contains(Path("/repo"), "origin", "feature") is False
        with pytest.raises(subprocess.TimeoutExpired):
            git_ls_remote_contains(Path("/repo"), "origin", "feature", raise_on_error=True)


def test_shared_git_helpers_live_in_utils_package() -> None:
    """The shared git helper implementation belongs below hephaestus.utils."""
    assert importlib.util.find_spec("hephaestus.utils.git") is not None


def test_run_git_requires_captured_text_output() -> None:
    """run_git keeps one captured text-output contract for all callers."""
    with pytest.raises(ValueError, match="captures text output"):
        run_git(["status"], capture_output=False)
    with pytest.raises(ValueError, match="captures text output"):
        run_git(["status"], text=False)


def test_git_config_get_returns_none_for_process_errors() -> None:
    """git_config_get treats unavailable git metadata as missing config."""
    with patch(
        "hephaestus.utils.git.run_git",
        side_effect=[subprocess.TimeoutExpired(["git"], 1), FileNotFoundError("git")],
    ):
        assert git_config_get("user.email") is None
        assert git_config_get("user.name") is None


def test_git_remote_url_returns_none_for_missing_values_and_process_errors() -> None:
    """git_remote_url handles missing remotes and unavailable git."""
    missing = subprocess.CompletedProcess(["git"], 1, stdout="", stderr="missing")
    with patch(
        "hephaestus.utils.git.run_git",
        side_effect=[
            missing,
            subprocess.TimeoutExpired(["git"], 1),
            FileNotFoundError("git"),
        ],
    ):
        assert git_remote_url() is None
        assert git_remote_url("upstream") is None
        assert git_remote_url("fork") is None


def test_git_branch_exists_returns_false_for_process_errors() -> None:
    """git_branch_exists degrades to False when git cannot list branches."""
    called_process_error = subprocess.CalledProcessError(128, ["git"], stderr="fatal")
    with patch(
        "hephaestus.utils.git.run_git",
        side_effect=[
            called_process_error,
            subprocess.TimeoutExpired(["git"], 1),
            FileNotFoundError("git"),
        ],
    ):
        assert git_branch_exists("feature") is False
        assert git_branch_exists("feature") is False
        assert git_branch_exists("feature") is False


def test_git_ls_remote_contains_accepts_full_exact_refs() -> None:
    """Full ref names are matched exactly without forcing a branch shorthand."""
    found = subprocess.CompletedProcess(["git"], 0, stdout="abc\trefs/tags/v1.0.0\n", stderr="")
    with patch("hephaestus.utils.git.run_git", return_value=found):
        assert git_ls_remote_contains(Path("/repo"), "origin", "refs/tags/v1.0.0") is True


def test_github_git_ops_reexports_shared_helpers() -> None:
    """GitHub callers keep their old import path while using the utils implementation."""
    import hephaestus.github.git_ops as github_git_ops
    import hephaestus.utils.git as shared_git

    assert github_git_ops.run_git is shared_git.run_git
    assert github_git_ops.git_ls_remote_contains is shared_git.git_ls_remote_contains
