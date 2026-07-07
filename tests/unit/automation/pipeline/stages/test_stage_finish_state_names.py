"""Naming checks for stage-local finish mini-states."""

from __future__ import annotations

from hephaestus.automation.pipeline.stages import merge_wait, plan_review, pr_review


def test_plan_review_pr_review_and_merge_wait_finish_states_are_distinct() -> None:
    """Each stage should use a unique finish token in persisted state."""
    assert len({pr_review.PR_FINISH, plan_review.PLAN_FINISH, merge_wait.MW_FINISH}) == 3
