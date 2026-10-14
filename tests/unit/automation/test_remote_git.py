"""Tests for bounded host-owned remote Git authentication."""

# ruff: noqa: D103
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation.remote_git import TrustedRemoteGit, trusted_gh_authenticated
from hephaestus.config.child_environments import build_git_signing_env, build_remote_git_env


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
            "hephaestus.automation.remote_git.trusted_gh_executable",
            return_value=None if failure == "gh" else "/trusted/gh",
        ),
        patch(
            "hephaestus.automation.remote_git.trusted_gh_authenticated",
            return_value=failure != "login",
        ),
        patch(
            "hephaestus.automation.remote_git.trusted_remote_git_config",
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
        patch("hephaestus.automation.remote_git.trusted_gh_executable", return_value="/trusted/gh"),
        patch("hephaestus.automation.remote_git.trusted_gh_authenticated", return_value=True),
        patch(
            "hephaestus.automation.remote_git.trusted_remote_git_config",
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


@pytest.mark.parametrize("extra", [None, Path("/host/tools")])
def test_default_host_transports_share_host_root(extra: Path | None) -> None:
    from hephaestus.automation.mnemosyne_learning_preparation import BoundLearningWorkspace
    from hephaestus.automation.mnemosyne_skill_host import (
        DefaultLearnDeliveryBackend,
        MnemosyneSkillHost,
    )

    host = MnemosyneSkillHost(gh_extra_path_root=extra)
    backend = host.delivery_service
    assert isinstance(backend, DefaultLearnDeliveryBackend)
    assert isinstance(backend._preparation._workspace, BoundLearningWorkspace)
    assert isinstance(backend._service.remote_git, TrustedRemoteGit)
    assert backend._service.remote_git.extra_path_root == extra
    assert isinstance(backend._preparation._workspace._remote_git, TrustedRemoteGit)
    assert backend._preparation._workspace._remote_git.extra_path_root == extra


def test_auth_decode_failure_is_safe() -> None:
    with patch(
        "hephaestus.utils.helpers.run_subprocess",
        side_effect=UnicodeDecodeError("utf-8", b"secret", 0, 1, "remote detail"),
    ):
        assert not trusted_gh_authenticated("/trusted/gh", 3)
