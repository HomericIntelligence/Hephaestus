"""Route live review threads to validation or implementation."""

from __future__ import annotations

from typing import Any, Protocol

from .base import (
    Continue,
    Disposition,
    JobRequest,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
)
from .pr_review_recovery import empty_diff_outcome
from .pr_review_threads import (
    _COMMENT_VALIDATION_ONLY,
    ADDRESS_WAIT,
    VALIDATE_WAIT,
    _normalize_remediation_threads,
    _scope_retraction_paths,
    _validation_receipt_fingerprints,
    _validation_thread_snapshots,
)


class _ThreadRoutingHost(Protocol):
    """Provide the review-stage actions used by the routing mixin."""

    def _cleanup_review_worktree_then(self, item: WorkItem, outcome: StageOutcome) -> StepResult:
        raise NotImplementedError

    def _submit_review_job(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        raise NotImplementedError


class PrReviewThreadRoutingMixin:
    """Route a stable thread snapshot to review, validation, or writing."""

    def _route_review_threads(
        self: _ThreadRoutingHost,
        item: WorkItem,
        ctx: StageContext,
        live_threads: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
    ) -> StepResult:
        """Route a stable thread snapshot to review, validation, or writing."""
        if item.payload.pop("scope_dependency_force_fresh_review", False):
            empty_diff = empty_diff_outcome(item)
            if empty_diff:
                return self._cleanup_review_worktree_then(item, empty_diff)
            item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
            return self._submit_review_job(item, ctx)
        if not live_threads:
            if item.payload.get(_COMMENT_VALIDATION_ONLY):
                # A reply may resolve the last thread while host checks run.
                # Keep the selected validation route instead of opening a new audit.
                return Continue(next_state=VALIDATE_WAIT)
            empty_diff = empty_diff_outcome(item)
            if empty_diff:
                return self._cleanup_review_worktree_then(item, empty_diff)
            return self._submit_review_job(item, ctx)
        snapshots = _validation_thread_snapshots(live_threads, receipts)
        remediation_threads = _normalize_remediation_threads(live_threads)
        if (
            snapshots is None
            or _validation_receipt_fingerprints(receipts) is None
            or len(remediation_threads) != len(live_threads)
            or _scope_retraction_paths(remediation_threads) is None
        ):
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_thread_receipts_invalid"),
            )
        if all(bool(snapshot.get("implementation_reply_submitted")) for snapshot in snapshots):
            item.payload[_COMMENT_VALIDATION_ONLY] = True
            return Continue(next_state=VALIDATE_WAIT)
        item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
        item.payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        item.payload["remediation_threads"] = remediation_threads
        item.payload["remediation_thread_snapshots"] = [dict(thread) for thread in live_threads]
        item.payload["unresolved_threads_before_address"] = len(remediation_threads)
        return Continue(next_state=ADDRESS_WAIT)


__all__ = ["PrReviewThreadRoutingMixin"]
