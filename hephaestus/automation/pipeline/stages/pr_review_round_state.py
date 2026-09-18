"""Clear transient review evidence before a new review round."""

from ..work_item import WorkItem

#: Round-scoped payload keys cleared at REVIEW_WAIT submission so a failed
#: later round can never replay an earlier round's results.
_ROUND_PAYLOAD_KEYS = (
    "review_audit",
    "review_feedback",
    "review_text",
    "review_failed",
    "validation_result",
    "review_threads",
    "raw_review_threads",
    "posted_thread_ids",
    "remediation_threads",
    "remediation_thread_snapshots",
    "unaddressed_findings",
    "review_audit_failure",
    "prior_comments_json",
    "validation_threads",
    "validation_receipt_fingerprints",
    "validation_pr_metadata_fingerprint",
    "scope_retraction_paths",
    "_scope_expansion_pending_request",
    "_scope_expansion_receipt",
    "_scope_expansion_receipt_error",
    "_scope_expansion_prepared_receipt",
    "reviewed_pr_base_sha",
    "review_change_records",
    "review_diff_base_sha",
    "review_target_base_sha",
    "repository_validation_attempt",
    "repository_validation_source_request",
    "repository_validation_source_result",
    "repository_validation_runtime_request",
    "repository_validation_runtime_result",
    "repository_validation_ci_request",
    "repository_validation_local_request",
    "repository_validation_failure",
    "host_verification_receipts",
    "host_verification_repository_profile",
    "host_verification_failure",
    "host_verification_pending",
)


def _clear_round_review_state(item: WorkItem) -> None:
    """Discard review evidence that cannot survive a head-changing commit."""
    for key in _ROUND_PAYLOAD_KEYS:
        item.payload.pop(key, None)
    item.payload.pop("retained_rebase_review_proof", None)
    item.payload.pop("pending_review_rebase_record", None)
    item.payload.pop("reviewed_pr_head_sha", None)
    item.payload.pop("reviewed_pr_node_id", None)
    item.payload.pop("pr_node_id", None)
    item.payload.pop("pr_diff", None)
    item.payload.pop("review_changed_paths", None)
