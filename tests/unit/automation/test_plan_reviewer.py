"""Tests for the PlanReviewer automation."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.models import PlanReviewerOptions
from hephaestus.automation.plan_reviewer import PlanReviewer
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
)


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


@pytest.fixture(autouse=True)
def _patch_repo_helpers() -> Any:
    """Stub repository discovery and authenticated ownership."""
    with (
        patch(
            "hephaestus.automation.plan_reviewer.get_repo_root",
            return_value=Path("/tmp/repo"),
        ),
        patch(
            "hephaestus.automation.plan_reviewer.get_repo_slug",
            return_value="name",
        ),
        patch("hephaestus.automation.plan_reviewer.gh_current_login", return_value="bot"),
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
        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            return_value=[{"viewerDidAuthor": True, **comment} for comment in comments],
        ):
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Implementation Plan" in result

    def test_get_latest_plan_returns_last_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns the LAST plan comment when multiple exist."""
        comments = [
            {"body": "# Implementation Plan\n\nFirst plan"},
            {"body": "# Implementation Plan\n\nSecond plan (updated)"},
        ]
        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            return_value=[{"viewerDidAuthor": True, **comment} for comment in comments],
        ):
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Second plan (updated)" in result

    def test_get_latest_plan_is_not_hidden_by_one_hundred_newer_comments(
        self, reviewer: PlanReviewer
    ) -> None:
        """Complete journal ingestion preserves an older actor-owned canonical plan."""
        comments = [
            {
                "body": "# Implementation Plan\n\nOwned plan",
                "user": {"login": "bot"},
            },
            *[
                {
                    "body": f"ordinary comment {index}",
                    "user": {"login": "someone-else"},
                }
                for index in range(100)
            ],
        ]
        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            return_value=comments,
        ):
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Owned plan" in result

    def test_get_latest_plan_ignores_foreign_marker_comment(self, reviewer: PlanReviewer) -> None:
        """Only the authenticated actor's canonical plan can be reviewed."""
        comments = [
            {
                "body": "# Implementation Plan\n\nOwned plan",
                "viewerDidAuthor": True,
            },
            {
                "body": "# Implementation Plan\n\nForeign spoof",
                "viewerDidAuthor": False,
            },
        ]
        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            return_value=comments,
        ):
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Owned plan" in result
        assert "Foreign spoof" not in result

    def test_get_latest_plan_returns_none_when_no_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns None when no plan comment exists."""
        comments = [
            {"body": "Just a regular comment"},
            {"body": "Another comment"},
        ]
        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            return_value=[{"viewerDidAuthor": True, **comment} for comment in comments],
        ):
            result = reviewer._get_latest_plan(123)

        assert result is None

    def test_get_latest_plan_propagates_github_error(self, reviewer: PlanReviewer) -> None:
        """A failed complete-journal read cannot be mistaken for no plan."""
        with (
            patch(
                "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
                side_effect=RuntimeError("gh failed"),
            ),
            pytest.raises(RuntimeError, match="gh failed"),
        ):
            reviewer._get_latest_plan(123)

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
                    "sections look fine.\n\nstate:plan-no-go"
                )
            },
        ]
        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            return_value=[{"viewerDidAuthor": True, **comment} for comment in comments],
        ):
            result = reviewer._get_latest_plan(123)

        assert result is not None
        # Must be the actual plan, NOT the review comment.
        assert result.lstrip().startswith("# Implementation Plan")
        assert "🔍 Plan Review" not in result

    def test_get_latest_plan_review_only_issue_returns_none(self, reviewer: PlanReviewer) -> None:
        """An issue with ONLY a review comment (no real plan) → None, not the review."""
        comments = [
            {"body": "## 🔍 Plan Review\n\nDiscusses a ## Plan.\n\nstate:plan-no-go"},
        ]
        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            return_value=[{"viewerDidAuthor": True, **comment} for comment in comments],
        ):
            result = reviewer._get_latest_plan(123)

        assert result is None


class TestPostReviewStateLabels:
    """Standalone review persistence uses only the state-token/label contract."""

    @pytest.mark.parametrize(
        ("token", "expected_add", "expected_remove"),
        [
            (
                STATE_PLAN_GO,
                STATE_PLAN_GO,
                [STATE_PLAN_NO_GO, STATE_NEEDS_PLAN],
            ),
            (
                STATE_PLAN_NO_GO,
                STATE_PLAN_NO_GO,
                [STATE_PLAN_GO, STATE_NEEDS_PLAN],
            ),
            (
                STATE_PLAN_BLOCKED,
                STATE_PLAN_BLOCKED,
                [STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO],
            ),
        ],
    )
    def test_state_token_updates_canonical_comment_and_authoritative_label(
        self,
        reviewer: PlanReviewer,
        token: str,
        expected_add: str,
        expected_remove: list[str],
    ) -> None:
        review = f"Concrete review explanation.\n\n{token}"
        with (
            patch.object(reviewer, "_fetch_issue_comments", return_value=[]),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment",
                create=True,
            ) as upsert,
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_comment",
                create=True,
            ) as legacy_upsert,
            patch("hephaestus.automation.plan_reviewer.gh_issue_edit_labels") as edit_labels,
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_json",
                return_value={"labels": [{"name": expected_add}]},
            ),
        ):
            reviewer._post_review(123, review)

        posted_body = upsert.call_args.args[2]
        assert "Verdict:" not in posted_body
        assert posted_body.rstrip().endswith(token)
        edit_labels.assert_called_once_with(123, add=[expected_add], remove=expected_remove)
        legacy_upsert.assert_not_called()

    def test_legacy_verdict_is_rejected_without_any_github_write(
        self, reviewer: PlanReviewer
    ) -> None:
        with (
            patch.object(reviewer, "_fetch_issue_comments", return_value=[]),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment",
                create=True,
            ) as upsert,
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_comment",
                create=True,
            ) as legacy_upsert,
            patch("hephaestus.automation.plan_reviewer.gh_issue_edit_labels") as edit_labels,
            pytest.raises(ValueError, match="state:plan"),
        ):
            reviewer._post_review(123, "Looks good.\n\nVerdict: GO")

        upsert.assert_not_called()
        legacy_upsert.assert_not_called()
        edit_labels.assert_not_called()

    def test_blocked_comment_failure_preserves_authoritative_blocked_latch(
        self, reviewer: PlanReviewer
    ) -> None:
        """Audit failure cannot prevent an externally-owned BLOCKED latch."""
        with (
            patch.object(reviewer, "_fetch_issue_comments", return_value=[]),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment",
                side_effect=RuntimeError("comment write failed"),
                create=True,
            ),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_comment",
                create=True,
            ) as legacy_upsert,
            patch("hephaestus.automation.plan_reviewer.gh_issue_edit_labels") as edit_labels,
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_json",
                side_effect=[
                    {"labels": [{"name": STATE_NEEDS_PLAN}]},
                    {"labels": [{"name": STATE_PLAN_BLOCKED}]},
                ],
            ),
            pytest.raises(RuntimeError, match="comment write failed"),
        ):
            reviewer._post_review(
                123,
                "Waiting for dependency issue #99.\n\nstate:plan-blocked",
            )

        edit_labels.assert_called_once_with(
            123,
            add=[STATE_PLAN_BLOCKED],
            remove=[STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO],
        )
        legacy_upsert.assert_not_called()

    def test_state_transition_rejects_target_with_stale_sibling(
        self, reviewer: PlanReviewer
    ) -> None:
        """Standalone success requires the full mutually-exclusive label state."""
        with (
            patch.object(reviewer, "_fetch_issue_comments", return_value=[]),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment",
                create=True,
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_edit_labels"),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_json",
                side_effect=[
                    {"labels": [{"name": STATE_NEEDS_PLAN}]},
                    {
                        "labels": [
                            {"name": STATE_PLAN_GO},
                            {"name": STATE_PLAN_NO_GO},
                        ]
                    },
                ],
            ),
            pytest.raises(RuntimeError, match="not confirmed"),
        ):
            reviewer._post_review(123, "Concrete review.\n\nstate:plan-go")

    def test_nonblocked_review_cannot_replace_live_blocked_latch(
        self, reviewer: PlanReviewer
    ) -> None:
        """A BLOCKED label appearing in flight stops writes from a stale verdict."""
        with (
            patch.object(reviewer, "_fetch_issue_comments", return_value=[]),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_json",
                return_value={"labels": [{"name": STATE_PLAN_BLOCKED}]},
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment") as upsert,
            patch("hephaestus.automation.plan_reviewer.gh_issue_edit_labels") as edit_labels,
            pytest.raises(RuntimeError, match="blocked pending external intervention"),
        ):
            reviewer._post_review(123, "Concrete review.\n\nstate:plan-go")

        upsert.assert_not_called()
        edit_labels.assert_not_called()


class TestLatestReviewUsesAuthoritativeLabel:
    """Standalone convergence never derives approval from comment prose."""

    @pytest.mark.parametrize(
        ("labels", "expected"),
        [([STATE_PLAN_GO], True), ([STATE_PLAN_NO_GO], False), ([], False)],
    )
    def test_gate_uses_issue_labels_only(
        self, reviewer: PlanReviewer, labels: list[str], expected: bool
    ) -> None:
        with patch(
            "hephaestus.automation.plan_reviewer.gh_issue_json",
            return_value={"labels": [{"name": label} for label in labels]},
        ):
            assert reviewer._latest_review_is_final(123) is expected

    def test_legacy_go_comment_cannot_grant_approval(self, reviewer: PlanReviewer) -> None:
        reviewer._comments_cache[123] = [
            {"body": "## 🔍 Plan Review\n\nVerdict: GO"},
        ]
        with patch(
            "hephaestus.automation.plan_reviewer.gh_issue_json",
            return_value={"labels": []},
        ):
            assert reviewer._latest_review_is_final(123) is False


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
            patch.object(reviewer, "_read_plan_state_labels", return_value=[STATE_NEEDS_PLAN]),
            patch.object(reviewer, "_get_latest_plan", return_value=None),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment"
            ) as mock_upsert,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_upsert.assert_not_called()

    def test_review_skipped_if_latest_review_is_final(self, reviewer: PlanReviewer) -> None:
        """When the latest plan review is GO, skip posting."""
        with (
            patch.object(reviewer, "_read_plan_state_labels", return_value=[STATE_PLAN_GO]),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment"
            ) as mock_upsert,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_upsert.assert_not_called()

    def test_review_skipped_when_plan_is_blocked(self, reviewer: PlanReviewer) -> None:
        """Standalone admission never invokes an agent for an operator-blocked issue."""
        with (
            patch.object(reviewer, "_read_plan_state_labels", return_value=[STATE_PLAN_BLOCKED]),
            patch.object(reviewer, "_ensure_blocked_audit") as ensure_audit,
            patch.object(reviewer, "_get_latest_plan") as get_plan,
            patch.object(reviewer, "_run_claude_analysis") as run_review,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        assert result.already_reviewed is True
        ensure_audit.assert_called_once_with(123)
        get_plan.assert_not_called()
        run_review.assert_not_called()

    def test_dry_run_blocked_issue_does_not_repair_audit(self, reviewer: PlanReviewer) -> None:
        """Dry-run observes the operator latch without making audit writes."""
        reviewer.options.dry_run = True
        with (
            patch.object(reviewer, "_read_plan_state_labels", return_value=[STATE_PLAN_BLOCKED]),
            patch.object(reviewer, "_ensure_blocked_audit") as ensure_audit,
            patch.object(reviewer, "_run_claude_analysis") as run_review,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        assert result.already_reviewed is True
        ensure_audit.assert_not_called()
        run_review.assert_not_called()

    def test_admission_label_read_failure_fails_closed(self, reviewer: PlanReviewer) -> None:
        """GitHub unavailability is not equivalent to an eligible review state."""
        with (
            patch.object(
                reviewer,
                "_read_plan_state_labels",
                side_effect=RuntimeError("labels unavailable"),
            ),
            patch.object(reviewer, "_get_latest_plan") as get_plan,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is False
        assert "labels unavailable" in str(result.error)
        get_plan.assert_not_called()

    @pytest.mark.parametrize(
        "token",
        [STATE_PLAN_GO, STATE_PLAN_NO_GO, STATE_PLAN_BLOCKED],
    )
    def test_review_posted_with_authoritative_state_label(
        self, reviewer: PlanReviewer, token: str
    ) -> None:
        """The standalone path persists each supported state end to end."""
        with (
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value=f"Concrete review explanation.\n\n{token}",
            ),
            patch.object(reviewer, "_fetch_issue_comments", return_value=[]),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment"
            ) as mock_upsert,
            patch("hephaestus.automation.plan_reviewer.gh_issue_edit_labels") as mock_edit_labels,
        ):
            mock_gh_json.side_effect = [
                {"labels": [{"name": STATE_NEEDS_PLAN}]},
                {"title": "Test Issue", "body": "Issue body"},
                {"labels": [{"name": STATE_NEEDS_PLAN}]},
                {"labels": [{"name": token}]},
            ]
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_upsert.assert_called_once()
        assert mock_upsert.call_args[0][1] == "<!-- hephaestus-plan-review:canonical -->"
        posted_body: str = mock_upsert.call_args[0][2]
        assert posted_body.startswith("<!-- hephaestus-plan-review:canonical -->")
        assert "Concrete review explanation." in posted_body
        assert posted_body.rstrip().endswith(token)
        assert "Verdict:" not in posted_body
        assert mock_edit_labels.call_args.kwargs["add"] == [token]

    def test_dry_run_no_post(self, mock_options: PlanReviewerOptions) -> None:
        """dry_run=True → gh_issue_upsert_comment never called."""
        mock_options.dry_run = True
        reviewer = PlanReviewer(mock_options)

        with (
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value="Review text\n\nstate:plan-no-go",
            ),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_upsert_owned_comment"
            ) as mock_upsert,
        ):
            mock_gh_json.side_effect = [
                {"labels": [{"name": STATE_NEEDS_PLAN}]},
                {"title": "Test Issue", "body": "Issue body"},
            ]
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_upsert.assert_not_called()

    def test_returns_failure_when_claude_returns_none(self, reviewer: PlanReviewer) -> None:
        """Returns failed WorkerResult when Claude analysis returns None."""
        with (
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(reviewer, "_run_claude_analysis", return_value=None),
        ):
            mock_gh_json.side_effect = [
                {"labels": [{"name": STATE_NEEDS_PLAN}]},
                {"title": "Test Issue", "body": "Issue body"},
            ]
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
        with (
            patch(
                "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
                return_value=[{"viewerDidAuthor": True, **comment} for comment in comments],
            ) as mock_fetch,
            patch.object(
                reviewer,
                "_read_plan_state_labels",
                return_value=[STATE_NEEDS_PLAN],
            ),
        ):
            # Call both methods that internally use _fetch_issue_comments
            reviewer._latest_review_is_final(123)
            reviewer._get_latest_plan(123)

        assert mock_fetch.call_count == 1, "Expected single API call due to caching"

    def test_api_called_once_per_issue(self, reviewer: PlanReviewer) -> None:
        """Different issue numbers each get their own API call."""
        comments_123 = [{"body": "## Implementation Plan\n\nIssue 123"}]
        comments_456 = [{"body": "## Implementation Plan\n\nIssue 456"}]

        call_count = 0

        def _side_effect(issue_number: int) -> list[dict[str, str | bool]]:
            nonlocal call_count
            call_count += 1
            selected = comments_123 if issue_number == 123 else comments_456
            return [{"viewerDidAuthor": True, **comment} for comment in selected]

        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            side_effect=_side_effect,
        ):
            reviewer._get_latest_plan(123)
            reviewer._get_latest_plan(123)  # should use cache
            reviewer._get_latest_plan(456)  # new issue → new API call
            reviewer._get_latest_plan(456)  # should use cache

        assert call_count == 2

    def test_api_error_propagates(self, reviewer: PlanReviewer) -> None:
        """API failure cannot be treated as an empty durable journal."""
        with (
            patch(
                "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
                side_effect=RuntimeError("network error"),
            ),
            pytest.raises(RuntimeError, match="network error"),
        ):
            reviewer._fetch_issue_comments(999)

    def test_actor_ownership_is_normalized_from_rest_author(self, reviewer: PlanReviewer) -> None:
        """REST metadata identifies actor-owned comments without GraphQL flags."""
        with patch(
            "hephaestus.automation.plan_reviewer.fetch_issue_comments_metadata",
            return_value=[{"body": "plan", "user": {"login": "bot"}}],
        ):
            comments = reviewer._fetch_issue_comments(1928)

        assert comments[0]["viewerDidAuthor"] is True


class TestMain:
    """Smoke tests for plan_reviewer.main()."""

    def test_success_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() returns 0 when every issue is reviewed successfully."""
        from hephaestus.automation import plan_reviewer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            "sys.argv",
            ["plan-reviewer", "--issues", "1", "2", "--no-ui", "--dry-run", "--agent", "claude"],
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
            [
                "plan-reviewer",
                "--issues",
                "1",
                "--no-ui",
                "--dry-run",
                "--json",
                "--agent",
                "claude",
            ],
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
            [
                "plan-reviewer",
                "--issues",
                "1",
                "2",
                "--no-ui",
                "--dry-run",
                "--json",
                "--agent",
                "claude",
            ],
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
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """KeyboardInterrupt with --json emits a 130 envelope."""
        import json as _json

        from hephaestus.automation import plan_reviewer

        report = tmp_path / "report.txt"
        monkeypatch.setenv("HEPH_WORK_REPORT", str(report))
        monkeypatch.setattr(
            "sys.argv",
            [
                "plan-reviewer",
                "--issues",
                "1",
                "--no-ui",
                "--dry-run",
                "--json",
                "--agent",
                "claude",
            ],
        )

        def fake_run(self: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(plan_reviewer.PlanReviewer, "run", fake_run)
        assert plan_reviewer.main() == 130
        payload = _json.loads(capsys.readouterr().out)
        assert payload["exit_code"] == 130
        assert payload["message"] == "interrupted"
        assert report.read_text(encoding="utf-8") == "0"

    def test_dedupes_issue_numbers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Duplicate --issues values are de-duplicated before review runs."""
        from hephaestus.automation import plan_reviewer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            "sys.argv",
            [
                "plan-reviewer",
                "--issues",
                "5",
                "5",
                "5",
                "--no-ui",
                "--dry-run",
                "--agent",
                "claude",
            ],
        )

        seen_issues: list[list[int]] = []

        def fake_run(self: object) -> dict[int, WorkerResult]:
            # self.options is set during PlanReviewer.__init__
            seen_issues.append(list(self.options.issues))  # type: ignore[attr-defined]
            return {5: WorkerResult(issue_number=5, success=True)}

        monkeypatch.setattr(plan_reviewer.PlanReviewer, "run", fake_run)
        assert plan_reviewer.main() == 0
        assert seen_issues == [[5]]

    def test_installs_cooperative_terminal_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() wraps the review workflow in terminal_guard(shutdown.set).

        This wires up both the cooperative double-Ctrl+C escalation and the
        SIGTSTP (Ctrl+Z) handler (#1784) for this looping, multi-issue entrypoint.
        """
        from hephaestus.automation import plan_reviewer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            "sys.argv",
            ["plan-reviewer", "--issues", "1", "--no-ui", "--dry-run", "--agent", "claude"],
        )

        def fake_run(self: object) -> dict[int, WorkerResult]:
            return {1: WorkerResult(issue_number=1, success=True)}

        monkeypatch.setattr(plan_reviewer.PlanReviewer, "run", fake_run)
        with patch("hephaestus.automation.plan_reviewer.terminal_guard") as mock_guard:
            assert plan_reviewer.main() == 0
            mock_guard.assert_called_once()
            (shutdown_fn,), _ = mock_guard.call_args
            assert callable(shutdown_fn)


class TestPlanReviewerAlreadyReviewedFlag:
    """Tests for WorkerResult.already_reviewed flag (#613).

    Re-homed from the deleted test_loop_runner_early_exit.py. ``already_reviewed``
    is the per-issue convergence signal: a short-circuited review (latest verdict
    already GO, or no plan to review) sets it True so it does NOT count as work,
    while an actual review pass leaves it False. ``plan_reviewer.main`` sums the
    False-and-successful results into the work report.
    """

    def _reviewer(self) -> PlanReviewer:
        return PlanReviewer(
            PlanReviewerOptions(issues=[123], dry_run=False, max_workers=1, enable_ui=False)
        )

    def test_skip_already_approved_sets_flag(self) -> None:
        """A latest-GO plan short-circuits with success=True, already_reviewed=True."""
        reviewer = self._reviewer()
        with patch.object(reviewer, "_read_plan_state_labels", return_value=[STATE_PLAN_GO]):
            result = reviewer._review_issue(123, slot_id=0)

        assert result.success is True
        assert result.already_reviewed is True

    def test_skip_no_plan_sets_flag(self) -> None:
        """No plan comment short-circuits with success=True, already_reviewed=True."""
        reviewer = self._reviewer()
        with (
            patch.object(reviewer, "_read_plan_state_labels", return_value=[STATE_NEEDS_PLAN]),
            patch.object(reviewer, "_get_latest_plan", return_value=None),
        ):
            result = reviewer._review_issue(123, slot_id=0)

        assert result.success is True
        assert result.already_reviewed is True

    def test_review_attempt_unsets_flag(self) -> None:
        """A real review pass leaves already_reviewed=False."""
        reviewer = self._reviewer()
        with (
            patch.object(reviewer, "_read_plan_state_labels", return_value=[STATE_NEEDS_PLAN]),
            patch.object(reviewer, "_get_latest_plan", return_value="# Implementation Plan\nDo it"),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value="Looks good\nstate:plan-go",
            ),
            patch.object(reviewer, "_post_review") as mock_post,
        ):
            result = reviewer._review_issue(123, slot_id=0)

        assert result.success is True
        assert result.already_reviewed is False
        mock_post.assert_called_once()

    def test_plan_reviewer_main_writes_correct_work_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() reports only successful, non-skipped reviews as work units."""
        from hephaestus.automation import plan_reviewer as plan_reviewer_mod
        from hephaestus.automation.models import WorkerResult

        # Two genuine reviews, one short-circuited skip, one failure → work=2.
        results = {
            1: WorkerResult(issue_number=1, success=True, already_reviewed=False),
            2: WorkerResult(issue_number=2, success=True, already_reviewed=False),
            3: WorkerResult(issue_number=3, success=True, already_reviewed=True),
            4: WorkerResult(issue_number=4, success=False, already_reviewed=False),
        }
        mock_reviewer = MagicMock()
        mock_reviewer.run.return_value = results
        report = tmp_path / "report.txt"

        monkeypatch.setenv("HEPH_WORK_REPORT", str(report))
        monkeypatch.setattr(
            "sys.argv",
            ["plan-reviewer", "--issues", "1", "2", "3", "4", "--agent", "claude"],
        )
        with patch.object(plan_reviewer_mod, "PlanReviewer", return_value=mock_reviewer):
            rc = plan_reviewer_mod.main()

        # issue 4 failed → rc=1, but the work report still reflects the 2 real reviews.
        assert rc == 1
        assert report.read_text(encoding="utf-8") == "2"
