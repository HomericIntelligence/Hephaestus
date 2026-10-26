"""Queue-owned PR review and implementation-state admission."""

from collections.abc import Callable
from typing import cast

from .base import (
    Continue,
    Disposition,
    Stage,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
)
from .pr_review_gate import PrReviewGate
from .pr_review_jobs import PrReviewJobs
from .pr_review_post import PrReviewPost
from .pr_review_threads import _STEP_HANDLER_NAMES, ENTER, logger


class PrReviewStage(PrReviewJobs, PrReviewPost, PrReviewGate, Stage):
    """Public stage façade over the review jobs and the approval gate."""

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next PR-review action for the item's current state."""
        if item.issue is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        if item.pr is None:
            # Nothing to review: fail back to implementation, whose PR_CREATE
            # step creates the pull request.
            logger.warning("pr_review:%d: no PR on item; failing back", item.issue)
            return self._fail_back_agent_error(item)
        handler_name = _STEP_HANDLER_NAMES.get(item.state)
        if handler_name is None:
            # A durable item can hold a state that no longer exists. Review it afresh.
            logger.warning(
                "pr_review:%d: unknown state %r; reviewing afresh", item.issue, item.state
            )
            return Continue(next_state=ENTER)
        handler = cast(Callable[[WorkItem, StageContext], StepResult], getattr(self, handler_name))
        return handler(item, ctx)
