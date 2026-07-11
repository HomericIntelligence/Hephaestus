"""Unit tests for PRDiscovery collaborator (refs #1179)."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pr_discovery import PRDiscovery


@pytest.fixture()
def discovery() -> PRDiscovery:
    """Return a PRDiscovery wired with test doubles."""
    options_mock = MagicMock()
    options_mock.include_all_authors = False
    return PRDiscovery(
        options_provider=lambda: options_mock,
        status_tracker_provider=MagicMock,
        repo_root_provider=MagicMock(side_effect=MagicMock),
    )


class TestResolveViewerLogin:
    """Tests for PRDiscovery.resolve_viewer_login."""

    def test_caches_login_on_success(self, discovery: PRDiscovery) -> None:
        discovery._viewer_login = ""
        with patch(
            "hephaestus.automation.pr_discovery._gh_call",
            return_value=MagicMock(stdout="testuser\n"),
        ) as mock_gh:
            assert discovery.resolve_viewer_login() == "testuser"
            assert discovery.resolve_viewer_login() == "testuser"
        assert mock_gh.call_count == 1  # cached on second call

    def test_empty_stdout_raises(self, discovery: PRDiscovery) -> None:
        discovery._viewer_login = ""
        with (
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=MagicMock(stdout=""),
            ),
            pytest.raises(RuntimeError, match="empty response"),
        ):
            discovery.resolve_viewer_login()

    def test_subprocess_error_raises(self, discovery: PRDiscovery) -> None:
        discovery._viewer_login = ""
        with (
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                side_effect=subprocess.CalledProcessError(1, ["gh"]),
            ),
            pytest.raises(RuntimeError, match="Could not resolve viewer login"),
        ):
            discovery.resolve_viewer_login()

    def test_already_cached_skips_gh_call(self, discovery: PRDiscovery) -> None:
        discovery._viewer_login = "cached"
        with patch("hephaestus.automation.pr_discovery._gh_call") as mock_gh:
            result = discovery.resolve_viewer_login()
        assert result == "cached"
        mock_gh.assert_not_called()


class TestIsBotPrMode:
    """Tests for PRDiscovery.is_bot_pr_mode."""

    def test_equal_numbers_returns_true(self, discovery: PRDiscovery) -> None:
        assert discovery.is_bot_pr_mode(42, 42) is True

    def test_different_numbers_returns_false(self, discovery: PRDiscovery) -> None:
        assert discovery.is_bot_pr_mode(1, 42) is False

    def test_zero_zero_returns_true(self, discovery: PRDiscovery) -> None:
        assert discovery.is_bot_pr_mode(0, 0) is True


class TestDiscoverBotPrs:
    """Tests for PRDiscovery.discover_bot_prs."""

    def test_returns_bot_prs_keyed_by_pr_number(self, discovery: PRDiscovery) -> None:
        pulls: list[dict[str, Any]] = [
            {"number": 10, "user": {"type": "Bot", "login": "depbot"}},
            {"number": 11, "user": {"type": "User", "login": "human"}},
        ]
        discovery._options().include_all_authors = True
        with (
            patch("hephaestus.automation.pr_discovery.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=MagicMock(returncode=0, stdout=json.dumps(pulls)),
            ),
        ):
            result = discovery.discover_bot_prs()
        assert result == {10: 10}

    def test_empty_on_get_repo_info_failure(self, discovery: PRDiscovery) -> None:
        with patch(
            "hephaestus.automation.pr_discovery.get_repo_info",
            side_effect=RuntimeError("no repo"),
        ):
            result = discovery.discover_bot_prs()
        assert result == {}

    def test_empty_on_gh_call_failure(self, discovery: PRDiscovery) -> None:
        discovery._options().include_all_authors = True
        with (
            patch("hephaestus.automation.pr_discovery.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                side_effect=subprocess.CalledProcessError(1, ["gh"]),
            ),
        ):
            result = discovery.discover_bot_prs()
        assert result == {}


class TestDiscoverFailingPrs:
    """Tests for PRDiscovery.discover_failing_prs."""

    def test_returns_matching_prs(self, discovery: PRDiscovery) -> None:
        pulls = [
            {
                "number": 5,
                "isDraft": False,
                "statusCheckRollup": None,
                "mergeStateStatus": "BLOCKED",
            },
            {
                "number": 7,
                "isDraft": True,
                "statusCheckRollup": None,
                "mergeStateStatus": "BLOCKED",
            },
        ]
        with (
            patch("hephaestus.automation.pr_discovery.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=MagicMock(returncode=0, stdout=json.dumps(pulls)),
            ),
        ):
            result = discovery.discover_failing_prs(lambda pr: not pr.get("isDraft"))
        assert result == {5: 5}

    def test_empty_on_repo_info_failure(self, discovery: PRDiscovery) -> None:
        with patch(
            "hephaestus.automation.pr_discovery.get_repo_info",
            side_effect=RuntimeError("no remote"),
        ):
            result = discovery.discover_failing_prs(lambda pr: True)
        assert result == {}


class TestValidatePrOpen:
    """Tests for direct-PR validation before the final containment sweep."""

    def test_non_object_response_returns_false(self, discovery: PRDiscovery) -> None:
        """Malformed JSON cannot abort the run before final containment executes."""
        with patch(
            "hephaestus.automation.pr_discovery._gh_call",
            return_value=MagicMock(returncode=0, stdout="[]"),
        ):
            assert discovery.validate_pr_open(42) is False


class TestListOpenPrsRemaining:
    """Tests for the final open-PR discovery used by containment."""

    @pytest.mark.parametrize(
        "result",
        [
            MagicMock(returncode=1, stdout="", stderr="gh failed"),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="{}", stderr=""),
        ],
        ids=["nonzero-exit", "empty-output", "non-list-json"],
    )
    def test_malformed_lookup_returns_unknown_sentinel(
        self, discovery: PRDiscovery, result: MagicMock
    ) -> None:
        """A failed final-sweep lookup cannot be reinterpreted as a clean repo."""
        with (
            patch("hephaestus.automation.pr_discovery.get_repo_info", return_value=("o", "r")),
            patch("hephaestus.automation.pr_discovery._gh_call", return_value=result),
        ):
            remaining = discovery.list_open_prs_remaining()

        assert remaining == [{"number": -1, "title": "(unknown: gh api pulls failed)"}]

    def test_malformed_nested_row_preserves_the_pr_for_containment(
        self, discovery: PRDiscovery
    ) -> None:
        """Malformed nested REST fields cannot hide a known open PR from the sweep."""
        discovery._options().include_all_authors = True
        discovery._pr_merge_state_fn = lambda _number: ("", "")
        raw_pr = {
            "number": 42,
            "title": "malformed fields",
            "head": "not-an-object",
            "labels": "not-a-list",
            "user": "not-an-object",
            "auto_merge": {"enabledAt": "now"},
        }
        with (
            patch("hephaestus.automation.pr_discovery.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=MagicMock(returncode=0, stdout=json.dumps([raw_pr])),
            ),
        ):
            remaining = discovery.list_open_prs_remaining()

        assert remaining[0]["number"] == 42
        assert remaining[0]["headRefName"] == ""
        assert remaining[0]["labels"] == []

    def test_mixed_malformed_rows_preserve_valid_prs_and_an_unknown_sentinel(
        self, discovery: PRDiscovery
    ) -> None:
        """A malformed sibling cannot hide known PRs from final containment."""
        discovery._options().include_all_authors = True
        discovery._pr_merge_state_fn = lambda _number: ("", "")
        raw_pr = {
            "number": 42,
            "title": "armed PR",
            "head": {"ref": "feature"},
            "labels": [],
            "user": {"login": "viewer", "type": "User"},
            "auto_merge": {"enabledAt": "now"},
        }
        with (
            patch("hephaestus.automation.pr_discovery.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=MagicMock(returncode=0, stdout=json.dumps([raw_pr, "malformed"])),
            ),
        ):
            remaining = discovery.list_open_prs_remaining()

        assert [pr["number"] for pr in remaining] == [42, -1]
        assert remaining[-1]["title"] == "(unknown: malformed gh api pull row)"

    def test_explicit_direct_pr_scope_keeps_other_author_for_containment(
        self, discovery: PRDiscovery
    ) -> None:
        """An explicit ``--prs`` target must not be dropped by the author filter."""
        discovery._options().include_all_authors = False
        discovery._options().prs = [42]
        discovery._viewer_login = "viewer"
        discovery._pr_merge_state_fn = lambda _number: ("", "")
        raw_pr = {
            "number": 42,
            "title": "teammate PR",
            "head": {"ref": "teammate-head"},
            "labels": [],
            "user": {"login": "teammate", "type": "User"},
            "auto_merge": {"enabledAt": "now"},
        }
        with (
            patch("hephaestus.automation.pr_discovery.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=MagicMock(returncode=0, stdout=json.dumps([raw_pr])),
            ),
        ):
            remaining = discovery.list_open_prs_remaining()

        assert [pr["number"] for pr in remaining] == [42]

    def test_malformed_merge_state_preserves_the_pr_for_containment(
        self, discovery: PRDiscovery
    ) -> None:
        """A non-object merge-state response is unknown, not a final-sweep exception."""
        discovery._options().include_all_authors = True
        raw_pr = {
            "number": 42,
            "title": "armed PR",
            "head": {"ref": "feature"},
            "labels": [],
            "user": {"login": "viewer", "type": "User"},
            "auto_merge": {"enabledAt": "now"},
        }

        def fake_gh_call(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv[:2] == ["api", "--paginate"]:
                return MagicMock(returncode=0, stdout=json.dumps([raw_pr]))
            assert argv[:2] == ["pr", "view"]
            return MagicMock(returncode=0, stdout="null")

        with (
            patch("hephaestus.automation.pr_discovery.get_repo_info", return_value=("o", "r")),
            patch("hephaestus.automation.pr_discovery._gh_call", side_effect=fake_gh_call),
        ):
            remaining = discovery.list_open_prs_remaining()

        assert remaining[0]["number"] == 42
        assert remaining[0]["mergeStateStatus"] == ""
