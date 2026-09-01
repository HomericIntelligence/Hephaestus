"""Durable implementation-go audit publication states."""

# This mixin consumes the shared PR-review stage namespace by design.
# ruff: noqa: F403, F405
from .pr_review_threads import *


class PrReviewAudit:
    """Persist and publish clean-review audits without repeating review work."""

    def _handle_clean_go(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Enter the durable audit receipt state after a clean structural proof."""
        if item.pr is None or item.issue is None:
            return self._fail_back_agent_error(item)  # type: ignore[attr-defined,no-any-return]
        logger.info(
            "pr_review:%d: clean structural audit; advancing PR #%d to merge wait",
            item.issue,
            item.pr,
        )
        audit = item.payload.get("review_audit")
        head_sha = str(item.payload.get("reviewed_pr_head_sha") or "")
        if (
            not isinstance(audit, ReviewAudit)
            or not audit.valid
            or audit.verdict != "GO"
            or audit.findings
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        if not is_full_commit_sha(head_sha):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        item.payload["pending_implementation_go_audit"] = audit
        item.payload["pending_implementation_go_audit_head"] = head_sha
        return self._go_audit_receipt(item, ctx)

    @staticmethod
    def _audit_retry(item: WorkItem, *, reason: str) -> StageOutcome:
        """Bound and back off publication recovery without re-running EVAL."""
        retries = item.payload.get("implementation_go_audit_retries", 0)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        retries += 1
        item.payload["implementation_go_audit_retries"] = retries
        if retries > IMPLEMENTATION_GO_AUDIT_RETRY_CAP:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_failed")
        item.payload["retry_delay_s"] = float(2 ** (retries - 1))
        return StageOutcome(Disposition.RETRY, reason)

    def _go_audit_receipt(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Persist the exact-head audit before applying the GO label."""
        if item.pr is None or item.issue is None:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        audit = item.payload.get("pending_implementation_go_audit")
        head_sha = str(item.payload.get("pending_implementation_go_audit_head") or "")
        if (
            not isinstance(audit, ReviewAudit)
            or not audit.valid
            or audit.verdict != "GO"
            or not is_full_commit_sha(head_sha)
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        try:
            ctx.github.persist_pending_implementation_go_audit(item.pr, head_sha, audit)
        except Exception as error:
            logger.warning(
                "pr_review:%d: failed to persist implementation-go audit receipt (%s)",
                item.issue,
                type(error).__name__,
            )
            item.state = GO_AUDIT_RECEIPT
            return self._audit_retry(item, reason="implementation_go_audit_receipt_retry")
        item.payload["reviewed_pr_head_sha"] = head_sha
        outcome = self._write_go(item, ctx)  # type: ignore[attr-defined]
        if isinstance(outcome, StageOutcome) and outcome.disposition is Disposition.ADVANCE:
            item.payload["implementation_go_audit_retries"] = 0
            return self._go_audit_publish(item, ctx)
        return outcome  # type: ignore[no-any-return]

    def _go_audit_publish(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Reconcile the public audit without repeating review or label writes."""
        if item.pr is None or item.issue is None:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        audit = item.payload.get("pending_implementation_go_audit")
        head_sha = str(item.payload.get("pending_implementation_go_audit_head") or "")
        if (
            not isinstance(audit, ReviewAudit)
            or not audit.valid
            or audit.verdict != "GO"
            or not is_full_commit_sha(head_sha)
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        try:
            ctx.github.publish_implementation_go_audit(item.pr, head_sha, audit)
            ctx.github.clear_pending_implementation_go_audit(item.pr, head_sha)
        except Exception as error:
            logger.warning(
                "pr_review:%d: failed to publish implementation-go audit (%s)",
                item.issue,
                type(error).__name__,
            )
            item.state = GO_AUDIT_PUBLISH
            return self._audit_retry(item, reason="implementation_go_audit_retry")
        item.payload.pop("implementation_go_audit_retries", None)
        item.payload.pop("pending_implementation_go_audit", None)
        item.payload.pop("pending_implementation_go_audit_head", None)
        return self._cleanup_review_worktree_then(  # type: ignore[attr-defined,no-any-return]
            item,
            StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending"),
        )
