"""Tests for the AddressReviewer automation (address_review.py)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.address_review import AddressReviewer
from hephaestus.automation.models import AddressReviewOptions, ReviewState

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
def base_deps(tmp_path: Path) -> dict:
    """Return constructor injection kwargs for AddressReviewer / PRReviewer."""
    return {
        "get_repo_root": lambda: tmp_path,
        "worktree_manager_factory": MagicMock(return_value=MagicMock()),
        "status_tracker_factory": MagicMock(return_value=MagicMock()),
        "log_manager_factory": MagicMock(return_value=MagicMock()),
    }


@pytest.fixture
def reviewer(
    mock_options: AddressReviewOptions, base_deps: dict, tmp_path: Path
) -> AddressReviewer:
    """Create an AddressReviewer with mocked collaborators pointing to tmp_path."""
    ar = AddressReviewer(mock_options, **base_deps)
    ar.state_dir = tmp_path  # point state writes to tmp
    return ar


# ---------------------------------------------------------------------------
# _load_impl_session_id
# ---------------------------------------------------------------------------


class TestLoadImplSessionId:
    """Tests for _load_impl_session_id method."""

    def test_load_impl_session_id_found(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """Legacy state file exists with Claude session_id → returns it for Claude."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(json.dumps({"session_id": "abc-session-123"}))
        reviewer.state_dir = tmp_path

        result = reviewer._load_impl_session_id(123)

        assert result == "abc-session-123"

    def test_load_impl_session_id_skips_legacy_session_for_codex(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """Legacy state files contain Claude sessions and must not resume as Codex."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(json.dumps({"session_id": "abc-session-123"}))
        reviewer.state_dir = tmp_path
        reviewer.options.agent = "codex"

        result = reviewer._load_impl_session_id(123)

        assert result is None

    def test_load_impl_session_id_returns_matching_codex_session(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """Provider metadata allows Codex sessions to be resumed by Codex."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(
            json.dumps({"session_id": "codex-session-123", "session_agent": "codex"})
        )
        reviewer.state_dir = tmp_path
        reviewer.options.agent = "codex"

        result = reviewer._load_impl_session_id(123)

        assert result == "codex-session-123"

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
# Address-review parse tracing
# ---------------------------------------------------------------------------


class TestAddressReviewRetirement:
    """The standalone surface cannot start a partial remediation lifecycle."""

    def test_missing_block_writes_address_trace_and_warning(
        self,
        reviewer: AddressReviewer,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        del tmp_path, caplog
        with pytest.raises(RuntimeError, match="address_review_retired_use_pipeline"):
            reviewer._run_fix_session(123, 456, Path("."), [], session_id=None)

    def test_invalid_block_writes_last_block_in_address_trace(
        self,
        reviewer: AddressReviewer,
        tmp_path: Path,
    ) -> None:
        del tmp_path
        with pytest.raises(RuntimeError, match="address_review_retired_use_pipeline"):
            reviewer._run_fix_session(123, 456, Path("."), [], session_id=None)


def test_codex_fix_session_is_retired_before_resume_attempt(
    reviewer: AddressReviewer,
    tmp_path: Path,
) -> None:
    """A saved session cannot bypass the pipeline's thread lifecycle."""
    reviewer.options.agent = "codex"
    threads = [{"id": "thread-1", "path": "file.py", "line": 10, "body": "fix this"}]
    with (
        patch(
            "hephaestus.automation.address_review.resume_agent_session",
        ) as mock_resume,
    ):
        with pytest.raises(RuntimeError, match="address_review_retired_use_pipeline"):
            reviewer._run_fix_session(123, 456, tmp_path, threads, session_id="old-session")

    mock_resume.assert_not_called()


class TestCommitIfChanges:
    """Tests for AddressReviewer._commit_if_changes."""

    def test_forwards_selected_agent_to_git_utils(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        reviewer.options.agent = "codex"

        with pytest.raises(RuntimeError, match="address_review_retired_use_pipeline"):
            reviewer._commit_if_changes(123, tmp_path)


# ---------------------------------------------------------------------------
# _address_issue integration
# ---------------------------------------------------------------------------


class TestAddressIssue:
    """Integration-level tests for _address_issue method."""

    def test_no_unresolved_threads_skips(self, reviewer: AddressReviewer) -> None:
        """The retired command rejects work before any thread read or mutation."""
        result = reviewer._address_issue(123, 456)

        assert result.success is False
        assert result.error == "address_review_retired_use_pipeline"

    def test_dry_run_stops_before_thread_handoff(
        self, mock_options: AddressReviewOptions, tmp_path: Path
    ) -> None:
        """Dry run cannot revive the retired standalone workflow."""
        mock_options.dry_run = True

        mock_wm = MagicMock()
        mock_wm.create_worktree.return_value = tmp_path
        dry_reviewer = AddressReviewer(
            mock_options,
            get_repo_root=lambda: tmp_path,
            worktree_manager_factory=MagicMock(return_value=mock_wm),
            status_tracker_factory=MagicMock(return_value=MagicMock()),
            log_manager_factory=MagicMock(return_value=MagicMock()),
        )
        dry_reviewer.state_dir = tmp_path

        with (
            patch.object(dry_reviewer, "_get_or_create_worktree") as mock_worktree,
            patch.object(dry_reviewer, "_push_branch") as mock_push,
        ):
            result = dry_reviewer._address_issue(123, 456)

        assert result.error == "address_review_retired_use_pipeline"
        mock_worktree.assert_not_called()
        mock_push.assert_not_called()

    def test_no_pr_found_skips_run(self, reviewer: AddressReviewer) -> None:
        """The public entry point reports a deliberate failure per requested issue."""
        results = reviewer.run()

        assert results[123].error == "address_review_retired_use_pipeline"

    def test_address_issue_claimed_fixes_require_reviewer_validation(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """No direct call can create a code-fix claim without a reply receipt."""
        with (
            patch.object(reviewer, "_run_fix_session") as mock_fix,
            patch.object(reviewer, "_commit_if_changes") as mock_commit,
        ):
            result = reviewer._address_issue(123, 456)

        assert result.success is False
        assert result.error == "address_review_retired_use_pipeline"
        mock_fix.assert_not_called()
        mock_commit.assert_not_called()


# ---------------------------------------------------------------------------
# #382/A4-06: AddressReviewer.run() must report preserved worktrees after cleanup_all
# ---------------------------------------------------------------------------


class TestAddressReviewerRetiredRun:
    """The retired command neither creates nor cleans up standalone worktrees."""

    def _make_reviewer_with_mock_wm(
        self,
        mock_options: AddressReviewOptions,
        tmp_path: Path,
        preserved: list,
    ) -> tuple["AddressReviewer", MagicMock]:
        """Create an AddressReviewer with a MagicMock WorktreeManager."""
        mock_wm = MagicMock()
        mock_wm.preserved = preserved
        ar = AddressReviewer(
            mock_options,
            get_repo_root=lambda: tmp_path,
            worktree_manager_factory=MagicMock(return_value=mock_wm),
            status_tracker_factory=MagicMock(return_value=MagicMock()),
            log_manager_factory=MagicMock(return_value=MagicMock()),
        )
        ar.state_dir = tmp_path
        return ar, mock_wm

    def test_preserved_worktrees_logged_after_cleanup(
        self,
        mock_options: AddressReviewOptions,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A retired run never reaches the old cleanup/reporting path."""
        import logging

        preserved_path = tmp_path / "issue-1"
        ar, _ = self._make_reviewer_with_mock_wm(mock_options, tmp_path, [(1, preserved_path)])

        with caplog.at_level(logging.INFO, logger="hephaestus.automation.address_review"):
            results = ar.run()

        assert results[123].error == "address_review_retired_use_pipeline"
        assert str(preserved_path) not in caplog.text

    def test_cleanup_all_called_when_prs_exist(
        self,
        mock_options: AddressReviewOptions,
        tmp_path: Path,
    ) -> None:
        """cleanup_all() is not called because no standalone worktree exists."""
        ar, mock_wm = self._make_reviewer_with_mock_wm(mock_options, tmp_path, [])

        ar.run()

        mock_wm.cleanup_all.assert_not_called()


# ---------------------------------------------------------------------------
# Extracted helpers: _check_threads_for_address, _setup_address_state,
# _commit_push_and_record
# ---------------------------------------------------------------------------


class TestCheckThreadsForAddress:
    """Tests for AddressReviewer._check_threads_for_address helper."""

    def test_no_threads_returns_none(self, reviewer: AddressReviewer) -> None:
        """No unresolved threads → returns None."""
        with patch(
            "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
            return_value=[],
        ):
            result = reviewer._check_threads_for_address(
                issue_number=123,
                pr_number=456,
                thread_id=1,
            )

        assert result is None

    def test_dry_run_returns_none(self, mock_options: AddressReviewOptions, tmp_path: Path) -> None:
        """dry_run=True → returns None (caller returns success)."""
        mock_options.dry_run = True

        dry_reviewer = AddressReviewer(
            mock_options,
            get_repo_root=lambda: tmp_path,
            worktree_manager_factory=MagicMock(return_value=MagicMock()),
            status_tracker_factory=MagicMock(return_value=MagicMock()),
            log_manager_factory=MagicMock(return_value=MagicMock()),
        )
        dry_reviewer.state_dir = tmp_path

        threads_list = [{"id": "thread-1", "path": "file.py", "line": 10, "body": "fix this"}]

        with patch(
            "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
            return_value=threads_list,
        ):
            result = dry_reviewer._check_threads_for_address(
                issue_number=123,
                pr_number=456,
                thread_id=1,
            )

        assert result is None

    def test_threads_present_returns_list(self, reviewer: AddressReviewer) -> None:
        """Threads exist and not dry-run → returns thread list."""
        threads_list = [
            {"id": "thread-1", "path": "file.py", "line": 10, "body": "fix this"},
            {"id": "thread-2", "path": "file.py", "line": 20, "body": "and this"},
        ]

        with patch(
            "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
            return_value=threads_list,
        ):
            result = reviewer._check_threads_for_address(
                issue_number=123,
                pr_number=456,
                thread_id=1,
            )

        assert result == threads_list
        assert len(result) == 2


class TestSetupAddressState:
    """Tests for AddressReviewer._setup_address_state helper."""

    def test_creates_new_review_state_when_none_exists(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """No existing state → creates new ReviewState."""
        with (
            patch.object(reviewer, "_load_impl_session_id", return_value=None),
            patch.object(reviewer, "_load_review_state", return_value=None),
            patch.object(
                reviewer,
                "_get_or_create_worktree",
                return_value=tmp_path / "worktree",
            ),
            patch.object(reviewer, "_save_review_state"),
            patch.object(reviewer.status_tracker, "update_slot"),
        ):
            session_id, review_state, branch_name, worktree_path = reviewer._setup_address_state(
                issue_number=123,
                pr_number=456,
                slot_id=0,
            )

        assert session_id is None
        assert review_state.issue_number == 123
        assert review_state.pr_number == 456
        assert review_state.branch_name == "123-auto-impl"
        assert branch_name == "123-auto-impl"
        assert worktree_path == tmp_path / "worktree"

    def test_updates_pr_number_on_existing_state(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """Existing state → updates pr_number."""
        existing_state = ReviewState(
            issue_number=123,
            pr_number=400,  # old pr_number
            branch_name="123-auto-impl",
        )

        with (
            patch.object(reviewer, "_load_impl_session_id", return_value="session-123"),
            patch.object(reviewer, "_load_review_state", return_value=existing_state),
            patch.object(
                reviewer,
                "_get_or_create_worktree",
                return_value=tmp_path / "worktree",
            ),
            patch.object(reviewer, "_save_review_state") as mock_save,
            patch.object(reviewer.status_tracker, "update_slot"),
        ):
            session_id, review_state, _branch_name, _worktree_path = reviewer._setup_address_state(
                issue_number=123,
                pr_number=456,
                slot_id=0,
            )

        # pr_number should be updated to the new value
        assert review_state.pr_number == 456
        assert session_id == "session-123"
        mock_save.assert_called_once()


class TestCommitPushAndRecord:
    """No standalone helper can commit/push a partial review lifecycle."""

    def test_updates_review_state_addressed_threads(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """The helper refuses a direct commit/push attempt."""
        review_state = ReviewState(
            issue_number=123,
            pr_number=456,
            branch_name="123-auto-impl",
            addressed_thread_ids=["old-t1"],
        )

        addressed = ["t1"]
        replies = {"t1": "Fixed."}
        threads = [{"id": "t1", "path": "a.py", "line": 10, "body": "fix"}]

        with pytest.raises(RuntimeError, match="address_review_retired_use_pipeline"):
            reviewer._commit_push_and_record(
                issue_number=123,
                pr_number=456,
                branch_name="123-auto-impl",
                worktree_path=tmp_path,
                addressed=addressed,
                replies=replies,
                threads=threads,
                review_state=review_state,
                slot_id=0,
                thread_id=1,
            )

        # No state is updated because the queue pipeline owns the full handoff.
        assert "old-t1" in review_state.addressed_thread_ids
        assert "t1" not in review_state.addressed_thread_ids
