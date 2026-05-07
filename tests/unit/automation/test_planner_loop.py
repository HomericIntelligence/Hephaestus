"""Tests for the strict review loop in planner.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hephaestus.automation.models import PlannerOptions
from hephaestus.automation.planner import MAX_REVIEW_ITERATIONS, Planner


@pytest.fixture
def options() -> PlannerOptions:
    """PlannerOptions for the loop tests (advise disabled)."""
    return PlannerOptions(
        issues=[123],
        dry_run=False,
        force=False,
        parallel=1,
        skip_closed=True,
        enable_advise=False,  # skip advise entirely so we don't need to mock Mnemosyne
    )


@pytest.fixture
def planner(options: PlannerOptions) -> Planner:
    """Planner instance for loop tests."""
    return Planner(options)


def _go_review() -> str:
    return "All good.\n\nGrade: A\nVerdict: GO\n"


def _nogo_review(grade: str = "D") -> str:
    return f"Significant gaps.\n\nGrade: {grade}\nVerdict: NOGO\n"


class TestRunPlanReviewLoop:
    """Integration tests for _run_plan_review_loop end-to-end."""

    def test_terminates_immediately_on_iter0_go(self, planner: Planner) -> None:
        """A GO on R0 must skip iterations 1 and 2."""
        with (
            patch(
                "hephaestus.automation.planner.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value="plan v0") as mock_plan,
            patch.object(planner, "_capture_planner_learnings", return_value="learn0"),
            patch.object(planner, "_run_plan_review", return_value=_go_review()) as mock_review,
        ):
            plan, review, iters = planner._run_plan_review_loop(123, slot_id=0)

        assert iters == 1
        assert plan == "plan v0"
        assert "Verdict: GO" in (review or "")
        assert mock_plan.call_count == 1
        assert mock_review.call_count == 1

    def test_runs_all_3_iterations_on_sustained_nogo(self, planner: Planner) -> None:
        """When every review says NOGO, the loop runs exactly 3 iterations."""
        with (
            patch(
                "hephaestus.automation.planner.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                planner,
                "_generate_plan",
                side_effect=["plan v0", "plan v1", "plan v2"],
            ) as mock_plan,
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_nogo_review("D"), _nogo_review("C"), _nogo_review("B")],
            ) as mock_review,
        ):
            plan, review, iters = planner._run_plan_review_loop(123, slot_id=0)

        assert iters == MAX_REVIEW_ITERATIONS == 3
        assert plan == "plan v2"
        assert "Verdict: NOGO" in (review or "")
        assert mock_plan.call_count == 3
        assert mock_review.call_count == 3

    def test_terminates_on_go_at_iter1(self, planner: Planner) -> None:
        with (
            patch(
                "hephaestus.automation.planner.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                planner, "_generate_plan", side_effect=["plan v0", "plan v1"]
            ) as mock_plan,
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_nogo_review(), _go_review()],
            ) as mock_review,
        ):
            plan, _review, iters = planner._run_plan_review_loop(123, slot_id=0)

        assert iters == 2
        assert plan == "plan v1"
        assert mock_plan.call_count == 2
        assert mock_review.call_count == 2

    def test_prior_review_passed_to_next_plan(self, planner: Planner) -> None:
        """Iteration N+1 must receive iteration N's review as `prior_review`."""
        review_iter0 = _nogo_review("D")
        with (
            patch(
                "hephaestus.automation.planner.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                planner, "_generate_plan", side_effect=["plan v0", "plan v1"]
            ) as mock_plan,
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[review_iter0, _go_review()],
            ),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        # First plan call: prior_review is None
        first_kwargs = mock_plan.call_args_list[0].kwargs
        assert first_kwargs.get("prior_review") is None

        # Second plan call: prior_review is the iter-0 NoGo review
        second_kwargs = mock_plan.call_args_list[1].kwargs
        assert second_kwargs.get("prior_review") == review_iter0


class TestCapturePlannerLearnings:
    """Learnings capture must fail safely (return '') without raising."""

    def test_returns_empty_on_call_claude_failure(self, planner: Planner) -> None:
        """Planner-learnings call failure is non-fatal — returns empty string."""
        with patch.object(planner, "_call_claude", side_effect=RuntimeError("claude down")):
            out = planner._capture_planner_learnings(123, "plan text")
        assert out == ""

    def test_returns_call_claude_output(self, planner: Planner) -> None:
        """Successful learnings call returns the output verbatim."""
        with patch.object(planner, "_call_claude", return_value="- learning A\n- learning B"):
            out = planner._capture_planner_learnings(123, "plan text")
        assert "learning A" in out


class TestRunPlanReview:
    """The reviewer must fail safely to NoGo when the call errors."""

    def test_review_call_failure_returns_synthetic_nogo(self, planner: Planner) -> None:
        """Reviewer call failure synthesizes a NOGO verdict so the loop continues."""
        with patch.object(planner, "_call_claude", side_effect=RuntimeError("review down")):
            out = planner._run_plan_review(
                issue_number=1,
                issue_title="t",
                issue_body="b",
                plan_text="p",
                learnings="",
                iteration=0,
                prior_review=None,
            )
        assert "Verdict: NOGO" in out
        assert "Grade: F" in out


class TestPostPlanWithReview:
    """The final plan comment must include the final-review block."""

    def test_post_plan_includes_review_when_present(self, planner: Planner) -> None:
        with patch("hephaestus.automation.planner.gh_issue_comment") as mock_cmt:
            planner._post_plan(123, "plan body", final_review="Grade: B\nVerdict: GO")
        body = mock_cmt.call_args[0][1]
        assert "plan body" in body
        assert "Grade: B" in body
        assert "Verdict: GO" in body

    def test_post_plan_omits_review_block_when_none(self, planner: Planner) -> None:
        with patch("hephaestus.automation.planner.gh_issue_comment") as mock_cmt:
            planner._post_plan(123, "plan body", final_review=None)
        body = mock_cmt.call_args[0][1]
        assert "plan body" in body
        assert "Final review verdict" not in body
