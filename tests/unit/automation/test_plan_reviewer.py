"""Tests for the PlanReviewer automation."""

import json
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
    """Return a mock CompletedProcess with JSON stdout."""
    mock = MagicMock()
    mock.stdout = json.dumps(payload)
    return mock


class TestGetLatestPlan:
    """Tests for _get_latest_plan method."""

    def test_get_latest_plan_finds_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns plan text from matching comment."""
        comments = [
            {"body": "Some other comment"},
            {"body": "## Implementation Plan\n\nStep 1: Do something\nStep 2: Do more"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Implementation Plan" in result

    def test_get_latest_plan_returns_last_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns the LAST plan comment when multiple exist."""
        comments = [
            {"body": "## Implementation Plan\n\nFirst plan"},
            {"body": "## Implementation Plan\n\nSecond plan (updated)"},
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


class TestHasExistingReview:
    """Tests for _has_existing_review method."""

    def test_has_existing_review_true(self, reviewer: PlanReviewer) -> None:
        """_has_existing_review returns True when review comment exists."""
        comments = [
            {"body": "## 🔍 Plan Review\n\nSome review content"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._has_existing_review(123)

        assert result is True

    def test_has_existing_review_false_no_review(self, reviewer: PlanReviewer) -> None:
        """_has_existing_review returns False when no review comment exists."""
        comments = [
            {"body": "## Implementation Plan\n\nSome plan"},
            {"body": "Just a comment"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._has_existing_review(123)

        assert result is False

    def test_has_existing_review_false_on_error(self, reviewer: PlanReviewer) -> None:
        """_has_existing_review returns False when gh call fails."""
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.side_effect = RuntimeError("gh failed")
            result = reviewer._has_existing_review(123)

        assert result is False


class TestRunClaudeAnalysis:
    """Tests for _run_claude_analysis method."""

    def test_returns_none_on_empty_output(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude returns empty output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="   ", stderr="")
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None

    def test_returns_none_on_nonzero_exit(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude exits non-zero."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error message")
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None

    def test_returns_analysis_on_success(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns review text on successful Claude call."""
        analysis_text = "This plan looks good. Here are some suggestions."
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=analysis_text, stderr="")
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result == analysis_text

    def test_returns_none_on_timeout(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude times out."""
        import subprocess

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("claude", 300)
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None


class TestReviewIssue:
    """Tests for _review_issue method."""

    def test_review_skipped_if_no_plan(self, reviewer: PlanReviewer) -> None:
        """When issue has no plan comment, _review_issue returns success with no post."""
        with (
            patch.object(reviewer, "_has_existing_review", return_value=False),
            patch.object(reviewer, "_get_latest_plan", return_value=None),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_not_called()

    def test_review_skipped_if_already_reviewed(self, reviewer: PlanReviewer) -> None:
        """When latest comment is a plan review, skip posting."""
        with (
            patch.object(reviewer, "_has_existing_review", return_value=True),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_not_called()

    def test_review_posted(self, reviewer: PlanReviewer) -> None:
        """When plan exists and no review yet, posts review comment with correct prefix."""
        with (
            patch.object(reviewer, "_has_existing_review", return_value=False),
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value="Great plan! A few suggestions.",
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
            patch.object(reviewer, "_has_existing_review", return_value=False),
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value="Review text",
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
            patch.object(reviewer, "_has_existing_review", return_value=False),
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
