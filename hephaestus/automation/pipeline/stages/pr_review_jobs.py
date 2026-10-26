# This mixin consumes the stage thread namespace by design.
# ruff: noqa: F403, F405

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.prompts.pr_review import build_bounded_pr_review_analysis_prompt
from hephaestus.automation.source_worktree import SourceWorkspaceError

from ..coordinator_sessions import agent_session_lifecycle
from ..diagnostics import redact_diagnostic_text
from ..summary import record_review_run
from .base import _terminal_pr_outcome, source_workspace_binding
from .pr_review_threads import *


def empty_diff_outcome(item: WorkItem) -> StageOutcome | None:
    """Fail a review back to implementation when the pull request diff is empty."""
    if str(item.payload.get("pr_diff") or "").strip():
        return None
    _clear_round_review_state(item)
    item.payload["empty_diff_reimplementation"] = True
    logger.warning(
        "pr_review:%d: empty cumulative diff; failing back to implementation",
        _issue_number(item),
    )
    return StageOutcome(Disposition.FAIL_BACK, "empty_pr_diff")


class PrReviewJobs(_PrReviewHost):
    """Own the review checkout, the review job, and the hand-off to implementation."""

    @staticmethod
    def _fail_back_implementation_remediation(item: WorkItem) -> StageOutcome:
        """Route unresolved review work back to implementation."""
        item.payload["implementation_remediation"] = True
        return StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Reset the round state when a new implementation pass starts."""
        for key in ("host_verification_workspace",):
            item.payload.pop(key, None)
        if item.pr is not None:
            item.payload.pop("reviewed_pr_head_sha", None)
            item.payload.pop("reviewed_pr_node_id", None)
        cycle = item.attempts.get("implement", 0)
        if item.payload.get("pr_review_cycle") != cycle:
            item.payload["pr_review_cycle"] = cycle
            item.payload["pr_review_round"] = 0
            item.payload.pop("review_error_retries", None)
        return None

    def _enter(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Send unanswered threads to implementation, then check out the PR for review."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        _clear_round_review_state(item)
        state = ctx.github.gh_pr_state(item.pr)
        if state is not None and (terminal := _terminal_pr_outcome(state, item.pr)):
            return terminal
        if item.payload.get("explicit_pr_review"):
            item.payload["existing_pr"] = True
        else:
            routed = self._route_existing_threads(item, ctx, after_checkout=False)
            if routed is not None:
                return routed
        if item.worktree and not item.payload.get("direct_pr_worktree"):
            # The reviewer gets its own detached checkout. The writer's checkout
            # stays recorded so implementation can resume on it.
            item.payload["writer_worktree"] = item.worktree
            item.payload["reviewer_checkout_needed"] = True
            item.worktree = ""
        if not item.worktree and (
            item.kind is ItemKind.PR
            or item.payload.get("existing_pr")
            or item.payload.get("reviewer_checkout_needed")
        ):
            return self._adopt_direct_pr_worktree(item, ctx)
        return Continue(next_state=REVIEW_WAIT)

    def _route_existing_threads(
        self, item: WorkItem, ctx: StageContext, *, after_checkout: bool
    ) -> StepResult | None:
        """Route the pull request's open threads before a new review.

        An open thread that the implementation agent has not answered goes back
        to implementation. When every open thread is answered, the reviewer
        validates the answers instead of reviewing the whole pull request again.
        Returns None when the review should go on.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
        except Exception as error:
            retries = item.payload.get(_THREAD_READ_RETRIES, 0) + 1
            item.payload[_THREAD_READ_RETRIES] = retries
            logger.warning(
                "pr_review:%s: could not read the review threads (%s); attempt %d",
                item.issue,
                type(error).__name__,
                retries,
            )
            outcome = StageOutcome(
                Disposition.FINISH_FAIL if retries > REVIEW_ERROR_RETRY_CAP else Disposition.RETRY,
                "review_threads_unavailable",
            )
            return self._cleanup_review_worktree_then(item, outcome)
        item.payload.pop(_THREAD_READ_RETRIES, None)
        if not live_threads:
            item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
            return None
        if item.payload.get("explicit_pr_review"):
            return None
        remediation = _normalize_remediation_threads(live_threads)
        retraction = _scope_retraction_paths(remediation)
        if retraction and not after_checkout:
            # A scope retraction needs the base of the review checkout.
            return None
        item.payload["existing_pr"] = True
        if all(
            snapshot["implementation_reply_submitted"]
            for snapshot in _thread_snapshots(live_threads)
        ):
            item.payload[_COMMENT_VALIDATION_ONLY] = True
            return None
        item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
        item.payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        item.payload["remediation_threads"] = remediation
        item.payload["remediation_thread_snapshots"] = [dict(thread) for thread in live_threads]
        item.payload["unresolved_threads_before_address"] = len(remediation)
        if not after_checkout:
            state = ctx.github.gh_pr_state(item.pr)
            head = str(state.get("headRefOid") or "") if isinstance(state, dict) else ""
            if head:
                item.payload["reviewed_pr_head_sha"] = head
        return self._handoff_implementation(item, ctx)

    def _adopt_direct_pr_worktree(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Create an isolated checkout of the pull request for the review."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        branch = ctx.github.get_pr_head_branch(item.pr) or item.branch
        if not branch:
            logger.error("pr_review:%s: no head branch for direct PR #%d", item.issue, item.pr)
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_no_head_branch")
        item.branch = branch
        item.payload["existing_pr"] = True
        generation = item.payload.get("direct_pr_worktree_generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            generation = 0
        logger.info(
            "pr_review:%d: adopting direct PR #%d (branch %r) for review",
            _issue_number(item),
            item.pr,
            branch,
        )
        kwargs: dict[str, object] = {
            "issue_number": _issue_number(item),
            "branch_name": branch,
            "refresh_base": False,
            "isolated": True,
            "source_lane": "review",
            "sync_to_remote": False,
            "pr_number": item.pr,
            "repo_root": str(ctx.paths.repo_root),
        }
        if generation:
            kwargs["isolated_generation"] = generation
        job = GitJob(
            repo=item.repo,
            op="create_worktree",
            timeout_s=stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S),
            expected_repository=f"{ctx.org}/{item.repo}",
            kwargs=kwargs,
            descr="direct_pr_review_worktree",
        )
        item.payload["direct_pr_worktree_pending"] = True
        return JobRequest(job, on_done_state=ADOPT_WORKTREE_WAIT)

    def _adopt_worktree_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Continue to the review once the detached checkout exists."""
        del ctx
        if item.payload.pop("direct_pr_worktree_error", None):
            self._restore_writer_worktree(item)
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_failed")
        if not item.worktree:
            self._restore_writer_worktree(item)
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_unfinished")
        if item.payload.get("direct_pr_worktree_dirty"):
            self._restore_writer_worktree(item)
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_dirty")
        return Continue(next_state=REVIEW_WAIT)

    def _review_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Read the pull request as it is now and sync the checkout to it."""
        _clear_round_review_state(item)
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        review_context = ctx.github.pr_review_context(item.pr)
        if review_context is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_review_context_unavailable")
        expected_head = str(review_context.get("pr_head_sha") or "")
        expected_base = str(review_context.get("pr_base_sha") or "")
        if not expected_head:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_review_head_unavailable")
        base_branch = str(review_context.get("pr_base_branch") or "main")
        item.payload.update(review_context)
        item.payload["review_checkout_expected_head"] = expected_head
        item.payload["review_worktree_expected_head"] = expected_head
        item.payload["review_checkout_pending"] = True
        job = GitJob(
            repo=item.repo,
            op="verify_pr_review_checkout",
            timeout_s=stage_timeout(ctx, "diff_collect", GIT_JOB_TIMEOUT_S),
            expected_repository=f"{ctx.org}/{item.repo}",
            kwargs={
                "worktree_path": str(_worktree_path(item, ctx)),
                "branch": item.branch,
                "expected_head_sha": expected_head,
                "expected_base_sha": expected_base,
                "base_branch": base_branch,
                "pr_number": item.pr,
            },
            descr="verify_pr_review_checkout",
        )
        return JobRequest(job, on_done_state=REVIEW_CHECKOUT_WAIT)

    def _review_checkout_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Start the review once the checkout holds the pull request."""
        expected_head = str(item.payload.pop("review_checkout_expected_head", "") or "")
        error = str(item.payload.pop("review_checkout_error", "") or "")
        ready = bool(item.payload.pop("review_checkout_ready", False))
        if error or not ready:
            retries = item.payload.get("review_checkout_retries", 0) + 1
            item.payload["review_checkout_retries"] = retries
            logger.warning(
                "pr_review:%s: review checkout not ready (%s); attempt %d",
                item.issue,
                error or "the pull request moved",
                retries,
            )
            if retries <= REVIEW_CHECKOUT_RETRY_CAP:
                return Continue(next_state=REVIEW_WAIT)
            item.payload.pop("review_checkout_retries", None)
            return self._cleanup_review_worktree_then(
                item, StageOutcome(Disposition.FINISH_FAIL, "review_checkout_unavailable")
            )
        item.payload.pop("review_checkout_retries", None)
        item.payload["reviewed_pr_head_sha"] = expected_head
        item.payload["reviewed_pr_node_id"] = item.payload.get("pr_node_id")
        routed = self._route_existing_threads(item, ctx, after_checkout=True)
        if routed is not None:
            return routed
        if item.payload.get(_COMMENT_VALIDATION_ONLY):
            return Continue(next_state=VALIDATE_WAIT)
        empty_diff = empty_diff_outcome(item)
        if empty_diff:
            return self._cleanup_review_worktree_then(item, empty_diff)
        return self._submit_review_job(item, ctx)

    def _submit_review_job(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Ask the reviewer to review the pull request."""
        issue = _issue_number(item)
        logger.info(
            "pr_review:%d: requesting review job (round %d, PR #%s)",
            issue,
            item.payload.get("pr_review_round", 0),
            item.pr,
        )
        try:
            workspace = source_workspace_binding(
                item,
                ctx,
                SourceLane.REVIEW,
                revision=str(item.payload.get("reviewed_pr_head_sha") or ""),
            )
        except (RuntimeError, SourceWorkspaceError) as error:
            logger.warning("pr_review:%d: review source binding failed: %s", issue, error)
            return self._cleanup_review_worktree_then(
                item, StageOutcome(Disposition.FINISH_FAIL, "review_source_binding_failed")
            )
        reviewer_agent = agent_provider(ctx, "reviewer")
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=reviewer_agent,
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=build_bounded_pr_review_analysis_prompt,
            cwd=workspace.cwd,
            timeout_s=stage_timeout(ctx, "reviewer", pr_reviewer_claude_timeout),
            workspace=workspace,
            session_agent=AGENT_PR_REVIEWER,
            resume_session_id=item.session_ids.get(AGENT_PR_REVIEWER),
            execution_request=ExecutionRequest(
                AgentRole.PR_REVIEWER,
                AgentOperation.PR_REVIEW,
                agent_session_lifecycle(item, AGENT_PR_REVIEWER),
            ),
            resume_binding=item.session_bindings.get(AGENT_PR_REVIEWER),
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep,Bash,Skill,Agent,WebFetch",
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": item.issue,
                "pr_diff": item.payload.get("pr_diff", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "pr_description": item.payload.get("pr_description", ""),
                "advise_findings": item.payload.get("advise_findings", ""),
                "repository_validation_json": "",
                "host_verifications_json": "[]",
                "host_verification_bootstrap_json": "",
                "include_nitpicks": ctx.config.nitpick,
                "review_context_kind": _review_context_kind(item),
                "reviewer_provider": reviewer_agent,
            },
            parse=_parse_review_response,
            descr="review",
        )
        item.payload["review_job_pending"] = True
        return JobRequest(job, on_done_state=POST)

    def _compact_reviewer_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Compact the reviewer before the next retry continues its session."""
        if not item.worktree:
            return Continue(next_state=REVIEW_WAIT)
        job = CompactJob(
            repo=item.repo,
            issue=_issue_number(item),
            agent=agent_provider(ctx, "reviewer"),
            session_agent=AGENT_PR_REVIEWER,
            model=stage_model(ctx, "reviewer", reviewer_model),
            cwd=_worktree_path(item, ctx),
            timeout_s=stage_timeout(ctx, "reviewer", pr_reviewer_claude_timeout()),
            session_id=item.session_ids.get(AGENT_PR_REVIEWER),
            sandbox="read-only",
            execution_request=ExecutionRequest(
                AgentRole.PR_REVIEWER,
                AgentOperation.COMPACT,
                SessionLifecycle.RESUME_REQUIRED,
            ),
            session_binding=item.session_bindings.get(AGENT_PR_REVIEWER),
        )
        return JobRequest(job, on_done_state=REVIEW_WAIT)

    @staticmethod
    def _restore_writer_worktree(item: WorkItem) -> None:
        """Give the item back the writer checkout that the review set aside."""
        writer_worktree = item.payload.pop("writer_worktree", None)
        if isinstance(writer_worktree, str) and writer_worktree:
            item.worktree = writer_worktree
            item.payload["implementation_writer_restored"] = True
        elif item.payload.get("review_worktree") == item.worktree:
            item.worktree = ""

    def _cleanup_review_worktree_then(
        self,
        item: WorkItem,
        outcome: StageOutcome,
    ) -> StepResult:
        """Remove the detached review checkout, then finish with *outcome*."""
        review_worktree = item.payload.get("review_worktree")
        if not isinstance(review_worktree, str) or not review_worktree:
            self._restore_writer_worktree(item)
            return outcome
        item.payload["review_worktree_cleanup_outcome"] = outcome.disposition.value
        item.payload["review_worktree_cleanup_note"] = outcome.note
        item.payload["review_worktree_cleanup_done"] = "pending"
        return Continue(next_state=CLEANUP_REVIEW_WORKTREE_WAIT)

    def _cleanup_review_worktree_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Remove the detached reviewer checkout and continue the saved outcome."""
        review_worktree = item.payload.get("review_worktree")
        if not isinstance(review_worktree, str) or not review_worktree:
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_invalid")
        cleanup_error = item.payload.pop("review_worktree_cleanup_error", None)
        if cleanup_error:
            item.worktree = review_worktree
            return StageOutcome(
                Disposition.FINISH_FAIL,
                f"review_worktree_cleanup_failed: "
                f"{redact_diagnostic_text(str(cleanup_error))[:500]}",
            )
        cleanup_state = item.payload.get("review_worktree_cleanup_done")
        if cleanup_state == "pending":
            job = GitJob(
                repo=item.repo,
                op="remove_worktree",
                timeout_s=stage_timeout(ctx, "metadata", GIT_JOB_TIMEOUT_S),
                expected_repository=f"{ctx.org}/{item.repo}",
                kwargs={
                    "worktree_path": review_worktree,
                    "repo_root": str(ctx.paths.repo_root),
                    "issue_number": item.issue or item.pr or 0,
                    "expected_head": item.payload.get("review_worktree_expected_head"),
                    "expected_detached": True,
                    "source_lane": SourceLane.REVIEW.value,
                    "force": False,
                },
                descr="remove_read_only_review_worktree",
            )
            return JobRequest(job, on_done_state=CLEANUP_REVIEW_WORKTREE_WAIT)
        if cleanup_state is not True:
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_state_invalid")
        outcome_value = item.payload.pop("review_worktree_cleanup_outcome", None)
        note = str(item.payload.pop("review_worktree_cleanup_note", "") or "")
        try:
            disposition = Disposition(str(outcome_value))
        except ValueError:
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_outcome_invalid")
        if disposition is Disposition.RETRY:
            item.worktree = ""
            if item.payload.get("writer_worktree"):
                item.payload["reviewer_checkout_needed"] = True
            item.state = ENTER
        else:
            self._restore_writer_worktree(item)
        item.payload.pop("review_worktree", None)
        for key in ("direct_pr_worktree", "direct_pr_worktree_dirty"):
            item.payload.pop(key, None)
        item.payload.pop("review_worktree_cleanup_done", None)
        item.payload.pop("review_worktree_expected_head", None)
        return StageOutcome(disposition, note)

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store one completed job result for the state that submitted it."""
        if self._consume_review_worktree_cleanup_result(item, result):
            return
        if item.state == POST:
            self._on_reconciliation_done(item, result)
            return
        if self._consume_direct_worktree_result(item, result):
            return
        if self._consume_review_checkout_result(item, result):
            return
        review_job_pending = bool(item.payload.pop("review_job_pending", None))
        if not result.ok:
            logger.warning("pr_review:%s: job failed: %s", item.issue, result.error)
            if review_job_pending or item.state == REVIEW_WAIT:
                item.payload["review_failed"] = True
            return
        if review_job_pending and result.value is not None:
            valid = self._store_review_result(item, result.value)
            if valid and item.payload.get("explicit_pr_review"):
                reviewed_head = item.payload.get("reviewed_pr_head_sha")
                if isinstance(reviewed_head, str) and is_full_commit_sha(reviewed_head):
                    record_review_run(item, reason="explicit-review", head_sha=reviewed_head)
        elif item.state == VALIDATE_WAIT and result.value is not None:
            item.payload["validation_result"] = result.value

    @staticmethod
    def _consume_direct_worktree_result(item: WorkItem, result: JobResult) -> bool:
        """Store a direct-worktree completion when one is pending."""
        if not item.payload.pop("direct_pr_worktree_pending", None):
            return False
        if not result.ok:
            logger.warning("pr_review:%s: direct PR worktree failed: %s", item.issue, result.error)
            item.worktree = ""
            item.payload["direct_pr_worktree_error"] = result.error or "worktree job failed"
            return True
        value = result.value
        if isinstance(value, dict):
            item.worktree = str(value.get("path", ""))
            item.payload["direct_pr_worktree_dirty"] = bool(value.get("dirty"))
            if item.worktree and not item.payload["direct_pr_worktree_dirty"]:
                item.payload["direct_pr_worktree"] = item.worktree
                item.payload["review_worktree"] = item.worktree
        else:
            item.payload["direct_pr_worktree_error"] = "worktree job returned no path"
        return True

    @staticmethod
    def _consume_review_worktree_cleanup_result(item: WorkItem, result: JobResult) -> bool:
        """Store one disposable-review-worktree cleanup result."""
        if item.payload.get("review_worktree_cleanup_done") != "pending":
            return False
        if result.ok:
            item.payload["review_worktree_cleanup_done"] = True
        else:
            item.payload["review_worktree_cleanup_error"] = result.error or "remove worktree failed"
        return True

    @staticmethod
    def _consume_review_checkout_result(item: WorkItem, result: JobResult) -> bool:
        """Store the review checkout result when one is pending."""
        if not item.payload.pop("review_checkout_pending", None):
            return False
        item.payload["review_checkout_ready"] = False
        if not result.ok:
            item.payload["review_checkout_error"] = result.error or "checkout job failed"
            return True
        value = result.value if isinstance(result.value, dict) else {}
        diff = value.get("diff")
        if value.get("ready") and isinstance(diff, str):
            item.payload["pr_diff"] = diff
            base = value.get("base")
            if isinstance(base, str) and is_full_commit_sha(base):
                item.payload["reviewed_pr_base_sha"] = base
            item.payload["review_checkout_ready"] = True
        return True

    @staticmethod
    def _store_review_result(item: WorkItem, value: object) -> bool:
        """Store the review and report whether the host could read it."""
        audit = (
            value.audit
            if isinstance(value, _ParsedReviewResponse)
            else value
            if isinstance(value, ReviewAudit)
            else None
        )
        if audit is None:
            item.payload["review_audit_failure"] = True
            return False
        item.payload["review_audit"] = audit
        item.payload["review_feedback"] = audit.raw_feedback
        item.payload["review_threads"] = [dict(comment) for comment in audit.findings]
        return audit.valid

    def _handoff_implementation(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Mark the pull request no-go and hand its open threads to the implementation agent.

        The implementation agent answers and resolves each thread. The review
        stage never writes source.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        no_go_outcome = self._write_no_go(item, ctx)
        if isinstance(no_go_outcome, StageOutcome):
            return self._cleanup_review_worktree_then(item, no_go_outcome)
        return self._cleanup_review_worktree_then(
            item,
            self._fail_back_implementation_remediation(item),
        )
