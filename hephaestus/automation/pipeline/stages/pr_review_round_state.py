"""Clear transient review evidence before a new review round."""

from ..work_item import WorkItem

#: Round-scoped payload keys cleared at REVIEW_WAIT submission so a failed
#: later round can never replay an earlier round's results.
_ROUND_PAYLOAD_KEYS = (
    "host_verification_bootstrap_proof",
    "host_verification_bootstrap_json",
    "review_status_manifest",
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
    "address_error",
    "address_output",
    "direct_push_retries",
    "detached_push_retry_head_sha",
    "push_no_commit",
    "no_commit_retry_done",
    "unaddressed_findings",
    "review_audit_failure",
    "review_refresh_required",
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
