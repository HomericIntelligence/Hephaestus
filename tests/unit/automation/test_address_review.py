"""Tests for the AddressReviewer automation (address_review.py)."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.runtime import AgentRunResult
from hephaestus.automation.address_review import (
    AddressReviewer,
    _parse_addressed_block,
    resolve_addressed_threads,
    run_address_fix_session,
)
from hephaestus.automation.models import AddressReviewOptions, ReviewPhase, ReviewState

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


class TestAddressReviewParseTracing:
    """AddressReviewer keeps standalone trace diagnostics through the shared parser."""

    def test_missing_block_writes_address_trace_and_warning(
        self,
        reviewer: AddressReviewer,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reviewer.state_dir = tmp_path

        def fake_run_address_fix_session(**kwargs: Any) -> dict[str, Any]:
            return kwargs["parse_fn"]("No json here.")

        with patch(
            "hephaestus.automation.address_review.run_address_fix_session",
            side_effect=fake_run_address_fix_session,
        ):
            result = reviewer._run_fix_session(123, 456, tmp_path, [], session_id=None)

        assert result == {"addressed": [], "replies": {}}
        trace = tmp_path / "address-123.parse-error.log"
        assert trace.exists()
        assert "reason: no fenced ```json block found in response" in trace.read_text()
        assert "Issue #123: address-review JSON parse failed" in caplog.text

    def test_invalid_block_writes_last_block_in_address_trace(
        self,
        reviewer: AddressReviewer,
        tmp_path: Path,
    ) -> None:
        reviewer.state_dir = tmp_path
        text = "```json\n{broken!!}\n```"

        def fake_run_address_fix_session(**kwargs: Any) -> dict[str, Any]:
            return kwargs["parse_fn"](text)

        with patch(
            "hephaestus.automation.address_review.run_address_fix_session",
            side_effect=fake_run_address_fix_session,
        ):
            result = reviewer._run_fix_session(123, 456, tmp_path, [], session_id=None)

        assert result == {"addressed": [], "replies": {}}
        payload = (tmp_path / "address-123.parse-error.log").read_text()
        assert "reason: json.JSONDecodeError:" in payload
        assert "=== last fenced block (if any) ===\n{broken!!}" in payload
        assert "=== full response ===\n" in payload


def test_codex_fix_session_falls_back_to_fresh_on_resume_failure(
    reviewer: AddressReviewer,
    tmp_path: Path,
) -> None:
    """Codex review repair should retry fresh when a saved session cannot resume."""
    reviewer.options.agent = "codex"
    threads = [{"id": "thread-1", "path": "file.py", "line": 10, "body": "fix this"}]
    resume_error = subprocess.CalledProcessError(
        1,
        ["codex"],
        stderr="session not found",
    )
    fresh_result = AgentRunResult(
        stdout='```json\n{"addressed": ["thread-1"], "replies": {}}\n```',
        stderr="",
        session_id="fresh-session",
    )

    with (
        patch(
            "hephaestus.automation.address_review.resume_agent_session",
            side_effect=resume_error,
        ),
        patch(
            "hephaestus.automation.address_review.run_agent_session",
            return_value=fresh_result,
        ) as mock_fresh,
    ):
        parsed = reviewer._run_fix_session(
            issue_number=123,
            pr_number=456,
            worktree_path=tmp_path,
            threads=threads,
            session_id="old-session",
        )

    assert parsed["addressed"] == ["thread-1"]
    mock_fresh.assert_called_once()


# ---------------------------------------------------------------------------
# _resolve_addressed_threads
# ---------------------------------------------------------------------------


class TestResolveAddressedThreads:
    """Tests for _resolve_addressed_threads method."""

    def test_resolve_only_addressed_threads(self, reviewer: AddressReviewer) -> None:
        """Claude reports [id1] addressed, [id2] not → only id1 resolved."""
        addressed = ["thread-id-1"]
        replies: dict[str, str] = {"thread-id-1": "Fixed the issue"}
        presented = {"thread-id-1", "thread-id-2"}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            reviewer._resolve_addressed_threads(addressed, replies, presented)

        mock_resolve.assert_called_once_with("thread-id-1", dry_run=False)

    def test_resolve_multiple_addressed_threads(self, reviewer: AddressReviewer) -> None:
        """All addressed threads present in the unresolved set are resolved."""
        addressed = ["thread-1", "thread-2"]
        replies: dict[str, str] = {
            "thread-1": "Renamed variable",
            "thread-2": "Added type hint",
        }
        presented = {"thread-1", "thread-2"}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            reviewer._resolve_addressed_threads(addressed, replies, presented)

        assert mock_resolve.call_count == 2
        called_ids = {call[0][0] for call in mock_resolve.call_args_list}
        assert called_ids == {"thread-1", "thread-2"}

    def test_skips_resolve_on_failure(self, reviewer: AddressReviewer) -> None:
        """Individual resolve failures do not abort the rest."""
        addressed = ["thread-1", "thread-2"]
        replies: dict[str, str] = {}
        presented = {"thread-1", "thread-2"}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            mock_resolve.side_effect = [RuntimeError("API error"), None]
            # Should not raise
            reviewer._resolve_addressed_threads(addressed, replies, presented)

        assert mock_resolve.call_count == 2

    def test_skips_unknown_thread_ids(self, reviewer: AddressReviewer) -> None:
        """Thread IDs Claude returns that we never presented are dropped silently.

        This is the M2 trust-boundary safeguard: a hallucinated or cross-PR
        thread ID must NOT reach gh_pr_resolve_thread.
        """
        addressed = ["thread-real", "thread-hallucinated"]
        replies: dict[str, str] = {
            "thread-real": "Fixed",
            "thread-hallucinated": "Pretended to fix",
        }
        presented = {"thread-real"}  # only the real one was on this PR

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            reviewer._resolve_addressed_threads(addressed, replies, presented)

        mock_resolve.assert_called_once_with("thread-real", dry_run=False)

    def test_dry_run_no_resolve(self, mock_options: AddressReviewOptions, tmp_path: Path) -> None:
        """dry_run=True → gh_pr_resolve_thread is called with dry_run=True."""
        mock_options.dry_run = True

        dry_reviewer = AddressReviewer(
            mock_options,
            get_repo_root=lambda: tmp_path,
            worktree_manager_factory=MagicMock(return_value=MagicMock()),
            status_tracker_factory=MagicMock(return_value=MagicMock()),
            log_manager_factory=MagicMock(return_value=MagicMock()),
        )
        dry_reviewer.state_dir = tmp_path

        addressed = ["thread-1"]
        replies: dict[str, str] = {"thread-1": "Fixed"}
        presented = {"thread-1"}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            # dry_run is forwarded from options via _resolve_addressed_threads
            dry_reviewer._resolve_addressed_threads(addressed, replies, presented)

        # With dry_run=True, gh_pr_resolve_thread is called but internally is a no-op;
        # we verify the dry_run flag is forwarded correctly.
        mock_resolve.assert_called_once_with("thread-1", dry_run=True)


class TestCommitIfChanges:
    """Tests for AddressReviewer._commit_if_changes."""

    def test_forwards_selected_agent_to_git_utils(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        reviewer.options.agent = "codex"

        with patch(
            "hephaestus.automation.address_review.commit_if_changes",
            return_value=True,
        ) as mock_commit:
            reviewer._commit_if_changes(123, tmp_path)

        mock_commit.assert_called_once_with(
            123,
            tmp_path,
            "codex",
            committed_log_message="Committed fix changes for issue #%s",
        )


# ---------------------------------------------------------------------------
# _address_issue integration
# ---------------------------------------------------------------------------


class TestAddressIssue:
    """Integration-level tests for _address_issue method."""

    def test_no_unresolved_threads_skips(self, reviewer: AddressReviewer) -> None:
        """gh_pr_list_unresolved_threads returns [] → skip gracefully."""
        with (
            patch.object(reviewer.status_tracker, "acquire_slot", return_value=0),
            patch(
                "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
        ):
            result = reviewer._address_issue(123, 456)

        assert result.success is True
        assert result.pr_number == 456

    def test_dry_run_stops_before_resolve(
        self, mock_options: AddressReviewOptions, tmp_path: Path
    ) -> None:
        """dry_run=True → no worktree creation, no resolve, no push calls."""
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

        # Dry-run guard now fires BEFORE worktree creation, so we only need
        # gh_pr_list_unresolved_threads to return some threads (the guard
        # skips the rest of the flow).
        threads = [{"id": "thread-1", "path": "foo.py", "line": 5, "body": "Fix this"}]

        with (
            patch.object(dry_reviewer.status_tracker, "acquire_slot", return_value=0),
            patch(
                "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch.object(dry_reviewer, "_get_or_create_worktree") as mock_worktree,
            patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve,
            patch.object(dry_reviewer, "_push_branch") as mock_push,
        ):
            result = dry_reviewer._address_issue(123, 456)

        assert result.success is True
        # Dry-run guard fires before worktree creation and resolution
        mock_worktree.assert_not_called()
        mock_resolve.assert_not_called()
        mock_push.assert_not_called()

    def test_no_pr_found_skips_run(self, reviewer: AddressReviewer) -> None:
        """No PR for any issue → run() returns {} without launching any workers."""
        with patch("hephaestus.automation.address_review.find_pr_for_issue", return_value=None):
            results = reviewer.run()

        assert results == {}


# ---------------------------------------------------------------------------
# #382/A4-06: AddressReviewer.run() must report preserved worktrees after cleanup_all
# ---------------------------------------------------------------------------


class TestAddressReviewerPreservedReporting:
    """Tests that AddressReviewer.run() logs preserved worktrees (#382/A4-06).

    Note: cleanup_all is only reached after the _address_all() call completes.
    The early-return for no-PR cases bypasses the try/finally intentionally.
    Tests must supply a non-empty pr_map so the code reaches the finally block.
    """

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
        """If cleanup_all preserves dirty worktrees, they are logged at INFO."""
        import logging

        preserved_path = tmp_path / "issue-1"
        ar, _ = self._make_reviewer_with_mock_wm(mock_options, tmp_path, [(1, preserved_path)])

        with (
            # Provide a non-empty pr_map so we reach the finally block
            patch.object(ar, "_discover_prs", return_value={123: 456}),
            patch.object(
                ar,
                "_address_all",
                return_value={123: MagicMock(success=True)},
            ),
            caplog.at_level(logging.INFO, logger="hephaestus.automation.address_review"),
        ):
            ar.run()

        logs = caplog.text
        assert "Preserved worktrees" in logs
        assert str(preserved_path) in logs

    def test_cleanup_all_called_when_prs_exist(
        self,
        mock_options: AddressReviewOptions,
        tmp_path: Path,
    ) -> None:
        """cleanup_all() is called when there are PRs to process."""
        ar, mock_wm = self._make_reviewer_with_mock_wm(mock_options, tmp_path, [])

        with (
            patch.object(ar, "_discover_prs", return_value={123: 456}),
            patch.object(ar, "_address_all", return_value={}),
        ):
            ar.run()

        mock_wm.cleanup_all.assert_called_once()


# ---------------------------------------------------------------------------
# Extracted module-level cores (Stage 2, #28) shared with the implementer loop
# ---------------------------------------------------------------------------


class TestParseAddressedBlock:
    """_parse_addressed_block is the trace-free shared JSON parser."""

    def test_extracts_last_block(self) -> None:
        payload = {"addressed": ["t1"], "replies": {"t1": "fixed"}}
        text = "```json\n{}\n```\nmore\n```json\n" + json.dumps(payload) + "\n```"
        assert _parse_addressed_block(text)["addressed"] == ["t1"]

    def test_no_block_defaults(self) -> None:
        assert _parse_addressed_block("no json") == {"addressed": [], "replies": {}}

    def test_invalid_json_defaults(self) -> None:
        assert _parse_addressed_block("```json\n{bad}\n```") == {"addressed": [], "replies": {}}


class TestResolveAddressedThreadsModuleLevel:
    """Module-level resolve_addressed_threads keeps the #661 hallucination guard."""

    def test_resolves_only_presented_threads(self) -> None:
        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            resolve_addressed_threads(
                ["t-real", "t-hallucinated"],
                {"t-real": "fixed"},
                {"t-real"},
                dry_run=False,
            )
        # The hallucinated id (not in the presented set) must NOT be resolved.
        mock_resolve.assert_called_once_with("t-real", dry_run=False)

    def test_forwards_dry_run(self) -> None:
        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            resolve_addressed_threads(["t1"], {"t1": "r"}, {"t1"}, dry_run=True)
        mock_resolve.assert_called_once_with("t1", dry_run=True)


class TestRunAddressFixSessionModuleLevel:
    """run_address_fix_session is the shared fix-session core; resumes AGENT_IMPLEMENTER."""

    def test_dry_run_returns_empty(self, tmp_path: Path) -> None:
        out = run_address_fix_session(
            issue_number=1,
            pr_number=42,
            worktree_path=tmp_path,
            threads=[{"id": "t1", "path": "a.py", "line": 1, "body": "fix"}],
            agent="claude",
            repo_root=tmp_path,
            parse_fn=_parse_addressed_block,
            log_file=tmp_path / "log.txt",
            dry_run=True,
        )
        assert out == {"addressed": [], "replies": {}}

    def test_claude_path_resumes_implementer_session(self, tmp_path: Path) -> None:
        """The Claude path invokes the implementer session (Session 2) and parses output."""
        from hephaestus.automation.session_naming import AGENT_IMPLEMENTER

        captured: dict[str, str] = {}

        def _fake_invoke(*, agent: str, **_: object) -> tuple[str, str]:
            captured["agent"] = agent
            return (
                '{"result": "```json\\n{\\"addressed\\": [\\"t1\\"], \\"replies\\": {}}\\n```"}',
                "",
            )

        with (
            patch("hephaestus.automation.address_review.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.address_review.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
        ):
            out = run_address_fix_session(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[{"id": "t1", "path": "a.py", "line": 1, "body": "fix"}],
                agent="claude",
                repo_root=tmp_path,
                parse_fn=_parse_addressed_block,
                log_file=tmp_path / "log.txt",
                dry_run=False,
            )

        assert out["addressed"] == ["t1"]
        # Fixes land in the long-lived implementer session, not a fresh one.
        assert captured["agent"] == AGENT_IMPLEMENTER

    def test_classifies_and_embeds_todo_list(self, tmp_path: Path) -> None:
        """The fix session classifies each comment and embeds a todo line.

        #1083: the difficulty-annotated todo line lands in the coordinator
        prompt.
        """
        prompts: dict[str, str] = {}

        def _fake_invoke(*, agent: str, prompt: str, **_: object) -> tuple[str, str]:
            prompts[agent] = prompt
            return ('{"result": "```json\\n{\\"addressed\\": [], \\"replies\\": {}}\\n```"}', "")

        with (
            patch("hephaestus.automation.address_review.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.address_review.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
            # Force the classifier result so the todo line's difficulty is known.
            patch(
                "hephaestus.automation.comment_difficulty._run_classifier_session",
                return_value={"t1": "hard"},
            ),
        ):
            run_address_fix_session(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[{"id": "t1", "path": "a.py", "line": 7, "body": "rework locking"}],
                agent="claude",
                repo_root=tmp_path,
                parse_fn=_parse_addressed_block,
                log_file=tmp_path / "log.txt",
                dry_run=False,
            )

        from hephaestus.automation.session_naming import AGENT_IMPLEMENTER

        coordinator_prompt = prompts[AGENT_IMPLEMENTER]
        assert "@ a.py Line 7 - hard - rework locking" in coordinator_prompt


# ---------------------------------------------------------------------------
# Extracted helpers: _check_threads_for_address, _setup_address_state,
# _commit_push_and_resolve (PR #1296 regression tests)
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


class TestCommitPushAndResolve:
    """Tests for AddressReviewer._commit_push_and_resolve helper.

    The key regression test (PR #1296): _commit_push_and_resolve must pass
    the real replies dict (not {}) to _resolve_addressed_threads.
    """

    def test_passes_real_replies_dict_to_resolve(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """The helper must forward the REAL replies dict to _resolve_addressed_threads.

        This is the critical safeguard against the historically-buggy {}
        sentinel path. The replies dict carries reply text that must be posted
        as comments on each resolved thread per the review protocol.
        """
        review_state = ReviewState(
            issue_number=123,
            pr_number=456,
            branch_name="123-auto-impl",
        )

        addressed = ["t1", "t2"]
        replies = {"t1": "Fixed the locking issue.", "t2": "Added the type hint."}
        threads = [
            {"id": "t1", "path": "a.py", "line": 10, "body": "fix this"},
            {"id": "t2", "path": "b.py", "line": 20, "body": "and this"},
        ]

        with (
            patch.object(reviewer, "_commit_if_changes"),
            patch.object(reviewer, "_push_branch"),
            patch.object(reviewer, "_resolve_addressed_threads") as mock_resolve,
            patch.object(reviewer, "_save_review_state"),
            patch.object(reviewer.status_tracker, "update_slot"),
        ):
            reviewer._commit_push_and_resolve(
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

        # The critical assertion: _resolve_addressed_threads is called with the
        # REAL replies dict, not an empty dict sentinel.
        mock_resolve.assert_called_once_with(
            addressed,
            replies,
            {"t1", "t2"},
        )

    def test_updates_review_state_addressed_threads(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """The helper updates review_state.addressed_thread_ids."""
        review_state = ReviewState(
            issue_number=123,
            pr_number=456,
            branch_name="123-auto-impl",
            addressed_thread_ids=["old-t1"],
        )

        addressed = ["t1"]
        replies = {"t1": "Fixed."}
        threads = [{"id": "t1", "path": "a.py", "line": 10, "body": "fix"}]

        with (
            patch.object(reviewer, "_commit_if_changes"),
            patch.object(reviewer, "_push_branch"),
            patch.object(reviewer, "_resolve_addressed_threads"),
            patch.object(reviewer, "_save_review_state") as mock_save,
            patch.object(reviewer.status_tracker, "update_slot"),
        ):
            reviewer._commit_push_and_resolve(
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

        # Check that the state was updated with the new addressed thread IDs
        assert "old-t1" in review_state.addressed_thread_ids
        assert "t1" in review_state.addressed_thread_ids
        assert review_state.phase == ReviewPhase.COMPLETED
        mock_save.assert_called_once()
