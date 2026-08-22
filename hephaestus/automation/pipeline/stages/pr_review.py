# The stage façade re-exports stage-local helper constants for compatibility.
# ruff: noqa: F403
import typing as _typing

from .pr_review_audit import PrReviewAudit
from .pr_review_gate import PrReviewGate
from .pr_review_jobs import PrReviewJobs

if _typing.TYPE_CHECKING:
    from .pr_review_threads import (
        _STEP_HANDLER_NAMES,
        ADOPT_WORKTREE_WAIT as ADOPT_WORKTREE_WAIT,
        CLEANUP_REVIEW_WORKTREE_WAIT as CLEANUP_REVIEW_WORKTREE_WAIT,
        DIRECT_PUSH_REMOTE_CHANGED_RESTART_CAP as DIRECT_PUSH_REMOTE_CHANGED_RESTART_CAP,
        DIRECT_PUSH_RETRY_CAP as DIRECT_PUSH_RETRY_CAP,
        ENTER as ENTER,
        GO_AUDIT_RECEIPT as GO_AUDIT_RECEIPT,
        HOST_VERIFICATION_WAIT as HOST_VERIFICATION_WAIT,
        REVIEW_CHECKOUT_WAIT as REVIEW_CHECKOUT_WAIT,
        REVIEW_ERROR_RETRY_CAP as REVIEW_ERROR_RETRY_CAP,
        Callable,
        Continue,
        Disposition,
        ItemKind,
        StageContext,
        StageOutcome,
        StepResult,
        WorkItem,
        _address_replies as _address_replies,
        _host_verification_receipt_matches as _host_verification_receipt_matches,
        _host_verification_specs as _host_verification_specs,
        _implementation_reply_handoff as _implementation_reply_handoff,
        _is_postable_finding as _is_postable_finding,
        _normalize_remediation_threads as _normalize_remediation_threads,
        _parse_validation_result as _parse_validation_result,
        _pr_is_current_open_head as _pr_is_current_open_head,
        _reviewer_thread_decisions as _reviewer_thread_decisions,
        _validation_receipt_fingerprints as _validation_receipt_fingerprints,
        _validation_thread_snapshots as _validation_thread_snapshots,
        _without_duplicate_live_findings as _without_duplicate_live_findings,
        cast,
        logger,
    )
else:
    from .pr_review_threads import *


class PrReviewStage(PrReviewJobs, PrReviewAudit, PrReviewGate):
    """Public stage façade over review jobs and the approval gate."""

    @staticmethod
    def _fail_back_agent_error(item: WorkItem) -> StageOutcome:
        """Route a bounded reviewer failure back to implementation."""
        item.payload["agent_error_failback"] = True
        return StageOutcome(Disposition.FAIL_BACK, "agent_error")

    @staticmethod
    def _fail_back_implementation_remediation(item: WorkItem) -> StageOutcome:
        """Route unresolved review work back to implementation."""
        item.payload["implementation_remediation"] = True
        return StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")

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
            return Continue(next_state=GO_AUDIT_RECEIPT)
        if (
            item.state == "ENTER"
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

        handler_name = _STEP_HANDLER_NAMES.get(item.state)
        if handler_name is not None:
            handler = cast(
                Callable[[WorkItem, StageContext], StepResult],
                getattr(self, handler_name),
            )
            return handler(item, ctx)

        logger.warning("pr_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")
