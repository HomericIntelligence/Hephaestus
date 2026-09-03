"""Scope-expansion handoff helpers for the PR-review stage."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from hephaestus.automation.review_audit import ReviewAudit

from ..github_jobs import (
    EnsureScopeExpansionChildrenRequest,
    FrozenJson,
    GitHubJob,
    ReconcileScopeExpansionDependenciesRequest,
    ScopeExpansionChildrenEnsured,
    ScopeExpansionDependenciesReconciled,
)
from .base import (
    Continue,
    Disposition,
    ItemKind,
    JobRequest,
    JobResult,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
)
from .pr_review_threads import (
    ENTER,
    EVAL,
    POST,
    REVIEW_WAIT,
    SCOPE_DEPENDENCY_WAIT,
    SCOPE_EXPANSION_PREPARE_SUBMIT,
    _issue_number,
    _scope_retraction_paths,
)
from .repo import is_full_commit_sha

logger = logging.getLogger(__name__)
_SCOPE_EXPANSION_PENDING_REQUEST = "_scope_expansion_pending_request"
_SCOPE_EXPANSION_RECEIPT = "_scope_expansion_receipt"
_SCOPE_EXPANSION_RECEIPT_ERROR = "_scope_expansion_receipt_error"
_SCOPE_EXPANSION_PREPARED_RECEIPT = "_scope_expansion_prepared_receipt"
_SCOPE_DEPENDENCY_PENDING_REQUEST = "_scope_dependency_pending_request"
_SCOPE_DEPENDENCY_RECEIPT = "_scope_dependency_receipt"
_SCOPE_DEPENDENCY_RECEIPT_ERROR = "_scope_dependency_receipt_error"


class PrReviewScopeExpansionMixin:
    """Own the reviewer-discovered scope-expansion child issue lifecycle."""

    @staticmethod
    def _scope_retraction_failure(item: WorkItem) -> StageOutcome | None:
        """Stop publication after an incomplete mixed-scope retraction."""
        if not item.payload.pop("scope_retraction_failure", False):
            return None
        logger.warning(
            "pr_review:%d: refusing to publish incomplete scope retraction",
            _issue_number(item),
        )
        return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_incomplete")

    def _enter(self: Any, item: WorkItem, ctx: StageContext) -> StepResult:
        """Reconcile durable child dependencies before checkout or review."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        state = ctx.github.gh_pr_state(item.pr)
        if not isinstance(state, dict):
            return StageOutcome(Disposition.BLOCKED, "scope_dependency_pr_state_unavailable")
        head_sha = state.get("headRefOid")
        if (
            state.get("state") != "OPEN"
            or "autoMergeRequest" not in state
            or state.get("autoMergeRequest") is not None
            or not is_full_commit_sha(head_sha)
        ):
            return StageOutcome(Disposition.BLOCKED, "scope_dependency_pr_state_invalid")
        request = ReconcileScopeExpansionDependenciesRequest(
            issue_number=_issue_number(item),
            pr_number=item.pr,
            source_head_sha=head_sha,
        )
        item.payload[_SCOPE_DEPENDENCY_PENDING_REQUEST] = request
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)).resolve(),
                request=request,
                descr="reconcile_scope_expansion_dependencies",
            ),
            on_done_state=SCOPE_DEPENDENCY_WAIT,
        )

    def _scope_dependency_wait(self: Any, item: WorkItem, ctx: StageContext) -> StepResult:
        """Route one closed dependency receipt before broad review work."""
        outcome = self._route_scope_dependency_receipt(item)
        if outcome is not None:
            if (
                isinstance(outcome, StageOutcome)
                and outcome.note == "scope_dependency_sync_required"
                and item.worktree
            ):
                item.payload["implementation_writer_restored"] = True
            if (
                isinstance(outcome, Continue)
                and item.worktree
                and not item.payload.get("direct_pr_worktree")
            ):
                item.payload["writer_worktree"] = item.worktree
                item.payload["reviewer_checkout_needed"] = True
                item.worktree = ""
                item.payload["scope_dependency_entry_reconciled"] = True
                return Continue(next_state=ENTER)
            elif isinstance(outcome, Continue) and not item.worktree:
                item.payload["scope_dependency_entry_reconciled"] = True
                return Continue(next_state=ENTER)
            return cast(StepResult, outcome)
        if item.worktree and not item.payload.get("direct_pr_worktree"):
            item.payload["writer_worktree"] = item.worktree
            item.payload["reviewer_checkout_needed"] = True
            item.worktree = ""
        thread_outcome = self._route_existing_threads_before_audit(item, ctx)
        if thread_outcome is not None:
            return cast(StepResult, thread_outcome)
        if not item.worktree and (
            item.kind is ItemKind.PR
            or item.payload.get("existing_pr")
            or item.payload.get("reviewer_checkout_needed")
        ):
            item.payload["scope_dependency_entry_reconciled"] = True
            return Continue(next_state=ENTER)
        return Continue(next_state=REVIEW_WAIT)

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
    def _consume_scope_dependency_result(item: WorkItem, result: JobResult) -> bool:
        """Store one correlated dependency reconciliation receipt."""
        request = item.payload.get(_SCOPE_DEPENDENCY_PENDING_REQUEST)
        if request is None:
            return False
        if not result.ok:
            item.payload[_SCOPE_DEPENDENCY_RECEIPT_ERROR] = (
                result.error or "scope dependency job failed"
            )
            return True
        receipt = result.value
        if (
            not isinstance(receipt, ScopeExpansionDependenciesReconciled)
            or receipt.request != request
        ):
            item.payload[_SCOPE_DEPENDENCY_RECEIPT_ERROR] = "invalid"
            return True
        item.payload[_SCOPE_DEPENDENCY_RECEIPT] = receipt
        return True

    @staticmethod
    def _route_scope_dependency_receipt(item: WorkItem) -> StepResult | None:
        """Route a verified dependency state without spending review budgets."""
        error = item.payload.pop(_SCOPE_DEPENDENCY_RECEIPT_ERROR, None)
        if error is not None:
            item.payload.pop(_SCOPE_DEPENDENCY_PENDING_REQUEST, None)
            item.payload.pop(_SCOPE_DEPENDENCY_RECEIPT, None)
            return StageOutcome(Disposition.BLOCKED, "scope_dependency_job_failed")
        receipt = item.payload.pop(_SCOPE_DEPENDENCY_RECEIPT, None)
        request = item.payload.pop(_SCOPE_DEPENDENCY_PENDING_REQUEST, None)
        if not isinstance(receipt, ScopeExpansionDependenciesReconciled):
            return StageOutcome(Disposition.BLOCKED, "scope_dependency_receipt_missing")
        if receipt.request != request:
            return StageOutcome(Disposition.BLOCKED, "scope_dependency_receipt_invalid")
        item.payload["scope_expansion_child_issue_numbers"] = list(receipt.child_issue_numbers)
        if receipt.status == "none":
            return None
        if receipt.status == "retraction_required":
            threads = receipt.retraction_threads.thaw()
            snapshots = receipt.retraction_snapshots.thaw()
            if (
                not isinstance(threads, list)
                or not threads
                or not isinstance(snapshots, list)
                or len(threads) != len(snapshots)
                or not all(isinstance(thread, dict) for thread in threads)
                or any(not _scope_retraction_paths([thread]) for thread in threads)
            ):
                return StageOutcome(Disposition.BLOCKED, "scope_expansion_operator_required")
            item.payload["implementation_remediation"] = True
            item.payload["scope_retraction_before_scope_block"] = True
            item.payload["remediation_threads"] = [dict(thread) for thread in threads]
            item.payload["remediation_thread_snapshots"] = [
                dict(snapshot) for snapshot in snapshots if isinstance(snapshot, dict)
            ]
            item.payload["unresolved_threads"] = [dict(thread) for thread in threads]
            item.payload["unresolved_threads_before_address"] = len(threads)
            return StageOutcome(Disposition.FAIL_BACK, "scope_retraction_before_scope_block")
        if receipt.status == "parked":
            return StageOutcome(Disposition.BLOCKED, "scope_expansion_blocked")
        if receipt.status == "operator_required":
            return StageOutcome(Disposition.BLOCKED, "scope_expansion_operator_required")
        if receipt.status == "sync_required":
            item.payload["post_review_rebase_required"] = True
            item.payload["scope_dependency_sync_required"] = True
            item.payload["scope_dependency_merge_shas"] = list(receipt.merge_shas)
            return StageOutcome(Disposition.FAIL_BACK, "scope_dependency_sync_required")
        if receipt.status == "fresh_review":
            item.payload.pop("reviewed_pr_head_sha", None)
            item.payload["scope_dependency_force_fresh_review"] = True
            return Continue(next_state=REVIEW_WAIT)
        return StageOutcome(Disposition.BLOCKED, "scope_dependency_receipt_invalid")

    @staticmethod
    def _route_scope_expansion_receipt(
        item: WorkItem, receipt: ScopeExpansionChildrenEnsured
    ) -> StepResult | None:
        """Route one child-creation result and preserve retraction-only work."""
        if receipt.status == "dry_run":
            return StageOutcome(Disposition.BLOCKED, "scope_expansion_dry_run")
        if receipt.status in {"blocked", "operator_required"}:
            raw_threads = item.payload.get("remediation_threads")
            threads = raw_threads if isinstance(raw_threads, list) else []
            retractions = [
                thread
                for thread in threads
                if isinstance(thread, dict) and _scope_retraction_paths([thread])
            ]
            if retractions:
                retraction_ids = {
                    str(thread.get("id") or thread.get("thread_id") or "") for thread in retractions
                }
                raw_snapshots = item.payload.get("remediation_thread_snapshots")
                snapshots = raw_snapshots if isinstance(raw_snapshots, list) else []
                retraction_snapshots = [
                    dict(snapshot)
                    for snapshot in snapshots
                    if isinstance(snapshot, dict)
                    and str(snapshot.get("id") or snapshot.get("thread_id") or "") in retraction_ids
                ]
                item.payload["implementation_remediation"] = True
                item.payload["scope_retraction_before_scope_block"] = True
                item.payload["remediation_threads"] = [dict(thread) for thread in retractions]
                item.payload["remediation_thread_snapshots"] = retraction_snapshots
                item.payload["unresolved_threads"] = [dict(thread) for thread in retractions]
                item.payload["unresolved_threads_before_address"] = len(retractions)
                return StageOutcome(
                    Disposition.FAIL_BACK,
                    "scope_retraction_before_scope_block",
                )
            return StageOutcome(Disposition.BLOCKED, f"scope_expansion_{receipt.status}")
        return None

    @staticmethod
    def _scope_expansion_request(
        item: WorkItem, audit: ReviewAudit
    ) -> EnsureScopeExpansionChildrenRequest:
        """Build the exact durable child request for the current review audit."""
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        if not is_full_commit_sha(reviewed_head):
            raise ValueError("reviewed pull-request head is invalid")
        retractions = [
            dict(finding) for finding in audit.findings if _scope_retraction_paths([finding])
        ]
        return EnsureScopeExpansionChildrenRequest(
            issue_number=_issue_number(item),
            pr_number=cast(int, item.pr),
            reviewed_head_sha=reviewed_head,
            scope_expansions=tuple(audit.scope_expansions),
            retraction_findings=FrozenJson.snapshot(retractions),
            review_diff=str(item.payload.get("pr_diff") or "") if retractions else "",
        )

    def _scope_expansion_prepare_submit(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit the durable scope transaction before review-thread publication."""
        audit = item.payload.get("review_audit")
        if not isinstance(audit, ReviewAudit) or not audit.valid or not audit.scope_expansions:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        result = self._handle_scope_expansions(item, ctx, audit, prepare_only=True)
        if result is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        return result

    def _prepare_scope_expansion_before_post(
        self, item: WorkItem, ctx: StageContext, audit: ReviewAudit
    ) -> StepResult | None:
        """Prepare and verify the durable scope receipt before thread publication."""
        if not audit.scope_expansions:
            return None
        if _SCOPE_EXPANSION_PREPARED_RECEIPT not in item.payload:
            if _SCOPE_EXPANSION_RECEIPT not in item.payload:
                return Continue(next_state=SCOPE_EXPANSION_PREPARE_SUBMIT)
            preparation = self._handle_scope_expansions(item, ctx, audit, prepare_only=True)
            if preparation is not None:
                return preparation
        prepared = item.payload.get(_SCOPE_EXPANSION_PREPARED_RECEIPT)
        try:
            expected = self._scope_expansion_request(item, audit)
        except ValueError:
            expected = None
        if not isinstance(prepared, ScopeExpansionChildrenEnsured) or prepared.request != expected:
            item.payload.pop(_SCOPE_EXPANSION_PREPARED_RECEIPT, None)
            return StageOutcome(
                Disposition.BLOCKED,
                "scope_expansion_projection_operator_required",
            )
        return None

    @staticmethod
    def _handle_scope_expansions(  # noqa: C901
        item: WorkItem,
        ctx: StageContext,
        audit: ReviewAudit,
        *,
        prepare_only: bool = False,
    ) -> StepResult | None:
        """Ensure deterministic child issues for reviewer scope expansions."""
        expansions = audit.scope_expansions
        if not expansions:
            item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
            item.payload.pop(_SCOPE_EXPANSION_RECEIPT_ERROR, None)
            item.payload.pop(_SCOPE_EXPANSION_RECEIPT, None)
            item.payload.pop(_SCOPE_EXPANSION_PREPARED_RECEIPT, None)
            return None
        error = item.payload.pop(_SCOPE_EXPANSION_RECEIPT_ERROR, None)
        if error is not None:
            item.payload.pop(_SCOPE_EXPANSION_RECEIPT, None)
            item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
            logger.warning("pr_review:%s: scope-expansion job failed: %s", item.issue, error)
            return StageOutcome(Disposition.BLOCKED, "scope_expansion_job_failed")

        from_prepared_receipt = False
        receipt = item.payload.get(_SCOPE_EXPANSION_RECEIPT)
        if receipt is None and not prepare_only:
            receipt = item.payload.pop(_SCOPE_EXPANSION_PREPARED_RECEIPT, None)
            from_prepared_receipt = receipt is not None
        if isinstance(receipt, ScopeExpansionChildrenEnsured):
            pending = item.payload.get(_SCOPE_EXPANSION_PENDING_REQUEST)
            try:
                expected = PrReviewScopeExpansionMixin._scope_expansion_request(item, audit)
            except ValueError:
                expected = None
            if receipt.request != expected or (
                not from_prepared_receipt and receipt.request != pending
            ):
                item.payload.pop(_SCOPE_EXPANSION_RECEIPT, None)
                item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
            item.payload.pop(_SCOPE_EXPANSION_RECEIPT, None)
            if prepare_only:
                item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
                item.payload[_SCOPE_EXPANSION_PREPARED_RECEIPT] = receipt
                return None
            if not from_prepared_receipt:
                item.payload.pop(_SCOPE_EXPANSION_PENDING_REQUEST, None)
            outcome = PrReviewScopeExpansionMixin._route_scope_expansion_receipt(item, receipt)
            if outcome is not None:
                return outcome
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)

        try:
            request = PrReviewScopeExpansionMixin._scope_expansion_request(item, audit)
        except ValueError:
            return StageOutcome(
                Disposition.BLOCKED,
                "scope_expansion_projection_operator_required",
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
            on_done_state=POST if prepare_only else EVAL,
        )
