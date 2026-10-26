"""Clear transient review state before a new review round."""

from ..work_item import WorkItem

#: Round-scoped payload keys cleared when a review starts, so a later round
#: never replays an earlier round's results.
_ROUND_PAYLOAD_KEYS = (
    "review_audit",
    "review_feedback",
    "review_failed",
    "validation_result",
    "review_threads",
    "review_body_only_findings",
    "remediation_threads",
    "remediation_thread_snapshots",
    "unaddressed_findings",
    "review_audit_failure",
    "prior_comments_json",
    "validation_threads",
    "scope_retraction_paths",
    "reviewed_pr_base_sha",
    "review_diff_base_sha",
    "review_target_base_sha",
)


def _clear_round_review_state(item: WorkItem) -> None:
    """Discard review evidence that cannot survive a new commit."""
    for key in _ROUND_PAYLOAD_KEYS:
        item.payload.pop(key, None)
    item.payload.pop("retained_rebase_review_proof", None)
    item.payload.pop("pending_review_rebase_record", None)
    item.payload.pop("reviewed_pr_head_sha", None)
    item.payload.pop("reviewed_pr_node_id", None)
    item.payload.pop("pr_node_id", None)
    item.payload.pop("pr_diff", None)
