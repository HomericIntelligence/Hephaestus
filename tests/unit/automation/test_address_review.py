"""Tests for the AddressReviewer automation (address_review.py)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.address_review import AddressReviewer
from hephaestus.automation.models import AddressReviewOptions

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_options() -> AddressReviewOptions:
    """Create AddressReviewOptions with minimal workers and no UI."""
    return AddressReviewOptions(
        issues=[123],
        max_workers=1,
        dry_run=False,
        enable_ui=False,
        resume_impl_session=True,
    )


@pytest.fixture
def reviewer(mock_options: AddressReviewOptions, tmp_path: Path) -> AddressReviewer:
    """Create an AddressReviewer with mocked repo root pointing to tmp_path."""
    with (
        patch("hephaestus.automation.address_review.get_repo_root", return_value=tmp_path),
        patch("hephaestus.automation.address_review.WorktreeManager"),
        patch("hephaestus.automation.address_review.StatusTracker"),
    ):
        ar = AddressReviewer(mock_options)
        ar.state_dir = tmp_path  # point state writes to tmp
        return ar


# ---------------------------------------------------------------------------
# _load_impl_session_id
# ---------------------------------------------------------------------------


class TestLoadImplSessionId:
    """Tests for _load_impl_session_id method."""

    def test_load_impl_session_id_found(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """State file exists with session_id → returns it."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(json.dumps({"session_id": "abc-session-123"}))
        reviewer.state_dir = tmp_path

        result = reviewer._load_impl_session_id(123)

        assert result == "abc-session-123"

    def test_load_impl_session_id_missing_file(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """No state file → returns None."""
        reviewer.state_dir = tmp_path  # empty dir

        result = reviewer._load_impl_session_id(123)

        assert result is None

    def test_load_impl_session_id_null(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """State file has session_id=null → returns None."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(json.dumps({"session_id": None}))
        reviewer.state_dir = tmp_path

        result = reviewer._load_impl_session_id(123)

        assert result is None

    def test_load_impl_session_id_no_key(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """State file has no session_id key → returns None."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(json.dumps({"phase": "completed"}))
        reviewer.state_dir = tmp_path

        result = reviewer._load_impl_session_id(123)

        assert result is None


# ---------------------------------------------------------------------------
# _parse_json_block (instance method on AddressReviewer)
# ---------------------------------------------------------------------------


class TestParseJsonBlock:
    """Tests for AddressReviewer._parse_json_block."""

    def test_extracts_last_json_block(self, reviewer: AddressReviewer) -> None:
        """Returns last parsed JSON block from Claude output."""
        payload = {"addressed": ["thread-1", "thread-2"], "replies": {"thread-1": "Fixed"}}
        text = (
            "Some output\n"
            "```json\n"
            '{"addressed": ["old"], "replies": {}}\n'
            "```\n"
            "More output\n"
            "```json\n" + json.dumps(payload) + "\n```"
        )
        result = reviewer._parse_json_block(text)
        assert result["addressed"] == ["thread-1", "thread-2"]

    def test_no_block_returns_defaults(self, reviewer: AddressReviewer) -> None:
        """No json block → returns defaults with empty addressed list."""
        result = reviewer._parse_json_block("No json here.")
        assert result == {"addressed": [], "replies": {}}

    def test_invalid_json_returns_defaults(self, reviewer: AddressReviewer) -> None:
        """Invalid json block → returns defaults."""
        result = reviewer._parse_json_block("```json\n{broken!!}\n```")
        assert result == {"addressed": [], "replies": {}}


# ---------------------------------------------------------------------------
# _resolve_addressed_threads
# ---------------------------------------------------------------------------


class TestResolveAddressedThreads:
    """Tests for _resolve_addressed_threads method."""

    def test_resolve_only_addressed_threads(self, reviewer: AddressReviewer) -> None:
        """Claude reports [id1] addressed, [id2] not → only id1 resolved."""
        addressed = ["thread-id-1"]
        replies: dict[str, str] = {"thread-id-1": "Fixed the issue"}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            reviewer._resolve_addressed_threads(addressed, replies)

        mock_resolve.assert_called_once_with("thread-id-1", "Fixed the issue", dry_run=False)

    def test_resolve_multiple_addressed_threads(self, reviewer: AddressReviewer) -> None:
        """All addressed threads are resolved, non-addressed ones are skipped."""
        addressed = ["thread-1", "thread-2"]
        replies: dict[str, str] = {
            "thread-1": "Renamed variable",
            "thread-2": "Added type hint",
        }

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            reviewer._resolve_addressed_threads(addressed, replies)

        assert mock_resolve.call_count == 2
        called_ids = {call[0][0] for call in mock_resolve.call_args_list}
        assert called_ids == {"thread-1", "thread-2"}

    def test_skips_resolve_on_failure(self, reviewer: AddressReviewer) -> None:
        """Individual resolve failures do not abort the rest."""
        addressed = ["thread-1", "thread-2"]
        replies: dict[str, str] = {}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            mock_resolve.side_effect = [RuntimeError("API error"), None]
            # Should not raise
            reviewer._resolve_addressed_threads(addressed, replies)

        assert mock_resolve.call_count == 2

    def test_dry_run_no_resolve(self, mock_options: AddressReviewOptions, tmp_path: Path) -> None:
        """dry_run=True → gh_pr_resolve_thread never called."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.address_review.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.address_review.WorktreeManager"),
            patch("hephaestus.automation.address_review.StatusTracker"),
        ):
            dry_reviewer = AddressReviewer(mock_options)
            dry_reviewer.state_dir = tmp_path

        addressed = ["thread-1"]
        replies: dict[str, str] = {"thread-1": "Fixed"}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            # dry_run is passed through from options via _resolve_addressed_threads
            # The method itself passes dry_run=self.options.dry_run
            dry_reviewer._resolve_addressed_threads(addressed, replies)

        # With dry_run=True, gh_pr_resolve_thread is called but internally is a no-op;
        # we verify the dry_run flag is forwarded correctly.
        mock_resolve.assert_called_once_with("thread-1", "Fixed", dry_run=True)


# ---------------------------------------------------------------------------
# _address_issue integration
# ---------------------------------------------------------------------------


class TestAddressIssue:
    """Integration-level tests for _address_issue method."""

    def test_no_unresolved_threads_skips(self, reviewer: AddressReviewer) -> None:
        """gh_pr_list_unresolved_threads returns [] → skip gracefully."""
        with patch(
            "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
            return_value=[],
        ):
            result = reviewer._address_issue(123, 456, 0)

        assert result.success is True
        assert result.pr_number == 456

    def test_dry_run_stops_before_resolve(
        self, mock_options: AddressReviewOptions, tmp_path: Path
    ) -> None:
        """dry_run=True → no resolve or push calls."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.address_review.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.address_review.WorktreeManager") as mock_wm_cls,
            patch("hephaestus.automation.address_review.StatusTracker"),
        ):
            mock_wm = MagicMock()
            mock_wm.create_worktree.return_value = tmp_path
            mock_wm_cls.return_value = mock_wm

            dry_reviewer = AddressReviewer(mock_options)
            dry_reviewer.state_dir = tmp_path

        threads = [{"id": "thread-1", "path": "foo.py", "line": 5, "body": "Fix this"}]

        with (
            patch.object(dry_reviewer, "_find_pr_for_issue", return_value=42),
            patch(
                "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch.object(dry_reviewer, "_load_impl_session_id", return_value=None),
            patch.object(dry_reviewer, "_load_review_state", return_value=None),
            patch.object(dry_reviewer, "_get_or_create_worktree", return_value=tmp_path),
            patch.object(
                dry_reviewer,
                "_run_fix_session",
                return_value={"addressed": ["thread-1"], "replies": {}},
            ),
            patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve,
            patch.object(dry_reviewer, "_push_branch") as mock_push,
        ):
            result = dry_reviewer._address_issue(123, 456, 0)

        assert result.success is True
        mock_resolve.assert_not_called()
        mock_push.assert_not_called()

    def test_no_pr_found_skips_run(self, reviewer: AddressReviewer) -> None:
        """No PR for any issue → run() returns {} without launching any workers."""
        with patch.object(reviewer, "_find_pr_for_issue", return_value=None):
            results = reviewer.run()

        assert results == {}
