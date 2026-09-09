"""Restore retained review evidence only after fresh host verification."""

from hephaestus.automation.rebase_review_receipt import RebaseReviewRecord

from ..rebase_review import RebaseReviewProof
from .base import (
    Disposition,
    GitJob,
    JobRequest,
    JobResult,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
)

_PENDING = "_rebase_review_recovery_pending"
_ERROR = "_rebase_review_recovery_error"
_RECORD = "pending_review_rebase_record"
_IDENTITY_FIELDS = (
    "repository",
    "issue_number",
    "pr_number",
    "reviewed_head_sha",
    "reviewed_base_sha",
    "source_head_sha",
    "target_base_sha",
    "resulting_head_sha",
    "resulting_tree_sha",
    "original_audit_id",
)


def recover_rebase_review(item: WorkItem, ctx: StageContext) -> StepResult | None:
    """Verify a retained record before the stage can submit merge checks."""
    if _ERROR in item.payload:
        return StageOutcome(Disposition.FINISH_FAIL, "rebase_review_recovery_failed")
    if _RECORD not in item.payload:
        return None
    record = item.payload[_RECORD]
    if (
        not isinstance(record, RebaseReviewRecord)
        or record.state != "active"
        or record.repository != f"{ctx.org}/{item.repo}"
        or record.issue_number != item.issue
        or record.pr_number != item.pr
        or item.payload.get("host_verification_bootstrap_proof") is not None
    ):
        return StageOutcome(Disposition.FINISH_FAIL, "rebase_review_recovery_invalid")
    if _PENDING in item.payload:
        return StageOutcome(Disposition.FINISH_FAIL, "rebase_review_recovery_incomplete")
    item.payload[_PENDING] = record
    return JobRequest(
        GitJob(
            repo=item.repo,
            op="verify_rebase_review",
            timeout_s=ctx.config.rebase_timeout,
            expected_repository=record.repository,
            kwargs={"record": record, "repo_root": str(ctx.paths.repo_root)},
            descr="restore_rebase_review",
        ),
        on_done_state="MERGE",
    )


def receive_rebase_review(item: WorkItem, result: JobResult) -> bool:
    """Accept only a host proof for the complete retained record identity."""
    if _PENDING not in item.payload:
        return False
    record = item.payload.pop(_PENDING)
    proof = result.value
    if (
        not result.ok
        or not isinstance(record, RebaseReviewRecord)
        or not isinstance(proof, RebaseReviewProof)
        or any(getattr(proof, name) != getattr(record, name) for name in _IDENTITY_FIELDS)
        or item.payload.get(_RECORD) != record
    ):
        item.payload[_ERROR] = True
        return True
    item.payload["retained_rebase_review_proof"] = proof
    item.payload["reviewed_pr_head_sha"] = record.reviewed_head_sha
    item.payload["review_audit"] = record.audit
    item.payload["reviewed_pr_proof_generation"] = 1
    for key in (
        _RECORD,
        "merge_readiness_head_sha",
        "merge_readiness_proof_generation",
        "merge_readiness_deadline_s",
        "merge_readiness_polls",
        "merge_readiness_declined_fingerprint",
        "merge_queue_admitted_head_sha",
        "merge_queue_admitted_proof_generation",
    ):
        item.payload.pop(key, None)
    return True
