# This mixin consumes the stage thread namespace by design.
# ruff: noqa: F403, F405
import json
from pathlib import Path

from hephaestus.agents.execution_policy import AgentOperation, AgentRole, ExecutionRequest
from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.automation.prompts.pr_review import build_bounded_review_validation_prompt

from ..coordinator_sessions import agent_session_lifecycle
from ..github_jobs import FrozenJson, GitHubJob, PrReviewReconciled, ReconcilePrReviewRequest
from .base import source_workspace_binding
from .pr_review_threads import *

_PENDING_GITHUB_REQUEST = "_pending_github_request"
_PR_REVIEW_RECEIPT = "_pr_review_reconciliation_receipt"
_PR_REVIEW_RECEIPT_ERROR = "_pr_review_reconciliation_error"


def _review_summary_line(audit: ReviewAudit) -> str:
    """Return the informational verdict, grade and summary for the review body."""
    parts = [f"Verdict: {audit.verdict}"] if audit.verdict else []
    if audit.grade:
        parts.append(f"Grade: {audit.grade}")
    head = " · ".join(parts)
    text = f"{head} — {audit.summary}" if head else audit.summary
    return f"**Automated review.** {text} (informational)"


class PrReviewPost(_PrReviewHost):
    """Validate answered threads, post the review, and read GitHub back."""

    def _validate_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Ask the reviewer whether the implementation agent's answers settle the open threads."""
        issue = _issue_number(item)
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            pr_context = ctx.github.pr_review_context(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not read the threads to validate (%s)",
                item.issue,
                type(error).__name__,
            )
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        if not live_threads or pr_context is None:
            return Continue(next_state=POST)
        item.payload["prior_comments_json"] = json.dumps(
            _thread_snapshots(live_threads), ensure_ascii=False, sort_keys=True
        )
        workspace = source_workspace_binding(
            item,
            ctx,
            SourceLane.REVIEW,
            revision=str(item.payload.get("reviewed_pr_head_sha") or ""),
        )
        logger.info("pr_review:%d: requesting validation job", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx, "reviewer"),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=build_bounded_review_validation_prompt,
            cwd=workspace.cwd,
            timeout_s=stage_timeout(ctx, "reviewer", pr_reviewer_claude_timeout),
            workspace=workspace,
            session_agent=AGENT_PR_REVIEWER,
            resume_session_id=item.session_ids.get(AGENT_PR_REVIEWER),
            execution_request=ExecutionRequest(
                AgentRole.PR_REVIEWER,
                AgentOperation.REVIEW_VALIDATE,
                agent_session_lifecycle(item, AGENT_PR_REVIEWER),
            ),
            resume_binding=item.session_bindings.get(AGENT_PR_REVIEWER),
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": item.issue,
                "prior_comments_json": item.payload["prior_comments_json"],
                "diff_text": item.payload.get("pr_diff", ""),
                "pr_title": pr_context.get("pr_title", ""),
                "pr_description": pr_context.get("pr_description", ""),
                "repository_validation_json": "",
                "host_verifications_json": "[]",
                "review_context_kind": _review_context_kind(item),
            },
            descr="validate",
        )
        return JobRequest(job, on_done_state=POST)

    def _post(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Post the review's comments and apply the reviewer's decisions on the open threads."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        validation_only = bool(item.payload.get(_COMMENT_VALIDATION_ONLY))
        audit = item.payload.get("review_audit")
        if not validation_only and (
            item.payload.pop("review_failed", None)
            or not isinstance(audit, ReviewAudit)
            or not audit.valid
        ):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        findings = (
            []
            if validation_only or not isinstance(audit, ReviewAudit)
            else [dict(finding) for finding in audit.findings]
        )
        summary = (
            _review_summary_line(audit)
            if not validation_only and isinstance(audit, ReviewAudit)
            else ""
        )
        resolved_ids, feedback = _validation_decisions(item.payload.get("validation_result"))
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        deadline_s = (
            pending.deadline_s
            if isinstance(pending, ReconcilePrReviewRequest)
            else operation_deadline_after(stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S))
        )
        request = ReconcilePrReviewRequest(
            issue_number=item.issue,
            pr_number=item.pr,
            findings=FrozenJson.snapshot(findings),
            review_diff=str(item.payload.get("pr_diff") or ""),
            deadline_s=deadline_s,
            resolved_thread_ids=resolved_ids,
            feedback=FrozenJson.snapshot(feedback),
            summary=summary,
        )
        item.payload[_PENDING_GITHUB_REQUEST] = request
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)).resolve(),
                request=request,
                descr="reconcile_pr_review",
            ),
            on_done_state=POST_APPLY,
        )

    @staticmethod
    def _on_reconciliation_done(item: WorkItem, result: JobResult) -> None:
        """Store the reconciliation receipt, or count the failure."""
        if not result.ok:
            retries = item.payload.get("pr_review_reconciliation_retries", 0) + 1
            item.payload["pr_review_reconciliation_retries"] = retries
            logger.warning("pr_review:%s: posting the review failed: %s", item.issue, result.error)
            item.payload[_PR_REVIEW_RECEIPT_ERROR] = (
                "retry" if retries <= REVIEW_ERROR_RETRY_CAP else "failed"
            )
            return
        if isinstance(result.value, PrReviewReconciled):
            item.payload[_PR_REVIEW_RECEIPT] = result.value
        else:
            item.payload[_PR_REVIEW_RECEIPT_ERROR] = "failed"

    def _post_apply(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Route on what GitHub lists after the review was posted."""
        error = item.payload.pop(_PR_REVIEW_RECEIPT_ERROR, None)
        item.payload.pop(_PENDING_GITHUB_REQUEST, None)
        if error == "retry":
            item.state = POST
            return StageOutcome(Disposition.RETRY, "pr_review_reconciliation_retry")
        receipt = item.payload.pop(_PR_REVIEW_RECEIPT, None)
        if error or not isinstance(receipt, PrReviewReconciled):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload.pop("pr_review_reconciliation_retries", None)
        threads = receipt.unresolved_threads.thaw()
        unresolved = (
            [dict(thread) for thread in threads if isinstance(thread, dict)]
            if isinstance(threads, list)
            else []
        )
        body_only = receipt.body_only_findings.thaw()
        item.payload["unresolved_threads"] = unresolved
        item.payload["review_body_only_findings"] = body_only if isinstance(body_only, list) else []
        if item.payload.pop(_COMMENT_VALIDATION_ONLY, None):
            # Validating answers settles threads. It does not approve the pull request.
            item.payload.pop("review_audit", None)
            if unresolved:
                remediation = _normalize_remediation_threads(unresolved)
                item.payload["remediation_threads"] = remediation
                item.payload["remediation_thread_snapshots"] = [dict(t) for t in unresolved]
                item.payload["unresolved_threads_before_address"] = len(remediation)
                return self._handoff_implementation(item, ctx)
            return Continue(next_state=REVIEW_WAIT)
        if unresolved:
            remediation = _normalize_remediation_threads(unresolved)
            item.payload["remediation_threads"] = remediation
            item.payload["remediation_thread_snapshots"] = [dict(t) for t in unresolved]
            item.payload["unresolved_threads_before_address"] = len(remediation)
        return Continue(next_state=EVAL)
