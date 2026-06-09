"""Tests for the PRReviewer posting side (pr_reviewer.py)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.claude_invoke import parse_review_verdict
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

        Patches ``_gh_call`` at the pr_reviewer module level so the inner
        diff-fetch raises deterministically. Without this, CI test ordering
        can trip the GitHub API circuit breaker before this test runs, and
        the captured error becomes "circuit breaker is open" instead of the
        domain-specific ``#0`` diagnostic the test exists to verify (#708).
        """
        gh_diff_failure = RuntimeError("no diff for PR #0 (test fixture)")
        with (
            patch.object(reviewer, "_find_pr_for_issue", return_value=None),
            patch(
                "hephaestus.automation.pr_reviewer._gh_call",
                side_effect=gh_diff_failure,
            ),
        ):
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


class TestGatherPrContextNoPolicyState:
    """_gather_pr_context no longer collects policy state (Closes/auto-merge/signing).

    Policy is enforced by the CI gates pr-policy + auto-merge-policy; the in-loop
    reviewer is code-quality only, so the context must NOT carry the removed
    ``auto_merge_enabled`` / ``commits_signing_state`` keys and must NOT make the
    GraphQL signing-state call. It still collects ``pr_description`` for review.
    """

    def test_context_omits_policy_keys_and_skips_signing_graphql(
        self, reviewer: PRReviewer, tmp_path: Path
    ) -> None:
        diff_result = MagicMock(returncode=0, stdout="diff --git a/x b/x\n+y\n", stderr="")
        view_result = MagicMock(
            returncode=0,
            stdout=json.dumps({"body": "Closes #1", "reviews": [], "comments": []}),
            stderr="",
        )
        checks_result = MagicMock(returncode=0, stdout="[]", stderr="")
        with (
            patch("hephaestus.automation.pr_reviewer._gh_call") as mock_gh,
            patch(
                "hephaestus.automation.pr_reviewer.fetch_issue_info",
                return_value=MagicMock(body=""),
            ),
        ):
            mock_gh.side_effect = [diff_result, view_result, checks_result]
            ctx = reviewer._gather_pr_context(pr_number=1, issue_number=1, worktree_path=tmp_path)

        # Policy keys are gone; code-quality fields remain.
        assert "auto_merge_enabled" not in ctx
        assert "commits_signing_state" not in ctx
        assert ctx["pr_description"] == "Closes #1"
        # No `api graphql` signing-state call is made anymore.
        argvs = [c.args[0] for c in mock_gh.call_args_list if c.args]
        assert not any("graphql" in argv for argv in argvs), "signing-state GraphQL call removed"
        # The `gh pr view` projection no longer requests autoMergeRequest.
        view_argv: list[str] = next((a for a in argvs if a[:2] == ["pr", "view"]), [])
        assert "autoMergeRequest" not in "".join(view_argv)


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
        assert out["review_text"] == out["summary"]

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

    def test_claude_path_preserves_review_text_for_verdict(self, tmp_path: Path) -> None:
        """Claude JSON summary may omit Verdict, but full prose must be returned."""
        response_text = (
            "Detailed review.\n\nGrade: A\nVerdict: GO\n\n"
            "```json\n" + json.dumps({"comments": [], "summary": "No inline findings."}) + "\n```"
        )

        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_reviewer.invoke_claude_with_session",
                return_value=(json.dumps({"result": response_text}), ""),
            ),
        ):
            out = run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "diff"},
                agent="claude",
                state_dir=tmp_path,
                dry_run=False,
            )

        assert out["summary"] == "No inline findings."
        assert "Verdict: GO" in out["review_text"]
        assert parse_review_verdict(out["review_text"]).verdict == "GO"

    def test_codex_path_preserves_stdout_for_verdict(self, tmp_path: Path) -> None:
        """Codex stdout prose must survive JSON parsing for verdict extraction."""
        stdout = (
            "Review complete.\n\nGrade: D\nVerdict: NOGO\n\n"
            "```json\n" + json.dumps({"comments": [], "summary": "Needs fixes."}) + "\n```"
        )

        with patch(
            "hephaestus.automation.pr_reviewer.run_codex_text",
            return_value=MagicMock(stdout=stdout),
        ):
            out = run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "diff"},
                agent="codex",
                state_dir=tmp_path,
                dry_run=False,
            )

        assert out["summary"] == "Needs fixes."
        assert "Verdict: NOGO" in out["review_text"]
        assert parse_review_verdict(out["review_text"]).verdict == "NOGO"

    def test_prompt_passed_via_stdin_not_argv(self, tmp_path: Path) -> None:
        """The reviewer prompt is piped via stdin, never embedded in argv.

        Regression for `[Errno 7] Argument list too long: 'claude'`: the
        PR-review prompt embeds the full diff and overflows ARG_MAX when passed
        as a positional argument, so the wrapper must be called with
        ``input_via_stdin=True``.
        """
        captured: dict[str, object] = {}

        def _fake_invoke(**kwargs: object) -> tuple[str, str]:
            captured.update(kwargs)
            return (
                '{"result": "```json\\n{\\"comments\\": [], \\"summary\\": \\"ok\\"}\\n```"}',
                "",
            )

        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_reviewer.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
        ):
            run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "x" * 200_000},
                agent="claude",
                state_dir=tmp_path,
                dry_run=False,
            )
        assert captured["input_via_stdin"] is True


class TestReviewPrInline:
    """review_pr_inline runs a FRESH per-iteration reviewer and posts inline threads."""

    def test_posts_threads_and_returns_verdict(self, tmp_path: Path) -> None:
        analysis = {
            "comments": [{"path": "a.py", "line": 1, "body": "fix"}],
            "summary": "Findings for GitHub.",
            "review_text": "Full reviewer prose.\n\nGrade: C\nVerdict: NOGO\n",
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
        assert mock_post.call_args.kwargs["summary"] == "Findings for GitHub."

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


class TestVerdictFromProseNotSummary:
    """The verdict (Verdict: GO/NOGO) lives in the review PROSE, not the JSON summary.

    Regression for the AMBIGUOUS misread: review_pr_inline must return the
    verdict-bearing prose so parse_review_verdict sees `Verdict: NOGO`, even
    though the JSON `summary` field (posted to GitHub) carries no verdict line.
    """

    def test_run_analysis_surfaces_review_text_with_verdict(self, tmp_path: Path) -> None:
        """run_pr_review_analysis returns the prose body (carrying Verdict:) as review_text."""
        prose = (
            "## Review\nFindings here.\n\n"
            "Verdict: NOGO — two real defects.\n\n"
            '```json\n{"comments": [], "summary": "two defects (no verdict here)"}\n```'
        )
        # Claude wraps the prose in a JSON result envelope.
        envelope = json.dumps({"result": prose})

        def _fake_invoke(**_: object) -> tuple[str, str]:
            return (envelope, "")

        with (
            patch("hephaestus.automation.pr_reviewer.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_reviewer.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_reviewer.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
        ):
            out = run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "d"},
                agent="claude",
                state_dir=tmp_path,
                dry_run=False,
            )
        # summary is the JSON field (no verdict); review_text is the prose (has verdict).
        assert out["summary"] == "two defects (no verdict here)"
        assert "Verdict: NOGO" in out["review_text"]

    def test_review_pr_inline_returns_verdict_text_not_summary(self, tmp_path: Path) -> None:
        """review_pr_inline returns the verdict-bearing prose, so the loop parses NOGO."""
        from hephaestus.automation.claude_invoke import parse_review_verdict

        analysis = {
            "comments": [
                {"path": "a.py", "line": 1, "side": "RIGHT", "severity": "major", "body": "x"}
            ],
            "summary": "a defect (no verdict token here)",
            "review_text": "## Review\nProse.\n\nVerdict: NOGO — a real defect.\n",
        }
        with (
            patch(
                "hephaestus.automation.pr_reviewer.run_pr_review_analysis", return_value=analysis
            ),
            patch("hephaestus.automation.pr_reviewer.gh_pr_review_post", return_value=["thread-1"]),
        ):
            review_text, thread_ids = review_pr_inline(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={},
                agent="claude",
                iteration=0,
                state_dir=tmp_path,
                dry_run=False,
            )
        # The returned text must carry the verdict so the loop reads NOGO, not AMBIGUOUS.
        assert parse_review_verdict(review_text).verdict == "NOGO"
        assert thread_ids == ["thread-1"]
