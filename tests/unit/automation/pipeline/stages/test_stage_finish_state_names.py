"""Naming checks for stage-local finish mini-states."""

from __future__ import annotations

from hephaestus.automation.pipeline.stages import merge_wait, plan_review, pr_review


def test_active_finish_tokens_are_stage_qualified() -> None:
    """Only stages with active finish states expose their qualified tokens."""
    assert plan_review.PLAN_FINISH == "PLAN_FINISH"
    assert merge_wait.MW_FINISH == "MW_FINISH"
    assert not hasattr(pr_review, "PR_FINISH")
    assert not hasattr(pr_review, "FINISH")
