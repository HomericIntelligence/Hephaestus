"""Durable implementation-go audit publication states."""

# This mixin consumes the shared PR-review stage namespace by design.
# ruff: noqa: F403, F405
from hephaestus.automation.review_audit import is_clean_go_review
from hephaestus.automation.review_finding_history import (
    ReviewFindingCompactedOutcomes,
    empty_review_finding_compacted_outcomes,
    normalize_review_finding_collection,
    review_finding_collection_payload,
)

from .pr_review_threads import *
from .pr_review_verification import _repository_validation_complete


class PrReviewAudit:
    """Persist and publish clean-review audits without repeating review work."""

    @staticmethod
    def _require_current_audit(item: WorkItem, ctx: StageContext) -> Continue | None:
        """Require active review evidence before a publication retry can advance."""
        audit = item.payload.get("pending_implementation_go_audit")
        head_sha = item.payload.get("pending_implementation_go_audit_head")
        finding_records = item.payload.get("pending_implementation_go_audit_findings", [])
        compacted_outcomes = item.payload.get(
            "pending_implementation_go_audit_compacted_outcomes",
            empty_review_finding_compacted_outcomes(),
        )
        try:
            normalize_review_finding_collection(
                finding_records,
                compacted_outcomes=compacted_outcomes,
            )
            records_are_valid = True
        except ValueError:
            records_are_valid = False
        if (
            is_clean_go_review(audit)
            and item.payload.get("review_audit") is audit
            and is_full_commit_sha(head_sha)
            and item.payload.get("reviewed_pr_head_sha") == head_sha
            and records_are_valid
            and _repository_validation_complete(item, ctx.org)
        ):
            return None
        # Keep the durable record. A new review can replace or reconcile it,
        # but that record cannot restore this process's review evidence.
        for key in (
            "pending_implementation_go_audit",
            "pending_implementation_go_audit_head",
            "pending_implementation_go_audit_findings",
            "pending_implementation_go_audit_compacted_outcomes",
            "pending_implementation_go_label_confirmed",
            "implementation_go_audit_retries",
        ):
            item.payload.pop(key, None)
        _clear_round_review_state(item)
        return Continue(next_state=ENTER)

    def _handle_clean_go(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Enter the durable audit receipt state after a clean structural proof."""
        if item.pr is None or item.issue is None:
            return self._fail_back_agent_error(item)  # type: ignore[attr-defined,no-any-return]
        if not _repository_validation_complete(item, ctx.org):
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_incomplete")
        logger.info(
            "pr_review:%d: clean structural audit; advancing PR #%d to merge wait",
            item.issue,
            item.pr,
        )
        audit = item.payload.get("review_audit")
        head_sha = str(item.payload.get("reviewed_pr_head_sha") or "")
        if not is_clean_go_review(audit):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        if not is_full_commit_sha(head_sha):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        item.payload["pending_implementation_go_audit"] = audit
        item.payload["pending_implementation_go_audit_head"] = head_sha
        try:
            records, compacted = normalize_review_finding_collection(
                item.payload.get("review_finding_records", []),
                compacted_outcomes=item.payload.get(
                    "review_finding_compacted_outcomes",
                    empty_review_finding_compacted_outcomes(),
                ),
            )
            item.payload["pending_implementation_go_audit_findings"] = [
                dict(record) for record in records
            ]
            item.payload["pending_implementation_go_audit_compacted_outcomes"] = compacted
        except ValueError:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
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

    @staticmethod
    def _restore_pending_go_finding_history(
        item: WorkItem,
    ) -> tuple[list[dict[str, object]], ReviewFindingCompactedOutcomes]:
        """Restore normalized pending audit history to the terminal payload."""
        records, compacted = normalize_review_finding_collection(
            item.payload.get("pending_implementation_go_audit_findings", []),
            compacted_outcomes=item.payload.get(
                "pending_implementation_go_audit_compacted_outcomes",
                empty_review_finding_compacted_outcomes(),
            ),
        )
        restored_records = [dict(record) for record in records]
        item.payload["review_finding_records"] = restored_records
        item.payload["review_finding_compacted_outcomes"] = compacted
        item.payload["carried_review_finding_records"] = [
            dict(record) for record in restored_records
        ]
        item.payload["carried_review_finding_compacted_outcomes"] = compacted
        return restored_records, compacted

    def _go_audit_receipt(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Persist the exact-head audit before applying the GO label."""
        if recovery := self._require_current_audit(item, ctx):
            return recovery
        if item.pr is None or item.issue is None:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        audit = item.payload.get("pending_implementation_go_audit")
        head_sha = str(item.payload.get("pending_implementation_go_audit_head") or "")
        if not is_clean_go_review(audit) or not is_full_commit_sha(head_sha):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        try:
            finding_records, compacted_outcomes = self._restore_pending_go_finding_history(item)
            history = (
                review_finding_collection_payload(finding_records, compacted_outcomes)
                if compacted_outcomes.get("identities")
                else finding_records
            )
            ctx.github.persist_pending_implementation_go_audit(
                item.pr,
                head_sha,
                audit,
                finding_records=history,
            )
        except Exception as error:
            logger.warning(
                "pr_review:%d: failed to persist implementation-go audit receipt (%s)",
                item.issue,
                type(error).__name__,
            )
            item.state = GO_AUDIT_RECEIPT
            return self._audit_retry(item, reason="implementation_go_audit_receipt_retry")
        outcome = self._write_go(item, ctx)  # type: ignore[attr-defined]
        if isinstance(outcome, StageOutcome) and outcome.disposition is Disposition.ADVANCE:
            item.payload["implementation_go_audit_retries"] = 0
            return self._go_audit_publish(item, ctx)
        if isinstance(outcome, Continue) and outcome.next_state == REVIEW_WAIT:
            # The exact head changed after the receipt was persisted.  A new
            # review must not inherit a receipt or label proof for the old
            # head, while transport retries deliberately retain those facts.
            item.payload.pop("pending_implementation_go_audit", None)
            item.payload.pop("pending_implementation_go_audit_head", None)
            item.payload.pop("pending_implementation_go_audit_findings", None)
            item.payload.pop("pending_implementation_go_audit_compacted_outcomes", None)
            item.payload.pop("pending_implementation_go_label_confirmed", None)
        return outcome  # type: ignore[no-any-return]

    def _go_audit_publish(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Reconcile the public audit without repeating review or label writes."""
        if recovery := self._require_current_audit(item, ctx):
            return recovery
        if item.pr is None or item.issue is None:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        audit = item.payload.get("pending_implementation_go_audit")
        head_sha = str(item.payload.get("pending_implementation_go_audit_head") or "")
        if not is_clean_go_review(audit) or not is_full_commit_sha(head_sha):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_audit_invalid")
        try:
            finding_records, compacted_outcomes = self._restore_pending_go_finding_history(item)
            history = (
                review_finding_collection_payload(finding_records, compacted_outcomes)
                if compacted_outcomes.get("identities")
                else finding_records
            )
            ctx.github.publish_implementation_go_audit(
                item.pr,
                head_sha,
                audit,
                finding_records=history,
            )
            ctx.github.clear_review_finding_journal(item.pr, head_sha)
            ctx.github.clear_pending_implementation_go_audit(item.pr, head_sha)
        except Exception as error:
            logger.warning(
                "pr_review:%d: failed to publish implementation-go audit (%s)",
                item.issue,
                type(error).__name__,
            )
            item.state = GO_AUDIT_PUBLISH
            return self._audit_retry(item, reason="implementation_go_audit_retry")
        item.payload.pop("retained_rebase_review_proof", None)
        item.payload.pop("pending_review_rebase_record", None)
        item.payload.pop("implementation_go_audit_retries", None)
        item.payload.pop("pending_implementation_go_audit", None)
        item.payload.pop("pending_implementation_go_audit_head", None)
        item.payload.pop("pending_implementation_go_audit_findings", None)
        item.payload.pop("pending_implementation_go_audit_compacted_outcomes", None)
        return self._cleanup_review_worktree_then(  # type: ignore[attr-defined,no-any-return]
            item,
            StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending"),
        )
