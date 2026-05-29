"""Tests for the PlanReviewer automation."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.models import PlanReviewerOptions
from hephaestus.automation.plan_reviewer import PlanReviewer


@pytest.fixture
def mock_options() -> PlanReviewerOptions:
    """Create mock PlanReviewerOptions."""
    return PlanReviewerOptions(
        issues=[123],
        dry_run=False,
        max_workers=1,
        enable_ui=False,
    )


@pytest.fixture
def reviewer(mock_options: PlanReviewerOptions) -> PlanReviewer:
    """Create a PlanReviewer instance."""
    return PlanReviewer(mock_options)


def _make_gh_result(payload: Any) -> MagicMock:
    """Return a mock CompletedProcess emulating the GraphQL response shape.

    The legacy ``{"comments": [...]}`` payload (from ``gh issue view --comments``)
    is automatically translated into the new GraphQL envelope
    ``{"data": {"repository": {"issue": {"comments": {"nodes": [...]}}}}}``
    with ``nodes`` in descending order (newest first) to match real GraphQL
    output (see ``orderBy: {field: UPDATED_AT, direction: DESC}`` in
    ``_fetch_issue_comments``). Production code reverses ``nodes`` back to
    chronological order, so test inputs continue to be authored in
    chronological order — the helper hides the reversal.

    Tests that want to drive the raw GraphQL envelope can pass it directly
    (any payload not matching ``{"comments": [...]}`` is forwarded as-is).
    """
    mock = MagicMock()
    if isinstance(payload, dict) and set(payload.keys()) == {"comments"}:
        nodes = list(reversed(payload["comments"]))
        graphql_payload = {"data": {"repository": {"issue": {"comments": {"nodes": nodes}}}}}
        mock.stdout = json.dumps(graphql_payload)
    else:
        mock.stdout = json.dumps(payload)
    return mock


@pytest.fixture(autouse=True)
def _patch_repo_helpers() -> Any:
    """Stub repo discovery helpers used by the GraphQL fetch.

    ``_fetch_issue_comments`` calls ``get_repo_info`` for the GraphQL query
    (#574). ``get_repo_slug`` is still patched for any code path that asks
    for the short slug (logger prefixes, etc.).
    """
    with (
        patch(
            "hephaestus.automation.plan_reviewer.get_repo_root",
            return_value=Path("/tmp/repo"),
        ),
        patch(
            "hephaestus.automation.plan_reviewer.get_repo_slug",
            return_value="name",
        ),
        patch(
            "hephaestus.automation.plan_reviewer.get_repo_info",
            return_value=("owner", "name"),
        ),
    ):
        yield


class TestGetLatestPlan:
    """Tests for _get_latest_plan method."""

    def test_get_latest_plan_finds_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns plan text from matching comment."""
        comments = [
            {"body": "Some other comment"},
            {"body": "# Implementation Plan\n\nStep 1: Do something\nStep 2: Do more"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Implementation Plan" in result

    def test_get_latest_plan_returns_last_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns the LAST plan comment when multiple exist."""
        comments = [
            {"body": "# Implementation Plan\n\nFirst plan"},
            {"body": "# Implementation Plan\n\nSecond plan (updated)"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Second plan (updated)" in result

    def test_get_latest_plan_returns_none_when_no_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns None when no plan comment exists."""
        comments = [
            {"body": "Just a regular comment"},
            {"body": "Another comment"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is None

    def test_get_latest_plan_returns_none_on_gh_error(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns None when gh call fails."""
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.side_effect = RuntimeError("gh failed")
            result = reviewer._get_latest_plan(123)

        assert result is None

    def test_get_latest_plan_ignores_review_comment(self, reviewer: PlanReviewer) -> None:
        """A review comment that quotes the plan must never be picked as the plan.

        Regression for #455/#468/#484: a ``## 🔍 Plan Review`` body contains
        ``## Objective``/``## Plan`` as substrings when it quotes the plan, and
        matching those caused the reviewer to review its own prior review.
        """
        comments = [
            {"body": "# Implementation Plan\n\n## Objective\nDo the thing."},
            # A later review comment quoting the plan's headings:
            {
                "body": (
                    "## 🔍 Plan Review\n\nThe plan's ## Objective and ## Plan "
                    "sections look fine.\n\n**Verdict: REVISE**"
                )
            },
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is not None
        # Must be the actual plan, NOT the review comment.
        assert result.lstrip().startswith("# Implementation Plan")
        assert "🔍 Plan Review" not in result

    def test_get_latest_plan_review_only_issue_returns_none(self, reviewer: PlanReviewer) -> None:
        """An issue with ONLY a review comment (no real plan) → None, not the review."""
        comments = [
            {"body": "## 🔍 Plan Review\n\nDiscusses a ## Plan.\n\n**Verdict: REVISE**"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is None


class TestLatestReviewIsFinal:
    """Tests for the FINAL/APPROVED short-circuit gate.

    The reviewer skips an issue only when the LATEST plan-review comment
    carries the `**Verdict: APPROVED**` marker. REVISE/BLOCK/no-marker
    re-runs the reviewer so an amended plan gets a fresh evaluation.
    """

    def test_skip_when_latest_review_is_approved(self, reviewer: PlanReviewer) -> None:
        """APPROVED in the latest plan-review comment → skip."""
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {"body": "## 🔍 Plan Review\n\nLooks good.\n\n**Verdict: APPROVED**"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is True

    def test_rerun_when_latest_review_revise(self, reviewer: PlanReviewer) -> None:
        """REVISE in the latest plan-review comment → re-run (not final)."""
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {"body": "## 🔍 Plan Review\n\nNeeds work.\n\n**Verdict: REVISE**"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is False

    def test_rerun_when_latest_review_block(self, reviewer: PlanReviewer) -> None:
        """BLOCK in the latest plan-review comment → re-run (not final)."""
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {"body": "## 🔍 Plan Review\n\nFundamental problem.\n\n**Verdict: BLOCK**"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is False

    def test_rerun_when_older_approved_but_newer_revise(self, reviewer: PlanReviewer) -> None:
        """An older APPROVED followed by a newer REVISE → re-run (latest wins)."""
        comments = [
            {"body": "## Implementation Plan\n\nFirst plan"},
            {"body": "## 🔍 Plan Review\n\nOld pass.\n\n**Verdict: APPROVED**"},
            {"body": "## Implementation Plan\n\nAmended plan"},
            {"body": "## 🔍 Plan Review\n\nNew concerns.\n\n**Verdict: REVISE**"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is False

    def test_rerun_when_review_lacks_verdict_marker(self, reviewer: PlanReviewer) -> None:
        """A plan-review comment without any verdict marker → re-run."""
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {"body": "## 🔍 Plan Review\n\nPre-marker convention review body."},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is False

    def test_rerun_when_no_review_comment_exists(self, reviewer: PlanReviewer) -> None:
        """No plan-review comment at all → re-run (not final)."""
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {"body": "Some unrelated drive-by comment"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is False

    def test_false_on_gh_error(self, reviewer: PlanReviewer) -> None:
        """Gh failure → _fetch_issue_comments returns [], gate returns False."""
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.side_effect = RuntimeError("gh failed")
            assert reviewer._latest_review_is_final(123) is False

    def test_false_when_approved_precedes_block_in_same_body(self, reviewer: PlanReviewer) -> None:
        """Only the LAST verdict line counts — APPROVED then BLOCK → False.

        Claude is allowed to discuss multiple verdict options in prose; the
        prompt instructs readers to take only the LAST matching line.
        Substring ``in`` would mis-fire True here.
        """
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {
                "body": (
                    "## 🔍 Plan Review\n\n"
                    "Initial impression: looked sound.\n\n"
                    "**Verdict: APPROVED**\n\n"
                    "On reflection a fatal correctness bug surfaced.\n\n"
                    "**Verdict: BLOCK**"
                )
            },
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is False

    def test_true_when_block_precedes_approved_in_same_body(self, reviewer: PlanReviewer) -> None:
        """BLOCK then APPROVED → True (last verdict line wins)."""
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {
                "body": (
                    "## 🔍 Plan Review\n\n"
                    "First-pass concern.\n\n"
                    "**Verdict: BLOCK**\n\n"
                    "After re-reading, concern was unfounded.\n\n"
                    "**Verdict: APPROVED**"
                )
            },
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is True

    def test_false_when_marker_is_quoted_in_discussion(self, reviewer: PlanReviewer) -> None:
        """A quoted marker in prose must not fire the gate.

        The substring check used to return True on inline mentions like
        ``avoid using **Verdict: APPROVED** here`` — the regex requires the
        marker to occupy an entire line.
        """
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {
                "body": (
                    "## 🔍 Plan Review\n\n"
                    "Reviewer note: avoid using **Verdict: APPROVED** here "
                    "because the plan still has gaps.\n\n"
                    "**Verdict: REVISE**"
                )
            },
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is False

    def test_handles_150_comments_without_truncating_latest(self, reviewer: PlanReviewer) -> None:
        """Issues with >100 comments must still surface the latest review.

        The previous ``gh issue view --comments`` call silently capped at
        100 entries. The GraphQL ``last: 100`` query is bounded too, but
        because it sorts ``UPDATED_AT DESC`` it always brings the newest
        100 — which includes the actual most-recent plan-review comment
        even when the issue has 150+ historical comments. See issue #553.
        """
        # Build 150 noise comments, then sandwich the real reviews so the
        # latest review (REVISE) is well past index 100 in chronological
        # order. After the helper reverses to DESC order to mimic GraphQL,
        # the production code reverses back to chronological — the test
        # still authors the list chronologically.
        chronological: list[dict[str, Any]] = []
        for i in range(120):
            chronological.append({"body": f"Drive-by comment {i}"})
        chronological.append({"body": "## 🔍 Plan Review\n\nOld pass.\n\n**Verdict: APPROVED**"})
        for i in range(120, 145):
            chronological.append({"body": f"Drive-by comment {i}"})
        chronological.append({"body": "## 🔍 Plan Review\n\nNew concerns.\n\n**Verdict: REVISE**"})
        # Total = 120 + 1 + 25 + 1 = 147; trim the OLDEST entries so the
        # remaining 100 (newest) still include both reviews. Real GraphQL
        # would do exactly this via ``last: 100`` + DESC ordering.
        newest_100 = chronological[-100:]

        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": newest_100})
            # The latest review is REVISE → not final → re-run.
            assert reviewer._latest_review_is_final(123) is False

    def test_approved_marker_anywhere_in_latest_review_counts(self, reviewer: PlanReviewer) -> None:
        """The marker may appear anywhere in the review body.

        Claude is free to put `**Verdict: APPROVED**` on the last line or
        anywhere else — we don't require strict end-of-text positioning.
        """
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
            {
                "body": (
                    "## 🔍 Plan Review\n\n"
                    "**Verdict: APPROVED**\n\n"
                    "Long-form rationale follows below..."
                )
            },
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            assert reviewer._latest_review_is_final(123) is True


class TestPostReviewVerdictFallback:
    """Tests for the defence-in-depth fallback verdict line in _post_review."""

    def test_appends_fallback_when_verdict_missing(self, reviewer: PlanReviewer) -> None:
        """If Claude output has no `**Verdict:`, _post_review appends REVISE."""
        with patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment:
            reviewer._post_review(123, "Just analysis prose, no verdict line.")

        mock_comment.assert_called_once()
        posted_body: str = mock_comment.call_args[0][1]
        assert "**Verdict: REVISE**" in posted_body
        assert posted_body.startswith("## 🔍 Plan Review")

    def test_does_not_double_append_when_verdict_present(self, reviewer: PlanReviewer) -> None:
        """If the review already has any **Verdict: line, no fallback is added."""
        with patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment:
            reviewer._post_review(
                123,
                "Some analysis.\n\n**Verdict: APPROVED**",
            )

        posted_body: str = mock_comment.call_args[0][1]
        # exactly one Verdict line, not two
        assert posted_body.count("**Verdict:") == 1
        assert "**Verdict: APPROVED**" in posted_body


class TestRunClaudeAnalysis:
    """Tests for _run_claude_analysis method.

    These tests patch ``invoke_claude_with_session`` at the
    ``plan_reviewer`` module boundary — that is the actual call site at
    ``plan_reviewer.py:400``. Patching ``subprocess.run`` would miss the
    real code path because the production code calls a thin Claude-CLI
    wrapper, not ``subprocess.run`` directly. The wrapper returns the
    ``(stdout, session_uuid)`` tuple documented in
    :func:`hephaestus.automation.claude_invoke.invoke_claude_with_session`.
    """

    def test_returns_none_on_empty_output(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude returns empty output."""
        with patch("hephaestus.automation.plan_reviewer.invoke_claude_with_session") as mock_invoke:
            mock_invoke.return_value = ("   ", "session-uuid")
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None
        mock_invoke.assert_called_once()

    def test_returns_none_on_nonzero_exit(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude exits non-zero.

        ``invoke_claude_with_session`` raises ``CalledProcessError`` on
        non-zero exit — that is the real failure mode, not a
        ``returncode=1`` ``CompletedProcess`` (the wrapper would have
        already raised before returning).
        """
        import subprocess

        exc = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="error message"
        )
        with patch("hephaestus.automation.plan_reviewer.invoke_claude_with_session") as mock_invoke:
            mock_invoke.side_effect = exc
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None

    def test_returns_analysis_on_success(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns review text on successful Claude call."""
        analysis_text = "This plan looks good. Here are some suggestions."
        with patch("hephaestus.automation.plan_reviewer.invoke_claude_with_session") as mock_invoke:
            mock_invoke.return_value = (analysis_text, "session-uuid")
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result == analysis_text
        # Sanity-check the wrapper was called with the expected kwargs.
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["issue"] == 123
        assert kwargs["input_via_stdin"] is True
        assert kwargs["allowed_tools"] == "Read,Glob,Grep"

    def test_returns_none_on_timeout(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude times out."""
        import subprocess

        with patch("hephaestus.automation.plan_reviewer.invoke_claude_with_session") as mock_invoke:
            mock_invoke.side_effect = subprocess.TimeoutExpired("claude", 300)
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None

    def test_retries_on_rate_limit_with_quota_reset(self, reviewer: PlanReviewer) -> None:
        """A 429 with a parseable reset epoch triggers wait_until + retry.

        Production code (``plan_reviewer.py:418-433``) catches
        ``CalledProcessError``, asks ``scan_quota_reset`` to extract an
        epoch from stderr, and on a hit recurses with ``max_retries-1``
        after ``wait_until(epoch)``. We patch ``wait_until`` so the test
        does not sleep, then verify the wrapper is called twice — the
        recursive retry path.
        """
        import subprocess

        reset_epoch = 1_700_000_000
        exc = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr="rate limited"
        )
        analysis_text = "Retry succeeded — plan is fine."

        with (
            patch("hephaestus.automation.plan_reviewer.invoke_claude_with_session") as mock_invoke,
            patch(
                "hephaestus.automation.plan_reviewer.scan_quota_reset",
                return_value=reset_epoch,
            ) as mock_scan,
            patch("hephaestus.automation.plan_reviewer.wait_until") as mock_wait,
        ):
            mock_invoke.side_effect = [exc, (analysis_text, "session-uuid")]
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result == analysis_text
        assert mock_invoke.call_count == 2
        mock_scan.assert_called_once_with("rate limited", "")
        mock_wait.assert_called_once_with(reset_epoch)


class TestReviewIssue:
    """Tests for _review_issue method."""

    def test_review_skipped_if_no_plan(self, reviewer: PlanReviewer) -> None:
        """When issue has no plan comment, _review_issue returns success with no post."""
        with (
            patch.object(reviewer, "_latest_review_is_final", return_value=False),
            patch.object(reviewer, "_get_latest_plan", return_value=None),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_not_called()

    def test_review_skipped_if_latest_review_is_final(self, reviewer: PlanReviewer) -> None:
        """When the latest plan review is APPROVED, skip posting."""
        with (
            patch.object(reviewer, "_latest_review_is_final", return_value=True),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_not_called()

    def test_review_posted(self, reviewer: PlanReviewer) -> None:
        """When plan exists and no APPROVED review yet, posts review with correct prefix."""
        with (
            patch.object(reviewer, "_latest_review_is_final", return_value=False),
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value="Great plan! A few suggestions.\n\n**Verdict: APPROVED**",
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            mock_gh_json.return_value = {"title": "Test Issue", "body": "Issue body"}
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_called_once()
        posted_body: str = mock_comment.call_args[0][1]
        assert posted_body.startswith("## 🔍 Plan Review")
        assert "Great plan!" in posted_body

    def test_dry_run_no_post(self, mock_options: PlanReviewerOptions) -> None:
        """dry_run=True → gh_issue_comment never called."""
        mock_options.dry_run = True
        reviewer = PlanReviewer(mock_options)

        with (
            patch.object(reviewer, "_latest_review_is_final", return_value=False),
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value="Review text\n\n**Verdict: REVISE**",
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            mock_gh_json.return_value = {"title": "Test Issue", "body": "Issue body"}
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_not_called()

    def test_returns_failure_when_claude_returns_none(self, reviewer: PlanReviewer) -> None:
        """Returns failed WorkerResult when Claude analysis returns None."""
        with (
            patch.object(reviewer, "_latest_review_is_final", return_value=False),
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(reviewer, "_run_claude_analysis", return_value=None),
        ):
            mock_gh_json.return_value = {"title": "Test Issue", "body": "Issue body"}
            result = reviewer._review_issue(123, 0)

        assert result.success is False
        assert result.error is not None


class TestFetchIssueCommentsCache:
    """Tests for the _fetch_issue_comments caching helper (#A3-009)."""

    def test_api_called_only_once_for_same_issue(self, reviewer: PlanReviewer) -> None:
        """Calling _latest_review_is_final and _get_latest_plan should hit the API once."""
        comments = [
            {"body": "## Implementation Plan\n\nDo stuff"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})

            # Call both methods that internally use _fetch_issue_comments
            reviewer._latest_review_is_final(123)
            reviewer._get_latest_plan(123)

        assert mock_gh.call_count == 1, "Expected single API call due to caching"

    def test_api_called_once_per_issue(self, reviewer: PlanReviewer) -> None:
        """Different issue numbers each get their own API call."""
        comments_123 = [{"body": "## Implementation Plan\n\nIssue 123"}]
        comments_456 = [{"body": "## Implementation Plan\n\nIssue 456"}]

        call_count = 0

        def _side_effect(args: Any, **kw: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "123" in args:
                return _make_gh_result({"comments": comments_123})
            return _make_gh_result({"comments": comments_456})

        with patch("hephaestus.automation.plan_reviewer._gh_call", side_effect=_side_effect):
            reviewer._get_latest_plan(123)
            reviewer._get_latest_plan(123)  # should use cache
            reviewer._get_latest_plan(456)  # new issue → new API call
            reviewer._get_latest_plan(456)  # should use cache

        assert call_count == 2

    def test_api_error_returns_empty_list(self, reviewer: PlanReviewer) -> None:
        """API failure → _fetch_issue_comments returns empty list, not exception."""
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.side_effect = RuntimeError("network error")
            result = reviewer._fetch_issue_comments(999)

        assert result == []

    def test_uses_owner_repo_tuple_from_get_repo_info(self, reviewer: PlanReviewer) -> None:
        """Regression test for #574 — derive owner+name from get_repo_info.

        ``_fetch_issue_comments`` must obtain ``owner`` and ``name`` from
        ``get_repo_info`` (which returns a tuple) rather than calling
        ``get_repo_slug(...).split('/', 1)`` (which crashes with "not enough
        values to unpack" because the slug is just the repo name with no
        owner prefix).

        We assert the GraphQL call receives the owner and name as SEPARATE
        ``-F`` flags, derived from ``get_repo_info``'s tuple, not from a
        string-split of the slug.
        """
        reviewer._comments_cache.clear()
        with (
            patch(
                "hephaestus.automation.plan_reviewer.get_repo_info",
                return_value=("HomericIntelligence", "ProjectMnemosyne"),
            ) as mock_info,
            patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh,
        ):
            mock_gh.return_value = _make_gh_result({"comments": []})
            reviewer._fetch_issue_comments(1928)

        mock_info.assert_called_once()
        gh_args = mock_gh.call_args[0][0]
        joined = " ".join(gh_args)
        assert "owner=HomericIntelligence" in joined
        assert "name=ProjectMnemosyne" in joined


class TestMain:
    """Smoke tests for plan_reviewer.main()."""

    def test_success_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() returns 0 when every issue is reviewed successfully."""
        from hephaestus.automation import plan_reviewer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            "sys.argv", ["plan-reviewer", "--issues", "1", "2", "--no-ui", "--dry-run"]
        )

        def fake_run(self: object) -> dict[int, WorkerResult]:
            return {
                1: WorkerResult(issue_number=1, success=True),
                2: WorkerResult(issue_number=2, success=True),
            }

        monkeypatch.setattr(plan_reviewer.PlanReviewer, "run", fake_run)
        assert plan_reviewer.main() == 0

    def test_success_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main() with --json emits ok envelope on success."""
        import json as _json

        from hephaestus.automation import plan_reviewer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            "sys.argv",
            ["plan-reviewer", "--issues", "1", "--no-ui", "--dry-run", "--json"],
        )

        def fake_run(self: object) -> dict[int, WorkerResult]:
            return {1: WorkerResult(issue_number=1, success=True)}

        monkeypatch.setattr(plan_reviewer.PlanReviewer, "run", fake_run)
        assert plan_reviewer.main() == 0
        payload = _json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["issues"] == [1]
        assert payload["failed"] == []

    def test_failure_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main() with --json emits error envelope when any review fails."""
        import json as _json

        from hephaestus.automation import plan_reviewer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            "sys.argv",
            ["plan-reviewer", "--issues", "1", "2", "--no-ui", "--dry-run", "--json"],
        )

        def fake_run(self: object) -> dict[int, WorkerResult]:
            return {
                1: WorkerResult(issue_number=1, success=True),
                2: WorkerResult(issue_number=2, success=False, error="boom"),
            }

        monkeypatch.setattr(plan_reviewer.PlanReviewer, "run", fake_run)
        assert plan_reviewer.main() == 1
        payload = _json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["failed"] == [2]

    def test_keyboard_interrupt_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """KeyboardInterrupt with --json emits a 130 envelope."""
        import json as _json

        from hephaestus.automation import plan_reviewer

        monkeypatch.setattr(
            "sys.argv",
            ["plan-reviewer", "--issues", "1", "--no-ui", "--dry-run", "--json"],
        )

        def fake_run(self: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(plan_reviewer.PlanReviewer, "run", fake_run)
        assert plan_reviewer.main() == 130
        payload = _json.loads(capsys.readouterr().out)
        assert payload["exit_code"] == 130
        assert payload["message"] == "interrupted"

    def test_dedupes_issue_numbers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Duplicate --issues values are de-duplicated before review runs."""
        from hephaestus.automation import plan_reviewer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            "sys.argv",
            ["plan-reviewer", "--issues", "5", "5", "5", "--no-ui", "--dry-run"],
        )

        seen_issues: list[list[int]] = []

        def fake_run(self: object) -> dict[int, WorkerResult]:
            # self.options is set during PlanReviewer.__init__
            seen_issues.append(list(self.options.issues))  # type: ignore[attr-defined]
            return {5: WorkerResult(issue_number=5, success=True)}

        monkeypatch.setattr(plan_reviewer.PlanReviewer, "run", fake_run)
        assert plan_reviewer.main() == 0
        assert seen_issues == [[5]]
