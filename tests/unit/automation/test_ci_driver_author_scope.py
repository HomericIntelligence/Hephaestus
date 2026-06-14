"""Author-scope tests for CIDriver discovery (#821)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.ci_driver import CIDriver
from hephaestus.automation.github_api import GitHubUnavailableError
from hephaestus.automation.models import CIDriverOptions


@pytest.fixture
def mock_options() -> CIDriverOptions:
    """Minimal options for author-scope tests. Mirrors test_ci_driver.py:54."""
    return CIDriverOptions(
        issues=[123],
        max_workers=1,
        dry_run=False,
        enable_ui=False,
        enable_advise=False,
        max_fix_iterations=1,
    )


@pytest.fixture
def driver(mock_options: CIDriverOptions, tmp_path: Path) -> CIDriver:
    """CIDriver with mocked repo root. Mirrors test_ci_driver.py:74."""
    with (
        patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
        patch("hephaestus.automation.ci_driver.WorktreeManager"),
        patch("hephaestus.automation.ci_driver.StatusTracker"),
    ):
        d = CIDriver(mock_options)
        d.state_dir = tmp_path
        return d


@pytest.fixture
def viewer_driver(driver: CIDriver) -> CIDriver:
    """Driver with viewer login pre-cached to 'mvillmow' so filter is deterministic."""
    driver._viewer_login = "mvillmow"
    return driver


# Mixed-author /pulls payload — note `login` keys on every fixture PR.
_MISSING_LOGIN_PULLS = [
    {
        "number": 200,
        "user": {"type": "User", "login": None},
        "auto_merge": None,
        "title": "malformed-no-login",
        "head": {"ref": "bx"},
        "labels": [],
    },
]

_MISSING_LOGIN_BOT_PULLS = [
    {
        "number": 201,
        "user": {"type": "Bot", "login": None},
        "auto_merge": None,
        "title": "malformed-bot-no-login",
        "head": {"ref": "by"},
        "labels": [],
    },
]

_MIXED_PULLS = [
    {
        "number": 100,
        "user": {"type": "User", "login": "mvillmow"},
        "auto_merge": None,
        "title": "mine",
        "head": {"ref": "b1"},
        "labels": [],
    },
    {
        "number": 101,
        "user": {"type": "User", "login": "alice"},
        "auto_merge": None,
        "title": "alice",
        "head": {"ref": "b2"},
        "labels": [],
    },
    {
        "number": 102,
        "user": {"type": "Bot", "login": "dependabot[bot]"},
        "auto_merge": None,
        "title": "bot",
        "head": {"ref": "b3"},
        "labels": [],
    },
]


class TestDefaultScopedDiscovery:
    """include_all_authors=False (default) hides non-viewer PRs."""

    def test_bot_pr_discovery_filters_to_viewer(self, viewer_driver: CIDriver) -> None:
        viewer_driver.options.include_all_authors = False
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MIXED_PULLS)),
            ),
        ):
            assert viewer_driver._discover_bot_prs() == {}

    def test_open_prs_remaining_filters_to_viewer(self, viewer_driver: CIDriver) -> None:
        viewer_driver.options.include_all_authors = False
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MIXED_PULLS)),
            ),
            patch.object(viewer_driver, "_pr_merge_state", return_value=("CLEAN", "MERGEABLE")),
        ):
            remaining = viewer_driver._list_open_prs_remaining()
        assert [pr["number"] for pr in remaining] == [100]


class TestAllFlagScopedDiscovery:
    """include_all_authors=True (--all) restores broad discovery."""

    def test_bot_pr_discovery_returns_all_bots(self, driver: CIDriver) -> None:
        driver.options.include_all_authors = True
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MIXED_PULLS)),
            ),
        ):
            assert driver._discover_bot_prs() == {102: 102}

    def test_open_prs_remaining_returns_all_prs(self, driver: CIDriver) -> None:
        driver.options.include_all_authors = True
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MIXED_PULLS)),
            ),
            patch.object(driver, "_pr_merge_state", return_value=("CLEAN", "MERGEABLE")),
        ):
            remaining = driver._list_open_prs_remaining()
        assert sorted(pr["number"] for pr in remaining) == [100, 101, 102]


class TestIssueScopedOverridesAuthor:
    """`--issues N` bypasses the author filter (AC9)."""

    def test_explicit_issue_with_other_author_pr(self, viewer_driver: CIDriver) -> None:
        viewer_driver.options.include_all_authors = False
        with (
            patch.object(viewer_driver, "_find_pr_for_issue", return_value=101),
            patch.object(viewer_driver, "_discover_bot_prs", return_value={}),
        ):
            assert viewer_driver._discover_prs([814]) == {814: 101}


class TestResolveViewerLogin:
    """Viewer login resolution is lazy, cached, and fails CLOSED."""

    def test_resolve_caches_value(self, driver: CIDriver) -> None:
        driver._viewer_login = ""  # reset
        with patch(
            "hephaestus.automation.ci_driver._gh_call", return_value=MagicMock(stdout="mvillmow\n")
        ) as mock_gh:
            assert driver._resolve_viewer_login() == "mvillmow"
            assert driver._resolve_viewer_login() == "mvillmow"
        assert mock_gh.call_count == 1

    def test_resolve_failure_raises_runtimeerror(self, driver: CIDriver) -> None:
        driver._viewer_login = ""
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=subprocess.CalledProcessError(1, ["gh"]),
        ):
            with pytest.raises(RuntimeError, match="Could not resolve viewer login"):
                driver._resolve_viewer_login()

    def test_resolve_empty_stdout_raises(self, driver: CIDriver) -> None:
        driver._viewer_login = ""
        with patch("hephaestus.automation.ci_driver._gh_call", return_value=MagicMock(stdout="")):
            with pytest.raises(RuntimeError, match="Could not resolve viewer login"):
                driver._resolve_viewer_login()

    def test_resolve_gh_not_installed_raises(self, driver: CIDriver) -> None:
        driver._viewer_login = ""
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=FileNotFoundError("gh not on PATH"),
        ):
            with pytest.raises(RuntimeError, match="Could not resolve viewer login"):
                driver._resolve_viewer_login()

    def test_resolve_breaker_open_raises_with_guidance(self, driver: CIDriver) -> None:
        """Open circuit breaker still fails CLOSED with operator guidance.

        ``GitHubUnavailableError`` (raised when the breaker opens) is mapped
        to a ``RuntimeError`` carrying the `gh auth login` / --all guidance
        rather than propagating raw (#821).
        """
        driver._viewer_login = ""
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=GitHubUnavailableError("circuit breaker open"),
        ):
            with pytest.raises(RuntimeError, match="Could not resolve viewer login"):
                driver._resolve_viewer_login()


class TestAllFlagSkipsViewerResolution:
    """When --all is set, viewer resolution is not required."""

    def test_discover_bot_prs_under_all_does_not_call_resolver(self, driver: CIDriver) -> None:
        driver.options.include_all_authors = True
        with (
            patch.object(
                driver,
                "_resolve_viewer_login",
                side_effect=RuntimeError("should not be called"),
            ),
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MIXED_PULLS)),
            ),
        ):
            assert driver._discover_bot_prs() == {102: 102}

    def test_list_open_prs_remaining_under_all_does_not_call_resolver(
        self, driver: CIDriver
    ) -> None:
        driver.options.include_all_authors = True
        with (
            patch.object(
                driver,
                "_resolve_viewer_login",
                side_effect=RuntimeError("should not be called"),
            ),
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MIXED_PULLS)),
            ),
            patch.object(driver, "_pr_merge_state", return_value=("CLEAN", "MERGEABLE")),
        ):
            remaining = driver._list_open_prs_remaining()
        assert sorted(pr["number"] for pr in remaining) == [100, 101, 102]


class TestMissingUserLogin:
    """PRs with user.login=None emit a warning instead of silently disappearing (#1152)."""

    def test_list_open_prs_remaining_warns_on_missing_login(
        self, viewer_driver: CIDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MISSING_LOGIN_PULLS)),
            ),
            caplog.at_level(logging.WARNING, logger="hephaestus.automation.ci_driver"),
        ):
            result = viewer_driver._list_open_prs_remaining()

        assert result == []
        assert any(
            "PR #200 has no user.login" in record.message and record.levelname == "WARNING"
            for record in caplog.records
        )

    def test_discover_bot_prs_warns_on_missing_login(
        self, viewer_driver: CIDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MISSING_LOGIN_BOT_PULLS)),
            ),
            caplog.at_level(logging.WARNING, logger="hephaestus.automation.ci_driver"),
        ):
            result = viewer_driver._discover_bot_prs()

        assert result == {}
        assert any(
            "PR #201 has no user.login" in record.message and record.levelname == "WARNING"
            for record in caplog.records
        )

    def test_list_open_prs_remaining_no_warning_under_all(
        self, driver: CIDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warning must NOT fire when --all is set (viewer filter bypassed entirely)."""
        driver.options.include_all_authors = True
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MISSING_LOGIN_PULLS)),
            ),
            patch.object(driver, "_pr_merge_state", return_value=("CLEAN", "MERGEABLE")),
            caplog.at_level(logging.WARNING, logger="hephaestus.automation.ci_driver"),
        ):
            result = driver._list_open_prs_remaining()

        assert any(r["number"] == 200 for r in result), "PR must appear in results under --all"
        assert not any("has no user.login" in record.message for record in caplog.records), (
            "Warning must not fire when viewer filter is bypassed"
        )

    def test_discover_bot_prs_no_warning_under_all(
        self, driver: CIDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warning must NOT fire when --all is set (viewer filter bypassed entirely)."""
        driver.options.include_all_authors = True
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(_MISSING_LOGIN_BOT_PULLS)),
            ),
            caplog.at_level(logging.WARNING, logger="hephaestus.automation.ci_driver"),
        ):
            driver._discover_bot_prs()

        assert not any("has no user.login" in record.message for record in caplog.records), (
            "Warning must not fire when viewer filter is bypassed"
        )
