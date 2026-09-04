"""Review-stage head, checkout, and cleanup barriers."""

from __future__ import annotations

import logging
from typing import Any

from .base import (
    GIT_JOB_TIMEOUT_S,
    Continue,
    Disposition,
    GitJob,
    JobRequest,
    JobResult,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _is_confirmed_open_unarmed,
    stage_timeout,
)
from .pr_review_threads import (
    CLEANUP_REVIEW_WORKTREE_WAIT,
    REVIEW_CHECKOUT_RETRY_CAP,
    REVIEW_WAIT,
)
from .repo import is_full_commit_sha

logger = logging.getLogger(__name__)

_REVIEW_HEAD_VISIBILITY_RETRIES = "review_head_visibility_retries"


class PrReviewHeadVisibilityMixin:
    """Wait for the published implementation head before review work."""

    @staticmethod
    def _implementation_head_visibility_outcome(
        item: WorkItem,
        observed_head: object,
        *,
        wait_note: str = "review_head_visibility_wait",
    ) -> StageOutcome | None:
        """Wait for the exact writer head to become visible on the PR."""
        source_revision = item.payload.get("_impl_source_revision")
        if source_revision is None:
            return None
        if not is_full_commit_sha(source_revision):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_source_revision_invalid")
        if observed_head == source_revision:
            return None
        return PrReviewHeadVisibilityMixin._record_review_head_drift(item, wait_note=wait_note)

    @staticmethod
    def _record_review_head_drift(
        item: WorkItem,
        *,
        wait_note: str,
    ) -> StageOutcome:
        """Consume the bounded retry budget for a head visibility wait."""
        retries = item.payload.get(_REVIEW_HEAD_VISIBILITY_RETRIES, 0)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            return StageOutcome(Disposition.FINISH_FAIL, "review_head_visibility_invalid")
        retries += 1
        item.payload[_REVIEW_HEAD_VISIBILITY_RETRIES] = retries
        if retries > REVIEW_CHECKOUT_RETRY_CAP:
            return StageOutcome(Disposition.FINISH_FAIL, "review_head_visibility_exhausted")
        item.payload["retry_delay_s"] = float(2 ** (retries - 1))
        return StageOutcome(Disposition.RETRY, wait_note)

    def _check_implementation_head_visibility(
        self,
        item: WorkItem,
        ctx: StageContext,
        *,
        wait_note: str = "review_head_visibility_wait",
    ) -> StageOutcome | None:
        """Check the live PR head against the implementation source head."""
        if item.pr is None:
            return None
        context = ctx.github.pr_review_context(item.pr)
        observed_head = context.get("pr_head_sha") if isinstance(context, dict) else None
        return self._implementation_head_visibility_outcome(
            item, observed_head, wait_note=wait_note
        )


class PrReviewCheckoutMixin:
    """Store immutable checkout and detached-worktree receipts."""

    @staticmethod
    def _consume_review_checkout_result(item: WorkItem, result: JobResult) -> bool:
        """Store the review checkout barrier result when one is pending."""
        if not item.payload.pop("review_checkout_pending", None):
            return False
        if not result.ok:
            item.payload["review_checkout_error"] = result.error or "checkout job failed"
            return True
        value = result.value
        ready = bool(isinstance(value, dict) and value.get("ready"))
        reason = value.get("reason") if isinstance(value, dict) else None
        if isinstance(reason, str) and reason:
            item.payload["review_checkout_reason"] = reason
        review_diff = value.get("diff") if isinstance(value, dict) else None
        review_base = value.get("base") if isinstance(value, dict) else None
        changed_paths = value.get("changed_paths") if isinstance(value, dict) else None
        if ready and not isinstance(review_diff, str):
            item.payload["review_checkout_error"] = "checkout job returned no bound diff"
            ready = False
        expected_head = item.payload.get("review_checkout_expected_head")
        if ready and (not is_full_commit_sha(expected_head) or value.get("head") != expected_head):
            item.payload["review_checkout_error"] = (
                "checkout job returned no immutable head receipt"
            )
            ready = False
        if ready:
            item.payload["pr_diff"] = review_diff
            if isinstance(changed_paths, list) and all(
                isinstance(path, str) and bool(path) for path in changed_paths
            ):
                item.payload["review_changed_paths"] = list(changed_paths)
            if is_full_commit_sha(review_base):
                item.payload["reviewed_pr_base_sha"] = review_base
        item.payload["review_checkout_ready"] = ready
        return True

    @staticmethod
    def _on_direct_pr_worktree_done(item: WorkItem, result: JobResult) -> None:
        """Record the exact checkout created for a direct PR review."""
        if not result.ok:
            logger.warning("pr_review:%s: direct PR worktree failed: %s", item.issue, result.error)
            item.worktree = ""
            item.payload["direct_pr_worktree_error"] = result.error or "worktree job failed"
            return
        value = result.value
        if (
            type(value) is not dict
            or set(value) != {"path", "head_sha", "detached", "dirty"}
            or not isinstance(value.get("path"), str)
            or not value.get("path")
            or not is_full_commit_sha(value.get("head_sha"))
            or value.get("detached") is not True
            or value.get("dirty") is not False
        ):
            item.worktree = ""
            item.payload["direct_pr_worktree_error"] = (
                "worktree job returned an invalid immutable detached receipt"
            )
            return
        item.worktree = value["path"]
        item.payload["direct_pr_worktree_dirty"] = False
        item.payload["direct_pr_worktree"] = item.worktree
        item.payload["review_worktree"] = item.worktree
        item.payload["review_worktree_expected_head"] = value["head_sha"]


class PrReviewCleanupMixin:
    """Remove detached review worktrees before stage transitions."""

    def _cleanup_review_worktree_then(
        self: Any,
        item: WorkItem,
        outcome: StageOutcome,
    ) -> StepResult:
        """Remove the detached review snapshot before leaving this stage."""
        review_worktree = item.payload.get("review_worktree")
        if not isinstance(review_worktree, str) or not review_worktree:
            self._restore_writer_worktree(item)
            if outcome.disposition is Disposition.RETRY and outcome.note in {
                "review_head_drift",
                "review_head_visibility_wait",
            }:
                item.state = REVIEW_WAIT
            return outcome
        item.payload["review_worktree_cleanup_outcome"] = outcome.disposition.value
        item.payload["review_worktree_cleanup_note"] = outcome.note
        item.payload["review_worktree_cleanup_done"] = "pending"
        return Continue(next_state=CLEANUP_REVIEW_WORKTREE_WAIT)

    def _cleanup_review_worktree_wait(
        self: Any,
        item: WorkItem,
        ctx: StageContext,
    ) -> StepResult:
        """Remove the detached reviewer checkout and continue its outcome."""
        review_worktree = item.payload.get("review_worktree")
        if not isinstance(review_worktree, str) or not review_worktree:
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_invalid")
        if item.payload.pop("review_worktree_cleanup_error", None):
            item.worktree = review_worktree
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_failed")
        cleanup_state = item.payload.get("review_worktree_cleanup_done")
        if cleanup_state == "pending":
            expected_head = item.payload.get("review_worktree_expected_head")
            if not is_full_commit_sha(expected_head):
                return StageOutcome(
                    Disposition.FINISH_FAIL, "review_worktree_cleanup_identity_invalid"
                )
            job = GitJob(
                repo=item.repo,
                op="remove_worktree",
                timeout_s=stage_timeout(ctx, "metadata", GIT_JOB_TIMEOUT_S),
                kwargs={
                    "worktree_path": review_worktree,
                    "repo_root": str(ctx.paths.repo_root),
                    "issue_number": item.issue or item.pr or 0,
                    "expected_head": expected_head,
                    "expected_detached": True,
                    "force": False,
                },
                descr="remove_read_only_review_worktree",
            )
            return JobRequest(job, on_done_state=CLEANUP_REVIEW_WORKTREE_WAIT)
        if cleanup_state is not True:
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_state_invalid")
        outcome_value = item.payload.pop("review_worktree_cleanup_outcome", None)
        note = str(item.payload.pop("review_worktree_cleanup_note", "") or "")
        try:
            disposition = Disposition(str(outcome_value))
        except ValueError:
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_outcome_invalid")
        if disposition is Disposition.RETRY:
            item.worktree = ""
            if item.payload.get("writer_worktree"):
                item.payload["reviewer_checkout_needed"] = True
            item.state = "ENTER"
        else:
            self._restore_writer_worktree(item)
        item.payload.pop("review_worktree", None)
        for key in ("direct_pr_worktree", "direct_pr_worktree_dirty"):
            item.payload.pop(key, None)
        item.payload.pop("review_worktree_cleanup_done", None)
        item.payload.pop("review_worktree_expected_head", None)
        return StageOutcome(disposition, note)

    @staticmethod
    def _consume_review_worktree_cleanup_result(item: WorkItem, result: JobResult) -> bool:
        """Store one disposable-review-worktree cleanup result."""
        if item.payload.get("review_worktree_cleanup_done") != "pending":
            return False
        if result.ok:
            item.payload["review_worktree_cleanup_done"] = True
        else:
            item.payload["review_worktree_cleanup_error"] = result.error or "remove worktree failed"
        return True


class PrReviewGoReadbackMixin:
    """Revalidate the non-conditional GO admission boundary."""

    @staticmethod
    def _handle_late_threads_after_go_write(
        item: WorkItem,
        unresolved_threads: int,
        ctx: StageContext,
    ) -> StageOutcome:
        """Stand down after a late thread read without changing labels."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        logger.warning(
            "pr_review:%d: %d review thread(s) appeared during GO admission on PR #%d; "
            "standing down without label changes",
            item.issue,
            unresolved_threads,
            item.pr,
        )
        return StageOutcome(Disposition.FINISH_FAIL, "review_activity_changed")

    @staticmethod
    def _revalidate_go_write(item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Check the GO write against fresh state and labels."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        try:
            state = ctx.github.gh_pr_state(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to revalidate GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        if isinstance(state, dict) and state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        live_head = str(state.get("headRefOid") or "") if isinstance(state, dict) else ""
        source_guard = PrReviewHeadVisibilityMixin._implementation_head_visibility_outcome(
            item, live_head
        )
        if source_guard is not None:
            return source_guard
        if not reviewed_head or not live_head or reviewed_head != live_head:
            item.payload.pop("reviewed_pr_head_sha", None)
            return Continue(next_state=REVIEW_WAIT)
        try:
            live_threads = ctx.github.list_unresolved_review_threads(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to reread review threads after GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "review_threads_unavailable")
        if live_threads:
            return PrReviewGoReadbackMixin._handle_late_threads_after_go_write(
                item,
                len(live_threads),
                ctx,
            )
        try:
            has_go, has_no_go = ctx.github.pr_has_implementation_state_label(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to revalidate GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        if _is_confirmed_open_unarmed(state) and has_go and not has_no_go:
            return None
        return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")


__all__ = [
    "_REVIEW_HEAD_VISIBILITY_RETRIES",
    "PrReviewCheckoutMixin",
    "PrReviewCleanupMixin",
    "PrReviewGoReadbackMixin",
    "PrReviewHeadVisibilityMixin",
]
