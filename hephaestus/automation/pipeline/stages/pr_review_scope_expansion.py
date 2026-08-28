"""Scope-expansion handoff helpers for the PR-review stage."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

from ..github_jobs import (
    EnsureScopeExpansionChildrenRequest,
    GitHubJob,
    ScopeExpansionChildrenEnsured,
)
from hephaestus.automation.review_audit import ReviewAudit

from .base import (
    Continue,
    Disposition,
    JobRequest,
    JobResult,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
)
from .repo import is_full_commit_sha
from .pr_review_threads import EVAL, _issue_number

logger = logging.getLogger(__name__)
_SCOPE_EXPANSION_PENDING_REQUEST = "_scope_expansion_pending_request"
_SCOPE_EXPANSION_RECEIPT = "_scope_expansion_receipt"
_SCOPE_EXPANSION_RECEIPT_ERROR = "_scope_expansion_receipt_error"


class PrReviewScopeExpansionMixin:
    """Own the reviewer-discovered scope-expansion child issue lifecycle."""

    @staticmethod
    def _consume_scope_expansion_result(item: WorkItem, result: JobResult) -> bool:
        """Store a closed scope-expansion receipt or preserve the failure."""
        if item.payload.get(_SCOPE_EXPANSION_PENDING_REQUEST) is None:
            return False
        if not result.ok:
            item.payload[_SCOPE_EXPANSION_RECEIPT_ERROR] = (
                result.error or "scope expansion job failed"
            )
            return True
        receipt = result.value
        if not isinstance(
            receipt, ScopeExpansionChildrenEnsured
        ) or receipt.request != item.payload.get(_SCOPE_EXPANSION_PENDING_REQUEST):
            item.payload[_SCOPE_EXPANSION_RECEIPT_ERROR] = "invalid"
            return True
        item.payload[_SCOPE_EXPANSION_RECEIPT] = receipt
        return True

    @staticmethod
    def _handle_scope_expansions(
        item: WorkItem, ctx: StageContext, audit: ReviewAudit
    ) -> StepResult | None:
        """Ensure deterministic child issues for reviewer scope expansions."""
        expansions = audit.scope_expansions
        if not expansions:
            item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
            item.payload.pop(_SCOPE_EXPANSION_RECEIPT_ERROR, None)
            item.payload.pop(_SCOPE_EXPANSION_RECEIPT, None)
            return None
        error = item.payload.pop(_SCOPE_EXPANSION_RECEIPT_ERROR, None)
        if error is not None:
            item.payload.pop(_SCOPE_EXPANSION_RECEIPT, None)
            item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
            logger.warning("pr_review:%s: scope-expansion job failed: %s", item.issue, error)
            return StageOutcome(Disposition.BLOCKED, "scope_expansion_job_failed")

        receipt = item.payload.get(_SCOPE_EXPANSION_RECEIPT)
        if isinstance(receipt, ScopeExpansionChildrenEnsured):
            pending = item.payload.get(_SCOPE_EXPANSION_PENDING_REQUEST)
            if receipt.request != pending:
                item.payload.pop(_SCOPE_EXPANSION_RECEIPT, None)
                item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
            item.payload.pop(_SCOPE_EXPANSION_RECEIPT, None)
            item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
            if receipt.status == "resolved":
                return None
            if receipt.status == "dry_run":
                return StageOutcome(Disposition.BLOCKED, "scope_expansion_dry_run")
            if receipt.status in {"blocked", "operator_required"}:
                return StageOutcome(Disposition.BLOCKED, f"scope_expansion_{receipt.status}")
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)

        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        if not is_full_commit_sha(reviewed_head):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        request = EnsureScopeExpansionChildrenRequest(
            issue_number=_issue_number(item),
            pr_number=cast(int, item.pr),
            reviewed_head_sha=reviewed_head,
            scope_expansions=tuple(expansions),
        )
        pending = item.payload.get(_SCOPE_EXPANSION_PENDING_REQUEST)
        if pending is None:
            item.payload[_SCOPE_EXPANSION_PENDING_REQUEST] = request
        elif pending != request:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)).resolve(),
                request=request,
                descr="ensure_scope_expansion_children",
            ),
            on_done_state=EVAL,
        )
