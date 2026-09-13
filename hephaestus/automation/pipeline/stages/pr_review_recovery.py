"""Exact-request review publication receipts and pending recovery."""

from pathlib import Path

from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.automation.review_anchors import normalize_review_finding_records
from hephaestus.automation.review_audit import ReviewAudit
from hephaestus.automation.review_finding_history import (
    empty_review_finding_compacted_outcomes,
    normalize_review_finding_collection,
    normalize_review_finding_compacted_outcomes,
)

from ..github_jobs import (
    FrozenJson,
    GitHubJob,
    PrReviewReconciled,
    RecoverPendingReviewFindingsRequest,
)
from .base import (
    Continue,
    Disposition,
    JobRequest,
    JobResult,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    stage_timeout,
)
from .pr_review_findings import _apply_review_receipt
from .pr_review_gate import PrReviewGate
from .pr_review_history import _pending_review_finding, _publication_head
from .pr_review_threads import (
    BLOCKING_SEVERITIES,
    ENTER,
    EVAL,
    GIT_JOB_TIMEOUT_S,
    POST,
    POST_APPLY,
    REVIEW_ERROR_RETRY_CAP,
    REVIEW_WAIT,
    VALIDATE_WAIT,
    _PrReviewHost,
)
from .repo import is_full_commit_sha

_PENDING_GITHUB_REQUEST = "_pending_github_request"
_PR_REVIEW_RECEIPT = "_pr_review_reconciliation_receipt"
_PR_REVIEW_RECEIPT_ERROR = "_pr_review_reconciliation_error"
_PENDING_FINDING_RECOVERY = "_pending_review_finding_recovery"
_PENDING_FINDING_RECOVERY_DEADLINE = "_pending_review_finding_recovery_deadline_s"


class PrReviewRecoveryMixin(_PrReviewHost):
    """Apply exact publication receipts before the next review can start."""

    def _prepare_pending_finding_recovery(
        self, item: WorkItem, ctx: StageContext
    ) -> StepResult | None:
        """Reconcile an exact-head pending publication before a new review."""
        try:
            records = list(
                normalize_review_finding_records(
                    item.payload.get("carried_review_finding_records", [])
                )
            )
            compacted = normalize_review_finding_compacted_outcomes(
                item.payload.get(
                    "carried_review_finding_compacted_outcomes",
                    empty_review_finding_compacted_outcomes(),
                ),
                retained_finding_ids=[record["finding_id"] for record in records],
            )
        except ValueError:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_finding_history_invalid"),
            )
        pending_records = [record for record in records if record["status"] == "pending"]
        if not pending_records:
            return None
        if item.pr is None:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "no_pr"),
            )
        reviewed_head = str(item.payload.get("review_finding_journal_head") or "")
        if not is_full_commit_sha(reviewed_head):
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_finding_history_invalid"),
            )
        active_head = item.payload.get("reviewed_pr_head_sha")
        if is_full_commit_sha(active_head) and active_head != reviewed_head:
            terminal_records = [record for record in records if record["status"] != "pending"]
            effective_records = [dict(record) for record in terminal_records]
            item.payload["carried_review_finding_records"] = effective_records
            item.payload["carried_review_finding_compacted_outcomes"] = compacted
            item.payload["review_finding_records"] = [dict(record) for record in effective_records]
            item.payload["review_finding_compacted_outcomes"] = compacted
            item.payload.pop("pending_finding_recovery_needs_checkout", None)
            item.payload.pop(_PENDING_FINDING_RECOVERY_DEADLINE, None)
            return None
        if any(_publication_head(record) != reviewed_head for record in pending_records):
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_finding_history_invalid"),
            )
        try:
            findings = [_pending_review_finding(record) for record in pending_records]
        except ValueError:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_finding_history_invalid"),
            )
        prior_request = item.payload.get(_PENDING_GITHUB_REQUEST)
        saved_deadline = item.payload.get(_PENDING_FINDING_RECOVERY_DEADLINE)
        deadline_s = (
            prior_request.deadline_s
            if isinstance(prior_request, RecoverPendingReviewFindingsRequest)
            else saved_deadline
            if isinstance(saved_deadline, (int, float)) and not isinstance(saved_deadline, bool)
            else operation_deadline_after(stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S))
        )
        request = RecoverPendingReviewFindingsRequest(
            issue_number=item.issue,
            pr_number=item.pr,
            reviewed_head_sha=reviewed_head,
            findings=FrozenJson.snapshot(findings),
            finding_records=FrozenJson.snapshot(records),
            deadline_s=deadline_s,
            review_diff=str(item.payload.get("pr_diff") or ""),
            compacted_outcomes=FrozenJson.snapshot(compacted),
        )
        if prior_request is None:
            item.payload[_PENDING_GITHUB_REQUEST] = request
        elif prior_request != request:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_finding_recovery_identity_invalid"),
            )
        item.payload["review_finding_records"] = [dict(record) for record in records]
        item.payload["review_finding_compacted_outcomes"] = compacted
        item.payload.pop("pending_finding_recovery_needs_checkout", None)
        item.payload[_PENDING_FINDING_RECOVERY] = True
        return self._pending_finding_recovery_job(item, ctx, request)

    @staticmethod
    def _pending_finding_recovery_job(
        item: WorkItem, ctx: StageContext, request: RecoverPendingReviewFindingsRequest
    ) -> JobRequest:
        """Dispatch one exact saved finding-recovery request."""
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)).resolve(),
                request=request,
                descr="recover_pending_review_findings",
            ),
            on_done_state=POST_APPLY,
        )

    @staticmethod
    def _on_reconciliation_done(item: WorkItem, result: JobResult) -> None:
        """Store only an exact request-bearing reconciliation receipt."""
        if not result.ok:
            retries = item.payload.get("pr_review_reconciliation_retries", 0)
            if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
                item.payload[_PR_REVIEW_RECEIPT_ERROR] = "invalid"
                return
            retries += 1
            item.payload["pr_review_reconciliation_retries"] = retries
            item.payload[_PR_REVIEW_RECEIPT_ERROR] = (
                "retry" if retries <= REVIEW_ERROR_RETRY_CAP else "failed"
            )
            return
        receipt = result.value
        if not isinstance(receipt, PrReviewReconciled) or receipt.request != item.payload.get(
            _PENDING_GITHUB_REQUEST
        ):
            item.payload[_PR_REVIEW_RECEIPT_ERROR] = "invalid"
            return
        item.payload[_PR_REVIEW_RECEIPT] = receipt

    def _post_apply(  # noqa: C901
        self, item: WorkItem, ctx: StageContext
    ) -> StepResult:
        """Apply the matching receipt and hand unresolved work to implementation."""
        pending_finding_recovery = bool(item.payload.get(_PENDING_FINDING_RECOVERY))
        error = item.payload.pop(_PR_REVIEW_RECEIPT_ERROR, None)
        if error == "retry":
            if pending_finding_recovery:
                request = item.payload.get(_PENDING_GITHUB_REQUEST)
                if not isinstance(request, RecoverPendingReviewFindingsRequest):
                    return self._cleanup_review_worktree_then(
                        item,
                        StageOutcome(
                            Disposition.FINISH_FAIL,
                            "review_finding_recovery_identity_invalid",
                        ),
                    )
                return self._pending_finding_recovery_job(item, ctx, request)
            item.state = POST
            return StageOutcome(Disposition.RETRY, "pr_review_reconciliation_retry")
        if error in {"failed", "invalid"}:
            if pending_finding_recovery:
                item.payload.pop(_PENDING_FINDING_RECOVERY, None)
                item.payload.pop(_PENDING_GITHUB_REQUEST, None)
                item.payload.pop(_PENDING_FINDING_RECOVERY_DEADLINE, None)
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        receipt = item.payload.pop(_PR_REVIEW_RECEIPT, None)
        if not isinstance(receipt, PrReviewReconciled) or receipt.request != item.payload.get(
            _PENDING_GITHUB_REQUEST
        ):
            if pending_finding_recovery:
                item.payload.pop(_PENDING_FINDING_RECOVERY, None)
                item.payload.pop(_PENDING_GITHUB_REQUEST, None)
                item.payload.pop(_PENDING_FINDING_RECOVERY_DEADLINE, None)
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload.pop(_PENDING_GITHUB_REQUEST, None)
        item.payload.pop("pr_review_reconciliation_retries", None)
        item.payload.pop(_PENDING_FINDING_RECOVERY, None)
        if receipt.action == "revalidate":
            item.payload.pop("validation_result", None)
            item.payload.pop("validation_threads", None)
            item.payload.pop("validation_receipt_fingerprints", None)
            item.payload.pop("validation_pr_metadata_fingerprint", None)
            if pending_finding_recovery:
                item.payload[_PENDING_FINDING_RECOVERY_DEADLINE] = receipt.request.deadline_s
                item.payload["pending_finding_recovery_needs_checkout"] = True
                item.payload["existing_pr"] = True
                item.payload["scope_dependency_entry_reconciled"] = True
                return Continue(next_state=ENTER)
            return Continue(next_state=VALIDATE_WAIT)
        if receipt.action == "fresh_review":
            item.payload.pop("validation_result", None)
            item.payload.pop("validation_threads", None)
            item.payload.pop("validation_receipt_fingerprints", None)
            item.payload.pop("validation_pr_metadata_fingerprint", None)
            if pending_finding_recovery:
                item.payload[_PENDING_FINDING_RECOVERY_DEADLINE] = receipt.request.deadline_s
                item.payload["pending_finding_recovery_needs_checkout"] = True
                item.payload["existing_pr"] = True
                item.payload["scope_dependency_entry_reconciled"] = True
                if item.payload.get("review_worktree"):
                    return self._cleanup_review_worktree_then(
                        item,
                        StageOutcome(
                            Disposition.RETRY,
                            "pending finding recovery head changed",
                        ),
                    )
                return Continue(next_state=ENTER)
            return Continue(next_state=REVIEW_WAIT)
        if receipt.action == "audit_failure":
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        record_source = (
            receipt.final_finding_records
            if receipt.final_finding_records is not None
            else receipt.request.finding_records
        )
        compacted_source = (
            receipt.final_compacted_outcomes
            if receipt.final_compacted_outcomes is not None
            else receipt.request.compacted_outcomes
        )
        try:
            records, compacted = normalize_review_finding_collection(
                record_source.thaw(),
                compacted_outcomes=None if compacted_source is None else compacted_source.thaw(),
            )
        except ValueError:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["review_finding_records"] = [dict(record) for record in records]
        item.payload["review_finding_compacted_outcomes"] = compacted
        if pending_finding_recovery:
            item.payload.pop(_PENDING_FINDING_RECOVERY_DEADLINE, None)
            item.payload["carried_review_finding_records"] = [dict(record) for record in records]
            item.payload["carried_review_finding_compacted_outcomes"] = compacted
            item.payload["review_finding_journal_head"] = receipt.request.reviewed_head_sha
        item.payload["review_publication_summary"] = {
            "published": [dict(record) for record in records if record["status"] == "published"],
            "corrected": [dict(record) for record in records if record["status"] == "corrected"],
            "could_not_publish": [
                dict(record) for record in records if record["status"] == "not_publishable"
            ],
        }
        if any(
            record["status"] == "not_publishable" and record["severity"] in BLOCKING_SEVERITIES
            for record in records
        ) or any(identity[2] == "n" and identity[3] == "b" for identity in compacted["identities"]):
            no_go_outcome = PrReviewGate._write_no_go(item, ctx)
            if no_go_outcome is not None:
                return no_go_outcome
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(
                    Disposition.FINISH_FAIL,
                    "review_finding_not_publishable",
                ),
            )
        audit = item.payload.get("review_audit")
        if isinstance(audit, ReviewAudit) and audit.scope_expansions:
            return Continue(next_state=EVAL)
        if pending_finding_recovery:
            reviewed_head = item.payload.get("reviewed_pr_head_sha")
            if not is_full_commit_sha(reviewed_head) or (
                item.payload.get("reviewer_checkout_needed") and not item.worktree
            ):
                item.payload["existing_pr"] = True
                item.payload["scope_dependency_entry_reconciled"] = True
                return Continue(next_state=ENTER)
            return self._route_threads_before_broad_review(item, ctx)
        return _apply_review_receipt(
            item,
            receipt,
            remediation_handoff=lambda: self._handoff_implementation(item, ctx),
        )
