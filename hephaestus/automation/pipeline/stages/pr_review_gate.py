# This mixin consumes the stage thread namespace by design.
# ruff: noqa: F403, F405
from .base import _terminal_pr_outcome
from .pr_review_threads import *


class PrReviewGate(_PrReviewHost):
    """Decide approval from GitHub: no unresolved review thread means GO."""

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Approve the pull request, or send it back to implementation.

        Any unresolved thread, or a finding the diff could not place inline,
        means NO-GO. The review budget is counted per completed review.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.issue is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        payload = item.payload
        audit = payload.get("review_audit")
        if (
            payload.pop("review_audit_failure", False)
            or not isinstance(audit, ReviewAudit)
            or not audit.valid
        ):
            return self._handle_error_verdict(item, None if audit is None else audit)
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            state = ctx.github.gh_pr_state(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not read the review threads (%s)",
                item.issue,
                type(error).__name__,
            )
            return self._handle_error_verdict(item, None)
        if state is not None and (terminal := _terminal_pr_outcome(state, item.pr)):
            return self._cleanup_review_worktree_then(item, terminal)
        payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        payload["review_error_retries"] = 0
        round_done = payload.get("pr_review_round", 0) + 1
        payload["pr_review_round"] = round_done
        item.attempts["pr_review_iter"] = item.attempts.get("pr_review_iter", 0) + 1
        body_only = payload.get("review_body_only_findings") or []
        if not live_threads and not body_only:
            outcome = self._write_go(item, ctx)
            return self._cleanup_review_worktree_then(item, outcome)
        return self._handle_non_go(item, ctx, live_threads, body_only, round_done)

    def _handle_non_go(
        self,
        item: WorkItem,
        ctx: StageContext,
        live_threads: list[dict[str, Any]],
        body_only: list[dict[str, Any]],
        round_done: int,
    ) -> StepResult:
        """Mark the pull request no-go and hand the findings to implementation."""
        payload = item.payload
        hard_cap = ctx.budget("pr_review_hard")
        if round_done >= hard_cap:
            logger.warning(
                "pr_review:%d: %d review rounds without approval; applying %s",
                _issue_number(item),
                round_done,
                STATE_SKIP,
            )
            write_skip_label(
                _issue_number(item),
                ctx,
                f"PR review ran {round_done} rounds without approval "
                f"({len(live_threads)} unresolved threads). Push commits that address "
                f"the review, then remove this label to re-enter the loop.",
            )
            return self._cleanup_review_worktree_then(
                item, StageOutcome(Disposition.SKIP, "exhaustion")
            )
        remediation = _normalize_remediation_threads(live_threads)
        payload["remediation_threads"] = remediation
        payload["remediation_thread_snapshots"] = [dict(thread) for thread in live_threads]
        payload["unresolved_threads_before_address"] = len(remediation)
        if body_only:
            payload["unaddressed_findings"] = [
                {
                    "path": str(finding.get("path") or ""),
                    "line": finding.get("line"),
                    "body": str(finding.get("body") or ""),
                }
                for finding in body_only
                if isinstance(finding, dict)
            ]
        return self._handoff_implementation(item, ctx)

    def _handle_error_verdict(self, item: WorkItem, audit: Any) -> StepResult:
        """Retry an unreadable review, without burning a round or changing a label."""
        payload = item.payload
        reason = "no review found" if audit is None else "review unreadable"
        retries = payload.get("review_error_retries", 0) + 1
        payload["review_error_retries"] = retries
        if retries > REVIEW_ERROR_RETRY_CAP:
            logger.error(
                "pr_review:%s: %s; %d consecutive reviewer failures — failing back to "
                "implementation",
                item.issue,
                reason,
                retries,
            )
            return self._cleanup_review_worktree_then(item, self._fail_back_agent_error(item))
        logger.warning(
            "pr_review:%s: %s; retry %d/%d (no round burned)",
            item.issue,
            reason,
            retries,
            REVIEW_ERROR_RETRY_CAP,
        )
        return self._cleanup_review_worktree_then(item, StageOutcome(Disposition.RETRY, reason))

    @staticmethod
    def _fail_back_agent_error(item: WorkItem) -> StageOutcome:
        """Fail back ``agent_error``. The flag makes the implementation gate count the trip."""
        item.payload["agent_error_failback"] = True
        return StageOutcome(Disposition.FAIL_BACK, "agent_error")

    @staticmethod
    def _write_no_go(item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Apply ``state:implementation-no-go``. The adapter reads the labels back."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        try:
            ctx.github.mark_pr_implementation_no_go(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review: failed to mark PR #%d implementation-no-go: %s", item.pr, error
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_label_failed")
        return None

    def _write_go(self, item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Apply ``state:implementation-go``. The merge stage arms auto-merge from it."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        try:
            ctx.github.mark_pr_implementation_go(item.pr)
        except Exception as error:
            logger.warning("pr_review: failed to mark PR #%d implementation-go: %s", item.pr, error)
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_label_failed")
        return StageOutcome(Disposition.ADVANCE, "review approved; merge wait pending")
