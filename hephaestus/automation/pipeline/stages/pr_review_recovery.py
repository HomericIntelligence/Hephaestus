"""Pure local-state recovery helpers for the PR-review stage."""

from __future__ import annotations

import logging
from pathlib import Path

from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.automation.session_naming import (
    AGENT_ADDRESS_REVIEW,
    AGENT_PR_REVIEWER,
)

from ..github_jobs import (
    DeliverReplyHandoffRequest,
    GitHubJob,
    bind_delivery_request,
)
from ..reply_handoff import (
    PENDING_IMPLEMENTATION_REPLY_HANDOFF as _PENDING_IMPLEMENTATION_REPLY_HANDOFF,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES as _PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES as _REPLY_VISIBILITY_RETRIES,
)
from ..work_item import WorkItem
from .base import (
    GIT_JOB_TIMEOUT_S,
    Disposition,
    JobRequest,
    StageContext,
    StageOutcome,
    StepResult,
    stage_timeout,
)
from .pr_review_threads import (
    _REPLY_HANDOFF_RECEIPT,
    _REPLY_HANDOFF_RECEIPT_ERROR,
    _clear_round_review_state,
    _issue_number,
)

logger = logging.getLogger(__name__)


def recovery_reply_job(
    item: WorkItem,
    ctx: StageContext,
    pending_request_key: str,
) -> StepResult:
    """Build one exact deadline-bound recovery reply job."""
    if item.issue is None or item.pr is None:
        return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
    handoff = item.payload.get(_PENDING_IMPLEMENTATION_REPLY_HANDOFF)
    retries = item.payload.get(_REPLY_VISIBILITY_RETRIES, 0)
    if (
        not isinstance(handoff, dict)
        or isinstance(retries, bool)
        or not isinstance(retries, int)
        or retries < 0
    ):
        return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
    pending = item.payload.get(pending_request_key)
    if isinstance(pending, DeliverReplyHandoffRequest):
        if pending.deadline_s is None:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        deadline_s = pending.deadline_s
    else:
        deadline_s = operation_deadline_after(stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S))
    try:
        request = bind_delivery_request(
            pending,
            issue_number=item.issue,
            pr_number=item.pr,
            handoff=handoff,
            visibility_retries=retries,
            deadline_s=deadline_s,
        )
    except (TypeError, ValueError):
        return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
    item.payload[pending_request_key] = request
    return JobRequest(
        GitHubJob(
            repo=item.repo,
            repo_root=Path(str(ctx.paths.repo_root)).resolve(),
            request=request,
            descr="recover_implementation_reply_handoff",
        ),
        on_done_state="EVAL",
    )


def empty_diff_outcome(item: WorkItem) -> StageOutcome | None:
    """Reject a thread-free review whose cumulative PR diff is empty."""
    if str(item.payload.get("pr_diff") or "").strip():
        return None
    _clear_round_review_state(item)
    item.payload["empty_diff_reimplementation"] = True
    logger.warning(
        "pr_review:%d: empty cumulative diff; failing back to implementation",
        _issue_number(item),
    )
    return StageOutcome(Disposition.FAIL_BACK, "empty_pr_diff")


def restart_direct_pr_review(item: WorkItem) -> str | None:
    """Reset a drifted detached checkout, returning a failure reason."""
    if not item.worktree:
        return "detached_push_recovery_worktree_missing"
    generation = item.payload.get("direct_pr_worktree_generation", 0)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return "direct_pr_worktree_generation_invalid"
    item.worktree = ""
    item.payload["existing_pr"] = True
    item.payload.pop("direct_pr_worktree", None)
    item.payload.pop("direct_pr_worktree_dirty", None)
    item.payload["direct_pr_worktree_generation"] = generation + 1
    item.session_ids.pop(AGENT_PR_REVIEWER, None)
    item.session_ids.pop(AGENT_ADDRESS_REVIEW, None)
    item.session_bindings.pop(AGENT_PR_REVIEWER, None)
    item.session_bindings.pop(AGENT_ADDRESS_REVIEW, None)
    _clear_round_review_state(item)
    return None


def consume_reply_handoff_receipt(item: WorkItem, pending_request_key: str) -> str:
    """Apply a correlated immutable recovery receipt to local payload."""
    from ..github_jobs import ReplyHandoffAttempted

    if not item.payload.get(_PENDING_IMPLEMENTATION_REPLY_HANDOFF):
        item.payload.pop(pending_request_key, None)
        item.payload.pop(_REPLY_HANDOFF_RECEIPT, None)
        item.payload.pop(_REPLY_HANDOFF_RECEIPT_ERROR, None)
        return "completed"
    error = item.payload.pop(_REPLY_HANDOFF_RECEIPT_ERROR, None)
    if error is not None:
        item.payload.pop(pending_request_key, None)
        return "retry" if error == "retry" else "invalid"
    receipt = item.payload.pop(_REPLY_HANDOFF_RECEIPT, None)
    if not isinstance(receipt, ReplyHandoffAttempted) or receipt.request != item.payload.get(
        pending_request_key
    ):
        return "invalid"
    item.payload.pop(pending_request_key, None)
    remaining = receipt.remaining_handoff.thaw() if receipt.remaining_handoff is not None else None
    if remaining is not None and not isinstance(remaining, dict):
        return "invalid"
    if remaining is None:
        item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
    else:
        item.payload[_PENDING_IMPLEMENTATION_REPLY_HANDOFF] = remaining
    item.payload[_REPLY_VISIBILITY_RETRIES] = receipt.visibility_retries
    if receipt.retry_delay_s is None:
        item.payload.pop("retry_delay_s", None)
    else:
        item.payload["retry_delay_s"] = receipt.retry_delay_s
    if receipt.status in {"completed", "stale", "blocked"}:
        item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
    if receipt.status == "blocked":
        item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
    return receipt.status


__all__ = [
    "consume_reply_handoff_receipt",
    "empty_diff_outcome",
    "recovery_reply_job",
    "restart_direct_pr_review",
]
