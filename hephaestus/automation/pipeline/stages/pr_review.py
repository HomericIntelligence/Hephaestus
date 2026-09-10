"""Queue-owned PR review and implementation-state admission."""

from collections.abc import Callable
from typing import cast

from ..work_item import ItemKind
from .base import (
    Continue,
    Disposition,
    Stage,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _reviewed_terminal_pr_outcome,
)
from .pr_review_audit import PrReviewAudit
from .pr_review_gate import PrReviewGate
from .pr_review_jobs import PrReviewJobs
from .pr_review_threads import _STEP_HANDLER_NAMES, ENTER, GO_AUDIT_RECEIPT, logger


class PrReviewStage(PrReviewJobs, PrReviewAudit, PrReviewGate, Stage):
    """Public stage façade over review jobs and the approval gate."""

    @staticmethod
    def _fail_back_agent_error(item: WorkItem) -> StageOutcome:
        """Route a bounded reviewer failure back to implementation."""
        item.payload["agent_error_failback"] = True
        return StageOutcome(Disposition.FAIL_BACK, "agent_error")

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next PR-review action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if item.issue is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        if item.pr is None:
            # Nothing to review: fail back to implementation, whose
            # PR_CREATE step is the designated (re)creation path.
            logger.warning("pr_review:%d: no PR on item; failing back", item.issue)
            return self._fail_back_agent_error(item)
        if item.state == ENTER and item.payload.get("pending_implementation_go_audit"):
            return self._require_current_audit(item) or Continue(next_state=GO_AUDIT_RECEIPT)
        if (
            item.state == "ENTER"
            and item.payload.pop("scope_dependency_entry_reconciled", False)
            and not item.worktree
            and item.pr is not None
            and (
                item.kind is ItemKind.PR
                or item.payload.get("existing_pr")
                or item.payload.get("reviewer_checkout_needed")
            )
        ):
            # A PR-review entry has no adopted checkout yet. It must never be
            # reviewed from the shared repository root, including when an
            # issue seed was routed to an already-open PR by drive-green.
            # It also must not detour through fresh implementation merely to
            # obtain that checkout: issue-level state:skip is intentionally
            # absolute for fresh implementation but is not a reason to skip
            # an existing PR review.
            return self._adopt_direct_pr_worktree(item, ctx)

        if item.state in {"VALIDATE_WAIT", "EVAL", "POST", "POST_APPLY", "GO_AUDIT_RECEIPT"} and (
            terminal := _reviewed_terminal_pr_outcome(item, ctx)
        ):
            return terminal
        handler_name = _STEP_HANDLER_NAMES.get(item.state)
        if handler_name is not None:
            handler = cast(
                Callable[[WorkItem, StageContext], StepResult],
                getattr(self, handler_name),
            )
            return handler(item, ctx)

        logger.warning("pr_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")
