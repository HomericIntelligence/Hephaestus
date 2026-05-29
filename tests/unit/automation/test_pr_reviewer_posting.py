"""Tests for the PRReviewer posting side (pr_reviewer.py)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.models import ReviewerOptions
from hephaestus.automation.pr_reviewer import (
    PRReviewer,
    _parse_json_block,
    gather_impl_review_context,
    review_pr_inline,
    run_pr_review_analysis,
)

# ---------------------------------------------------------------------------
# _parse_json_block (module-level function)
# ---------------------------------------------------------------------------


class TestParseJsonBlock:
    """Tests for the module-level _parse_json_block function."""

    def test_parse_json_block_extracts_last_block(self) -> None:
        """Multiple ```json blocks → returns last one parsed."""
        text = (
            "Some analysis\n"
            "```json\n"
            '{"comments": ["first"], "summary": "first"}\n'
            "```\n"
            "More text\n"
            "```json\n"
            '{"comments": ["second"], "summary": "second"}\n'
            "```"
        )
        result = _parse_json_block(text)
        assert result["summary"] == "second"
        assert result["comments"] == ["second"]

    def test_parse_json_block_no_block(self) -> None:
        """No json block → returns defaults with empty comments."""
        result = _parse_json_block("No json here at all.")
        assert result["comments"] == []
        assert "No structured output" in result["summary"]

    def test_parse_json_block_invalid_json(self) -> None:
        """Malformed json block → returns default dict."""
        text = "```json\n{invalid json!!!}\n```"
        result = _parse_json_block(text)
        assert result["comments"] == []
        assert "Failed to parse" in result["summary"]

    def test_parse_json_block_single_valid_block(self) -> None:
        """Single valid json block → returns parsed content."""
        comments = [{"path": "foo.py", "line": 10, "body": "Fix this"}]
        text = "```json\n" + json.dumps({"comments": comments, "summary": "Looks good"}) + "\n```"
        result = _parse_json_block(text)
        assert len(result["comments"]) == 1
        assert result["summary"] == "Looks good"


# ---------------------------------------------------------------------------
# PRReviewer fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_options() -> ReviewerOptions:
    """Create ReviewerOptions with UI and dry_run disabled."""
    return ReviewerOptions(
        issues=[123],
        max_workers=1,
        dry_run=False,
        enable_ui=False,
    )


@pytest.fixture
def reviewer(mock_options: ReviewerOptions, tmp_path: Path) -> PRReviewer:
    """Create PRReviewer with mocked repo root and state dir."""
    with (
        patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
        patch("hephaestus.automation.pr_reviewer.WorktreeManager"),
        patch("hephaestus.automation.pr_reviewer.StatusTracker"),
    ):
        return PRReviewer(mock_options)


# ---------------------------------------------------------------------------
# _find_pr_for_issue helpers
# ---------------------------------------------------------------------------


def _mock_no_pr() -> Any:
    """Return a mock _gh_call that reports no open PRs."""
    mock = MagicMock()
    mock.stdout = "[]"
    return mock


def _mock_pr_found(pr_number: int) -> Any:
    """Return a mock _gh_call result that reports one open PR."""
    mock = MagicMock()
    mock.stdout = json.dumps([{"number": pr_number}])
    return mock


# ---------------------------------------------------------------------------
# PRReviewer tests
# ---------------------------------------------------------------------------


class TestNoPrFoundSkipsGracefully:
    """Tests for graceful handling when no PR exists for an issue."""

    def test_no_pr_found_yields_failed_result_with_diagnostic(self, reviewer: PRReviewer) -> None:
        """A nonexistent PR (#0) makes _review_pr fail with a PR-diff diagnostic.

        The "skip when no PR" decision lives in run()/_discover_prs (covered by
        test_run_returns_empty_when_no_prs). When _review_pr is nonetheless
        handed a PR number that cannot be fetched, it must surface a failed
        WorkerResult that names the offending issue rather than crashing the
        worker thread.
        """
        with patch.object(reviewer, "_find_pr_for_issue", return_value=None):
            result = reviewer._review_pr(issue_number=123, pr_number=0)

        assert result.success is False
        assert result.issue_number == 123
        assert result.pr_number is None
        assert result.error is not None
        assert "#0" in result.error

    def test_run_returns_empty_when_no_prs(self, reviewer: PRReviewer) -> None:
        """run() returns {} when no PRs are discovered."""
        with patch.object(reviewer, "_discover_prs", return_value={}):
            results = reviewer.run()

        assert results == {}


class TestDryRunSkipsPost:
    """Tests for dry_run=True preventing actual posting."""

    def test_dry_run_skips_post(self, mock_options: ReviewerOptions, tmp_path: Path) -> None:
        """dry_run=True → gh_pr_review_post not called."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.WorktreeManager") as mock_wm_cls,
            patch("hephaestus.automation.pr_reviewer.StatusTracker"),
        ):
            mock_wm = MagicMock()
            mock_wm.create_worktree.return_value = tmp_path
            mock_wm_cls.return_value = mock_wm

            dry_reviewer = PRReviewer(mock_options)

        analysis = {"comments": [{"path": "a.py", "line": 1, "body": "fix this"}], "summary": "ok"}
        with (
            patch.object(dry_reviewer, "_gather_pr_context", return_value={}),
            patch.object(dry_reviewer, "_run_analysis_session", return_value=analysis),
            patch("hephaestus.automation.pr_reviewer.gh_pr_review_post") as mock_post,
        ):
            result = dry_reviewer._review_pr(issue_number=123, pr_number=42)

        assert result.success is True
        mock_post.assert_not_called()

    def test_dry_run_analysis_session_returns_placeholder(
        self, mock_options: ReviewerOptions, tmp_path: Path
    ) -> None:
        """_run_analysis_session returns placeholder dict when dry_run=True."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.WorktreeManager"),
            patch("hephaestus.automation.pr_reviewer.StatusTracker"),
        ):
            dry_reviewer = PRReviewer(mock_options)

        result = dry_reviewer._run_analysis_session(
            pr_number=42,
            issue_number=123,
            worktree_path=tmp_path,
            context={},
        )

        assert result["comments"] == []
        assert "DRY RUN" in result["summary"]


class TestIdempotencyGuard:
    """Tests for the COMPLETED-state idempotency guard (#374)."""

    def test_completed_state_on_disk_skips_review(
        self, mock_options: ReviewerOptions, tmp_path: Path
    ) -> None:
        """review-{n}.json on disk shows COMPLETED → _review_pr succeeds without posting."""
        from hephaestus.automation.models import ReviewPhase, ReviewState

        # Write a completed review state to disk
        state_dir = tmp_path / "build" / ".issue_implementer"
        state_dir.mkdir(parents=True)
        completed_state = ReviewState(issue_number=123, pr_number=42, phase=ReviewPhase.COMPLETED)
        (state_dir / "review-123.json").write_text(completed_state.model_dump_json())

        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.WorktreeManager") as mock_wm_cls,
            patch("hephaestus.automation.pr_reviewer.StatusTracker"),
        ):
            mock_wm = MagicMock()
            mock_wm_cls.return_value = mock_wm
            live_reviewer = PRReviewer(mock_options)

        with patch("hephaestus.automation.pr_reviewer.gh_pr_review_post") as mock_post:
            result = live_reviewer._review_pr(issue_number=123, pr_number=42)

        assert result.success is True
        mock_post.assert_not_called()
        # Worktree should NOT have been created
        mock_wm.create_worktree.assert_not_called()

    def test_malformed_state_file_starts_fresh(
        self, mock_options: ReviewerOptions, tmp_path: Path
    ) -> None:
        """Malformed state file → warning logged, fresh state created."""
        state_dir = tmp_path / "build" / ".issue_implementer"
        state_dir.mkdir(parents=True)
        (state_dir / "review-123.json").write_text("{not valid json!!!}")

        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.WorktreeManager") as mock_wm_cls,
            patch("hephaestus.automation.pr_reviewer.StatusTracker"),
        ):
            mock_wm = MagicMock()
            mock_wm.create_worktree.return_value = tmp_path
            mock_wm_cls.return_value = mock_wm
            live_reviewer = PRReviewer(mock_options)

        analysis = {"comments": [], "summary": "clean"}
        with (
            patch.object(live_reviewer, "_gather_pr_context", return_value={}),
            patch.object(live_reviewer, "_run_analysis_session", return_value=analysis),
            patch("hephaestus.automation.pr_reviewer.gh_pr_review_post", return_value=[]),
        ):
            result = live_reviewer._review_pr(issue_number=123, pr_number=42)

        # Should succeed with fresh state (bad file ignored, review proceeds)
        assert result.success is True


class TestReviewPostsInlineComments:
    """Tests for inline comment posting flow."""

    def test_review_posts_inline_comments(
        self, mock_options: ReviewerOptions, tmp_path: Path
    ) -> None:
        """Analysis returns valid JSON → gh_pr_review_post called with correct args."""
        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.WorktreeManager") as mock_wm_cls,
            patch("hephaestus.automation.pr_reviewer.StatusTracker"),
        ):
            mock_wm = MagicMock()
            mock_wm.create_worktree.return_value = tmp_path
            mock_wm_cls.return_value = mock_wm

            live_reviewer = PRReviewer(mock_options)

        comments = [{"path": "foo.py", "line": 5, "body": "Consider renaming this variable"}]
        analysis = {"comments": comments, "summary": "Looks mostly good"}

        with (
            patch.object(live_reviewer, "_gather_pr_context", return_value={}),
            patch.object(live_reviewer, "_run_analysis_session", return_value=analysis),
            patch("hephaestus.automation.pr_reviewer.gh_pr_review_post") as mock_post,
        ):
            mock_post.return_value = ["thread-id-1"]
            result = live_reviewer._review_pr(issue_number=123, pr_number=42)

        assert result.success is True
        mock_post.assert_called_once()
        # gh_pr_review_post is invoked entirely by keyword (see _review_pr).
        post_kwargs = mock_post.call_args.kwargs
        assert post_kwargs["pr_number"] == 42
        assert post_kwargs["comments"] == comments
        assert post_kwargs["summary"] == "Looks mostly good"
        assert post_kwargs["dry_run"] is False
        # The posted thread IDs propagate onto the saved review state.
        assert result.pr_number == 42


class TestGatherPrContextPolicyState:
    """Tests that _gather_pr_context collects auto-merge + commit signing state.

    The policy gate in PR_REVIEW_ANALYSIS_PROMPT depends on two new fields
    being populated; if they are absent or misparsed the reviewer treats the
    PR as a policy failure, so this is load-bearing.
    """

    def _gh_call_side_effect(
        self,
        diff_text: str,
        pr_view_json: dict[str, Any],
        graphql_nodes: list[dict[str, Any]] | None = None,
        checks_json: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Build a side_effect for the four _gh_call invocations.

        Order: pr diff, pr view --json (body+autoMergeRequest), api graphql
        (signing state), pr checks --json. The reviewer calls them in that
        sequence in ``_gather_pr_context``.
        """
        diff_result = MagicMock(returncode=0, stdout=diff_text, stderr="")
        view_result = MagicMock(returncode=0, stdout=json.dumps(pr_view_json), stderr="")
        graphql_payload = {
            "data": {"repository": {"pullRequest": {"commits": {"nodes": graphql_nodes or []}}}}
        }
        graphql_result = MagicMock(returncode=0, stdout=json.dumps(graphql_payload), stderr="")
        checks_result = MagicMock(returncode=0, stdout=json.dumps(checks_json or []), stderr="")
        return [diff_result, view_result, graphql_result, checks_result]

    def test_extracts_auto_merge_enabled(self, reviewer: PRReviewer, tmp_path: Path) -> None:
        with (
            patch("hephaestus.automation.pr_reviewer._gh_call") as mock_gh,
            patch(
                "hephaestus.automation.pr_reviewer.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.pr_reviewer.fetch_issue_info",
                return_value=MagicMock(body=""),
            ),
        ):
            mock_gh.side_effect = self._gh_call_side_effect(
                diff_text="diff --git a/x b/x\n+y\n",
                pr_view_json={
                    "body": "Closes #1",
                    "reviews": [],
                    "comments": [],
                    "autoMergeRequest": {"enabledBy": {"login": "alice"}},
                },
                graphql_nodes=[
                    {
                        "commit": {
                            "oid": "abc",
                            "signature": {"isValid": True, "signer": {"login": "alice"}},
                        }
                    }
                ],
            )
            ctx = reviewer._gather_pr_context(pr_number=1, issue_number=1, worktree_path=tmp_path)
        assert ctx["auto_merge_enabled"] is True
        assert ctx["commits_signing_state"] == [
            {"oid": "abc", "signature_valid": True, "signer": "alice"}
        ]

    def test_extracts_auto_merge_disabled(self, reviewer: PRReviewer, tmp_path: Path) -> None:
        with (
            patch("hephaestus.automation.pr_reviewer._gh_call") as mock_gh,
            patch(
                "hephaestus.automation.pr_reviewer.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.pr_reviewer.fetch_issue_info",
                return_value=MagicMock(body=""),
            ),
        ):
            mock_gh.side_effect = self._gh_call_side_effect(
                diff_text="diff --git a/x b/x\n+y\n",
                pr_view_json={
                    "body": "Closes #1",
                    "reviews": [],
                    "comments": [],
                    "autoMergeRequest": None,
                },
                graphql_nodes=[],
            )
            ctx = reviewer._gather_pr_context(pr_number=1, issue_number=1, worktree_path=tmp_path)
        assert ctx["auto_merge_enabled"] is False

    def test_unsigned_commit_yields_signature_valid_false(
        self, reviewer: PRReviewer, tmp_path: Path
    ) -> None:
        """GitHub returns commit.signature == null for unsigned commits."""
        with (
            patch("hephaestus.automation.pr_reviewer._gh_call") as mock_gh,
            patch(
                "hephaestus.automation.pr_reviewer.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.pr_reviewer.fetch_issue_info",
                return_value=MagicMock(body=""),
            ),
        ):
            mock_gh.side_effect = self._gh_call_side_effect(
                diff_text="diff --git a/x b/x\n+y\n",
                pr_view_json={
                    "body": "Closes #1",
                    "reviews": [],
                    "comments": [],
                    "autoMergeRequest": None,
                },
                graphql_nodes=[{"commit": {"oid": "deadbeef", "signature": None}}],
            )
            ctx = reviewer._gather_pr_context(pr_number=1, issue_number=1, worktree_path=tmp_path)
        assert ctx["commits_signing_state"] == [
            {"oid": "deadbeef", "signature_valid": False, "signer": None}
        ]

    def test_pr_view_failure_leaves_policy_state_at_nogo_default(
        self, reviewer: PRReviewer, tmp_path: Path
    ) -> None:
        """`gh pr view` failure must default policy state to a NOGO.

        If we silently treated a fetch failure as "no policy state needed",
        the reviewer prompt would pass a PR that ought to be blocked.
        """
        with (
            patch("hephaestus.automation.pr_reviewer._gh_call") as mock_gh,
            patch(
                "hephaestus.automation.pr_reviewer.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.pr_reviewer.fetch_issue_info",
                return_value=MagicMock(body=""),
            ),
        ):
            diff_result = MagicMock(returncode=0, stdout="diff\n+x\n", stderr="")
            # pr view fails → outer except handler skips both auto-merge AND
            # the graphql signing fetch, so we only need a checks mock after.
            checks_result = MagicMock(returncode=0, stdout="[]", stderr="")
            mock_gh.side_effect = [diff_result, RuntimeError("API down"), checks_result]
            ctx = reviewer._gather_pr_context(pr_number=1, issue_number=1, worktree_path=tmp_path)
        assert ctx["auto_merge_enabled"] is False
        assert ctx["commits_signing_state"] == []


# ---------------------------------------------------------------------------
# Extracted in-loop cores (Stage 2, #28) shared with the implementer session
# ---------------------------------------------------------------------------


class TestGatherImplReviewContext:
    """gather_impl_review_context folds TASK + PLAN + PLAN_REVIEW + diff together."""

    def test_composes_full_context(self) -> None:
        ctx = gather_impl_review_context(
            pr_number=42,
            issue_number=1,
            issue_title="Add widget",
            issue_body="The widget body.",
            plan_text="# Implementation Plan\nStep 1",
            plan_review_text="## 🔍 Plan Review\nVerdict: GO",
            diff_text="diff --git a/x b/x",
        )
        assert ctx["pr_diff"] == "diff --git a/x b/x"
        # TASK title + body and both PLAN sections are surfaced to the reviewer.
        assert "Add widget" in ctx["issue_body"]
        assert "The widget body." in ctx["issue_body"]
        assert "## PLAN" in ctx["issue_body"]
        assert "Step 1" in ctx["issue_body"]
        assert "## PLAN_REVIEW" in ctx["issue_body"]
        assert "Verdict: GO" in ctx["issue_body"]

    def test_missing_plan_sections_get_placeholders(self) -> None:
        ctx = gather_impl_review_context(
            pr_number=42,
            issue_number=1,
            issue_title="t",
            issue_body="b",
            plan_text="",
            plan_review_text="",
            diff_text="",
        )
        assert "no plan comment found" in ctx["issue_body"]
        assert "no plan-review comment found" in ctx["issue_body"]


class TestRunPrReviewAnalysis:
    """run_pr_review_analysis is the shared analysis core (standalone + in-loop)."""

    def test_dry_run_returns_placeholder(self, tmp_path: Path) -> None:
        out = run_pr_review_analysis(
            pr_number=1,
            issue_number=1,
            worktree_path=tmp_path,
            context={},
            agent="claude",
            state_dir=tmp_path,
            dry_run=True,
        )
        assert out["comments"] == []
        assert "DRY RUN" in out["summary"]

    def test_passes_review_agent_token_to_claude(self, tmp_path: Path) -> None:
        """The review_agent token is forwarded verbatim to invoke_claude_with_session."""
        captured: dict[str, str] = {}

        def _fake_invoke(*, agent: str, **_: object) -> tuple[str, str]:
            captured["agent"] = agent
            return (
                '{"result": "```json\\n{\\"comments\\": [], \\"summary\\": \\"ok\\"}\\n```"}',
                "",
            )

        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_reviewer.current_trunk_githash",
                return_value="abc1234",
            ),
            patch(
                "hephaestus.automation.pr_reviewer.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
        ):
            run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "d"},
                agent="claude",
                review_agent="pr-reviewer-r1",
                state_dir=tmp_path,
                dry_run=False,
            )
        assert captured["agent"] == "pr-reviewer-r1"


class TestReviewPrInline:
    """review_pr_inline runs a FRESH per-iteration reviewer and posts inline threads."""

    def test_posts_threads_and_returns_verdict(self, tmp_path: Path) -> None:
        analysis = {
            "comments": [{"path": "a.py", "line": 1, "body": "fix"}],
            "summary": "Findings.\n\nGrade: C\nVerdict: NOGO\n",
        }
        with (
            patch(
                "hephaestus.automation.pr_reviewer.run_pr_review_analysis",
                return_value=analysis,
            ) as mock_analysis,
            patch(
                "hephaestus.automation.pr_reviewer.gh_pr_review_post",
                return_value=["thread-1"],
            ) as mock_post,
        ):
            summary, thread_ids = review_pr_inline(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "d"},
                agent="claude",
                iteration=2,
                state_dir=tmp_path,
                dry_run=False,
            )

        assert thread_ids == ["thread-1"]
        assert "NOGO" in summary
        # FRESH per-iteration reviewer session: reviewer_agent(AGENT_PR_REVIEWER, 2).
        assert mock_analysis.call_args.kwargs["review_agent"] == "pr-reviewer-r2"
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["pr_number"] == 42

    def test_dry_run_skips_posting(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.pr_reviewer.gh_pr_review_post") as mock_post:
            _summary, thread_ids = review_pr_inline(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                context={},
                agent="claude",
                iteration=0,
                state_dir=tmp_path,
                dry_run=True,
            )
        assert thread_ids == []
        mock_post.assert_not_called()
