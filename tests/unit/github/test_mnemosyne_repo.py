"""Tests for hephaestus.github.mnemosyne_repo.

The resolver picks which ``owner/ProjectMnemosyne`` to clone, push to, and PR
against. All ``gh`` calls are mocked at the module namespace so no live network
or ``gh`` auth is required.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.github import mnemosyne_repo
from hephaestus.github.mnemosyne_repo import (
    UPSTREAM_SLUG,
    MnemosyneTarget,
    fork_upstream,
    gh_authenticated_login,
    remote_repo_exists,
    resolve_mnemosyne_target,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


@pytest.fixture(autouse=True)
def _clear_owner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the HEPH_MNEMOSYNE_OWNER override never leaks in from the host env."""
    monkeypatch.delenv(mnemosyne_repo.OWNER_ENV_VAR, raising=False)


class TestGhAuthenticatedLogin:
    """Tests for gh_authenticated_login()."""

    def test_returns_login(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", return_value=_completed(0, "mvillmow\n")):
            assert gh_authenticated_login() == "mvillmow"

    def test_nonzero_returns_none(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", return_value=_completed(1, "", "no auth")):
            assert gh_authenticated_login() is None

    def test_empty_stdout_returns_none(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", return_value=_completed(0, "   \n")):
            assert gh_authenticated_login() is None

    def test_exception_returns_none(self) -> None:
        with patch.object(
            mnemosyne_repo, "gh_call", side_effect=subprocess.TimeoutExpired("gh", 10)
        ):
            assert gh_authenticated_login() is None


class TestRemoteRepoExists:
    """Tests for remote_repo_exists()."""

    def test_exists(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", return_value=_completed(0, '{"name":"x"}')):
            assert remote_repo_exists("me/ProjectMnemosyne") is True

    def test_missing(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", return_value=_completed(1, "", "not found")):
            assert remote_repo_exists("me/ProjectMnemosyne") is False

    def test_exception_returns_false(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", side_effect=RuntimeError("boom")):
            assert remote_repo_exists("me/ProjectMnemosyne") is False


class TestForkUpstream:
    """Tests for fork_upstream()."""

    def test_success(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", return_value=_completed(0)) as gh:
            assert fork_upstream("me") is True
        args = gh.call_args[0][0]
        assert args[:2] == ["repo", "fork"]
        assert UPSTREAM_SLUG in args
        assert "--clone=false" in args

    def test_failure_returns_false(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", return_value=_completed(1, "", "denied")):
            assert fork_upstream("me") is False

    def test_exception_returns_false(self) -> None:
        with patch.object(mnemosyne_repo, "gh_call", side_effect=OSError("boom")):
            assert fork_upstream("me") is False


class TestResolveMnemosyneTarget:
    """Tests for resolve_mnemosyne_target() precedence ladder."""

    def test_upstream_login_clones_upstream_no_fork(self) -> None:
        with (
            patch.object(
                mnemosyne_repo, "gh_authenticated_login", return_value="HomericIntelligence"
            ),
            patch.object(mnemosyne_repo, "remote_repo_exists") as exists,
            patch.object(mnemosyne_repo, "fork_upstream") as fork,
        ):
            target = resolve_mnemosyne_target()
        assert target == MnemosyneTarget(
            owner="HomericIntelligence",
            slug=UPSTREAM_SLUG,
            is_fork_of_upstream=False,
        )
        exists.assert_not_called()
        fork.assert_not_called()

    def test_existing_user_fork_used_no_fork_created(self) -> None:
        with (
            patch.object(mnemosyne_repo, "gh_authenticated_login", return_value="mvillmow"),
            patch.object(mnemosyne_repo, "remote_repo_exists", return_value=True),
            patch.object(mnemosyne_repo, "fork_upstream") as fork,
        ):
            target = resolve_mnemosyne_target()
        assert target.slug == "mvillmow/ProjectMnemosyne"
        assert target.is_fork_of_upstream is True
        fork.assert_not_called()

    def test_missing_fork_is_created(self) -> None:
        with (
            patch.object(mnemosyne_repo, "gh_authenticated_login", return_value="mvillmow"),
            patch.object(mnemosyne_repo, "remote_repo_exists", return_value=False),
            patch.object(mnemosyne_repo, "fork_upstream", return_value=True) as fork,
        ):
            target = resolve_mnemosyne_target()
        assert target.slug == "mvillmow/ProjectMnemosyne"
        assert target.is_fork_of_upstream is True
        fork.assert_called_once_with("mvillmow")

    def test_fork_disabled_falls_back_to_upstream(self) -> None:
        with (
            patch.object(mnemosyne_repo, "gh_authenticated_login", return_value="mvillmow"),
            patch.object(mnemosyne_repo, "remote_repo_exists", return_value=False),
            patch.object(mnemosyne_repo, "fork_upstream") as fork,
        ):
            target = resolve_mnemosyne_target(allow_fork=False)
        assert target.slug == UPSTREAM_SLUG
        assert target.is_fork_of_upstream is False
        fork.assert_not_called()

    def test_fork_failure_falls_back_to_upstream(self) -> None:
        with (
            patch.object(mnemosyne_repo, "gh_authenticated_login", return_value="mvillmow"),
            patch.object(mnemosyne_repo, "remote_repo_exists", return_value=False),
            patch.object(mnemosyne_repo, "fork_upstream", return_value=False),
        ):
            target = resolve_mnemosyne_target()
        assert target.slug == UPSTREAM_SLUG

    def test_no_login_falls_back_to_upstream(self) -> None:
        with patch.object(mnemosyne_repo, "gh_authenticated_login", return_value=None):
            target = resolve_mnemosyne_target()
        assert target.slug == UPSTREAM_SLUG
        assert target.is_fork_of_upstream is False

    def test_explicit_override_owner_wins(self) -> None:
        with (
            patch.object(mnemosyne_repo, "gh_authenticated_login") as login,
            patch.object(mnemosyne_repo, "remote_repo_exists") as exists,
        ):
            target = resolve_mnemosyne_target(override_owner="acme")
        assert target == MnemosyneTarget(
            owner="acme",
            slug="acme/ProjectMnemosyne",
            is_fork_of_upstream=True,
        )
        login.assert_not_called()
        exists.assert_not_called()

    def test_env_override_owner_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(mnemosyne_repo.OWNER_ENV_VAR, "acme")
        with patch.object(mnemosyne_repo, "gh_authenticated_login") as login:
            target = resolve_mnemosyne_target()
        assert target.slug == "acme/ProjectMnemosyne"
        login.assert_not_called()

    def test_override_equal_to_upstream_is_not_marked_fork(self) -> None:
        target = resolve_mnemosyne_target(override_owner="HomericIntelligence")
        assert target.slug == UPSTREAM_SLUG
        assert target.is_fork_of_upstream is False

    def test_invalid_override_is_ignored(self) -> None:
        # An override that is already a slug (contains "/") is rejected; falls
        # through to gh login resolution.
        with patch.object(mnemosyne_repo, "gh_authenticated_login", return_value=None):
            target = resolve_mnemosyne_target(override_owner="bad/owner")
        assert target.slug == UPSTREAM_SLUG

    def test_login_with_slash_is_ignored(self) -> None:
        with patch.object(mnemosyne_repo, "gh_authenticated_login", return_value="weird/login"):
            target = resolve_mnemosyne_target()
        assert target.slug == UPSTREAM_SLUG
