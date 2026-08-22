"""Tests for shared git utilities."""

from __future__ import annotations

import inspect
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import ANY, patch

import hephaestus.utils as utils_pkg
import hephaestus.utils.git as shared_git
from hephaestus.diagnostics import bounded_git_diagnostic
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT


def test_bounded_git_diagnostic_redacts_credentials_before_truncation() -> None:
    """Durable Git failure evidence is bounded and excludes credential values."""
    diagnostic = (
        "earlier output " * 20 + "https://operator:credential-value@example.invalid/repository.git"
    )

    result = bounded_git_diagnostic(diagnostic, limit=80)

    assert len(result) <= 80
    assert "credential-value" not in result
    assert "<redacted-git-url>" in result


def test_run_git_routes_through_standard_subprocess_helper() -> None:
    """run_git normalizes git commands and uses the shared subprocess adapter."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.utils.git.run_subprocess", return_value=completed) as mock_run:
        assert shared_git.run_git(["git", "status"], cwd=Path("/repo")) is completed

    mock_run.assert_called_once_with(
        ["git", "status"],
        env=ANY,
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


def test_run_git_does_not_retry_deterministic_network_command_errors() -> None:
    """Deterministic network-command failures are not retried."""
    failure = subprocess.CalledProcessError(
        128, ["git", "push"], stderr="fatal: Authentication failed"
    )
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
    with patch("hephaestus.utils.git.run_subprocess", side_effect=[failure, completed]) as mock_run:
        try:
            shared_git.run_git(["push", "origin", "feature"])
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover - assertion below is clearer when this fails
            raise AssertionError("deterministic Git failure should be raised")

    assert mock_run.call_count == 1


def test_git_ls_remote_contains_matches_exact_branch_refs_only() -> None:
    """Branch shorthand must not match similarly named remote refs."""
    found = subprocess.CompletedProcess(["git"], 0, stdout="abc\trefs/heads/feature\n", stderr="")
    partial = subprocess.CompletedProcess(
        ["git"], 0, stdout="abc\trefs/heads/feature-old\n", stderr=""
    )
    with patch("hephaestus.utils.git.run_git", side_effect=[found, partial]):
        assert shared_git.git_ls_remote_contains(Path("/repo"), "origin", "feature") is True
        assert shared_git.git_ls_remote_contains(Path("/repo"), "origin", "feature") is False


def test_git_ls_remote_contains_can_raise_probe_errors() -> None:
    """Callers can request an exception when a remote probe fails."""
    probe_error = subprocess.TimeoutExpired(["git", "ls-remote"], timeout=1)
    with patch("hephaestus.utils.git.run_git", side_effect=probe_error):
        assert shared_git.git_ls_remote_contains(Path("/repo"), "origin", "feature") is False
        try:
            shared_git.git_ls_remote_contains(
                Path("/repo"), "origin", "feature", raise_on_error=True
            )
        except subprocess.TimeoutExpired:
            pass
        else:  # pragma: no cover - assertion below is clearer when this fails
            raise AssertionError("remote probe failure should be raised")


def test_git_helper_timeout_defaults_are_stable() -> None:
    """Metadata and network helpers expose the established explicit defaults."""
    metadata_helpers: tuple[Callable[..., object], ...] = (
        shared_git.git_config_get,
        shared_git.git_unmerged_files,
        shared_git.git_rev_list_count,
    )
    network_helpers: tuple[Callable[..., object], ...] = (
        shared_git.git_ls_remote_contains,
        shared_git.git_ls_remote_sha,
    )

    for helper in metadata_helpers:
        assert inspect.signature(helper).parameters["timeout"].default == METADATA_TIMEOUT == 10
    for helper in network_helpers:
        assert inspect.signature(helper).parameters["timeout"].default == NETWORK_TIMEOUT == 120


def test_git_config_get_forwards_explicit_timeout() -> None:
    """Git config metadata probes honor a caller timeout budget."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="value\n", stderr="")
    with patch("hephaestus.utils.git.run_git", return_value=completed) as run:
        assert shared_git.git_config_get("user.email", timeout=17) == "value"

    assert run.call_args.kwargs["timeout"] == 17


def test_git_unmerged_files_forwards_explicit_timeout() -> None:
    """Conflict probes honor a caller timeout budget."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="one\0two\0", stderr="")
    with patch("hephaestus.utils.git.run_git", return_value=completed) as run:
        assert shared_git.git_unmerged_files(Path("/repo"), timeout=18) == ["one", "two"]

    assert run.call_args.kwargs["timeout"] == 18


def test_git_rev_list_count_forwards_explicit_timeout() -> None:
    """Revision-count probes honor a caller timeout budget."""
    completed = subprocess.CompletedProcess(["git"], 0, stdout="42\n", stderr="")
    with patch("hephaestus.utils.git.run_git", return_value=completed) as run:
        assert shared_git.git_rev_list_count(Path("/repo"), "HEAD", timeout=19) == 42

    assert run.call_args.kwargs["timeout"] == 19


def test_git_ls_remote_sha_forwards_explicit_timeout() -> None:
    """Remote-ref probes honor a caller timeout budget."""
    completed = subprocess.CompletedProcess(
        ["git"], 0, stdout="abc\trefs/heads/feature\n", stderr=""
    )
    with patch("hephaestus.utils.git.run_git", return_value=completed) as run:
        assert shared_git.git_ls_remote_sha(Path("/repo"), "origin", "feature", timeout=20) == "abc"

    assert run.call_args.kwargs["timeout"] == 20


def test_git_ls_remote_contains_threads_explicit_timeout() -> None:
    """The boolean remote-ref facade passes its caller budget to the SHA probe."""
    with patch("hephaestus.utils.git.git_ls_remote_sha", return_value="abc") as probe:
        assert shared_git.git_ls_remote_contains(Path("/repo"), "origin", "feature", timeout=21)

    probe.assert_called_once_with(
        Path("/repo"),
        "origin",
        "feature",
        raise_on_error=False,
        timeout=21,
    )


def test_run_git_is_available_from_utils_package() -> None:
    """The package-level utils surface exposes the shared git runner."""
    assert utils_pkg.run_git is shared_git.run_git
