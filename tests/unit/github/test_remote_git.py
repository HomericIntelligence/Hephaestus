"""Tests for bounded host-owned remote Git authentication in the library layer."""

# ruff: noqa: D103
import argparse
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.config.child_environments import build_git_signing_env, build_remote_git_env
from hephaestus.github.remote_git import (
    TrustedRemoteGit,
    gh_extra_path_root_type,
    trusted_gh_authenticated,
    trusted_gh_executable,
)


@pytest.mark.parametrize(
    "tokens", [{}, {"GH_TOKEN": "fixture-token"}, {"GITHUB_TOKEN": "fixture-token"}]
)
def test_remote_credentials_are_not_local(tokens: dict[str, str]) -> None:
    with patch.dict("os.environ", tokens, clear=True):
        remote = build_remote_git_env()
        local = build_git_signing_env()
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        assert remote.get(key) == tokens.get(key)
        assert key not in local
    assert remote["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert remote["GIT_CONFIG_NOSYSTEM"] == "1"
    assert remote["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("failure", ["gh", "login", "ssh"])
def test_missing_auth_never_runs_git(tmp_path: Path, failure: str) -> None:
    with (
        patch(
            "hephaestus.github.remote_git.trusted_gh_executable",
            return_value=None if failure == "gh" else "/trusted/gh",
        ),
        patch(
            "hephaestus.github.remote_git.trusted_gh_authenticated",
            return_value=failure != "login",
        ),
        patch(
            "hephaestus.github.remote_git.trusted_remote_git_config",
            return_value=None if failure == "ssh" else ("-c", "credential.helper=trusted"),
        ),
        patch("hephaestus.utils.helpers.run_subprocess") as run,
        pytest.raises(RuntimeError, match=r"^remote Git authentication unavailable$"),
    ):
        TrustedRemoteGit()(tmp_path, ("push", "origin", "branch"), 5)
    run.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        OSError("secret"),
        UnicodeDecodeError("utf-8", b"secret", 0, 1, "remote detail"),
        subprocess.TimeoutExpired("secret", 1),
        subprocess.CompletedProcess([], 1, "secret", "secret"),
        subprocess.CompletedProcess([], 2, "secret", "secret"),
    ],
)
def test_remote_failures_remove_diagnostics(tmp_path: Path, failure: object) -> None:
    with (
        patch("hephaestus.github.remote_git.trusted_gh_executable", return_value="/trusted/gh"),
        patch("hephaestus.github.remote_git.trusted_gh_authenticated", return_value=True),
        patch(
            "hephaestus.github.remote_git.trusted_remote_git_config",
            return_value=("-c", "credential.helper=trusted"),
        ),
        patch(
            "hephaestus.utils.helpers.run_subprocess",
            side_effect=failure if isinstance(failure, Exception) else None,
            return_value=failure,
        ) as run,
    ):
        if isinstance(failure, Exception):
            with pytest.raises(RuntimeError, match=r"^remote Git transport failed$"):
                TrustedRemoteGit()(tmp_path, ("ls-remote", "origin", "branch"), 5)
        else:
            result = TrustedRemoteGit()(tmp_path, ("ls-remote", "origin", "branch"), 5)
            assert result.returncode in (1, 2)
            assert result.stdout == "" and result.stderr == "remote Git transport failed"
    assert run.call_args.args[0] == [
        "git",
        "-c",
        "credential.helper=trusted",
        "ls-remote",
        "origin",
        "branch",
    ]
    assert run.call_args.kwargs["track_process_group"] is True
    assert run.call_args.kwargs["timeout"] == 5


def test_auth_check_uses_approved_token_environment() -> None:
    with (
        patch.dict("os.environ", {"GH_TOKEN": "fixture-token", "UNTRUSTED": "secret"}, clear=True),
        patch(
            "hephaestus.utils.helpers.run_subprocess",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run,
    ):
        assert trusted_gh_authenticated("/trusted/gh", 7)
    assert run.call_args.kwargs["env"]["GH_TOKEN"] == "fixture-token"  # noqa: S105 - synthetic fixture
    assert "UNTRUSTED" not in run.call_args.kwargs["env"]
    assert run.call_args.args[0] == ["/trusted/gh", "auth", "status", "--hostname", "github.com"]


def test_auth_decode_failure_is_safe() -> None:
    with patch(
        "hephaestus.utils.helpers.run_subprocess",
        side_effect=UnicodeDecodeError("utf-8", b"secret", 0, 1, "remote detail"),
    ):
        assert not trusted_gh_authenticated("/trusted/gh", 3)


def test_executable_override_uses_operator_root(tmp_path: Path) -> None:
    (bindir := tmp_path / "bin").mkdir()
    executable = bindir / "gh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert trusted_gh_executable(tmp_path) == str(executable.resolve())


def test_executable_fallback_looks_up_path(tmp_path: Path) -> None:
    (bindir := tmp_path / "bin").mkdir()
    executable = bindir / "gh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    missing = tmp_path / "empty"
    missing.mkdir()

    assert trusted_gh_executable(missing, path=f"{missing}:{bindir}:{bindir}") == str(
        executable.resolve()
    )
    assert trusted_gh_executable(missing, path=f"{missing}:{missing}") is None


def test_root_without_gh_is_rejected(tmp_path: Path) -> None:
    (bindir := tmp_path / "bin").mkdir()
    executable = bindir / "gh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (empty := tmp_path / "no-gh").mkdir()

    assert gh_extra_path_root_type(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(argparse.ArgumentTypeError, match=r"executable bin/gh"):
        gh_extra_path_root_type(str(empty))
    with pytest.raises(argparse.ArgumentTypeError, match=r"absolute path"):
        gh_extra_path_root_type("relative/host/tools")
