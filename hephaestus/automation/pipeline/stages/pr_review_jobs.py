# This mixin consumes the stage thread namespace by design.
# ruff: noqa: F403, F405
from pathlib import Path

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.workspace import SourceLane

from ..github_jobs import (
    DeliverReplyHandoffRequest,
    FrozenJson,
    GitHubJob,
    PrReviewReconciled,
    ReconcilePrReviewRequest,
    ReplyHandoffAttempted,
)
from .base import source_workspace_binding, stage_timeout
from .pr_review_diagnostics import publish_host_verification_failure
from .pr_review_recovery import (
    consume_reply_handoff_receipt,
    empty_diff_outcome,
    restart_direct_pr_review,
)
from .pr_review_threads import *
from .pr_review_threads import (
    _REPLY_HANDOFF_RECEIPT,
    _REPLY_HANDOFF_RECEIPT_ERROR,
    POST_APPLY,
    _finding_key,
)

_PENDING_GITHUB_REQUEST = "_pending_github_request"
_PR_REVIEW_RECEIPT = "_pr_review_reconciliation_receipt"
_PR_REVIEW_RECEIPT_ERROR = "_pr_review_reconciliation_error"


class PrReviewJobs(_PrReviewHost):
    """Own review worktrees, validation jobs, and result handoffs."""

    @staticmethod
    def _fail_back_implementation_remediation(item: WorkItem) -> StageOutcome:
        """Route unresolved review work back to implementation."""
        item.payload["implementation_remediation"] = True
        return StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Hydrate review inputs, require an unarmed PR, and reset the round counter.

        The per-cycle review budget lives in ``payload["pr_review_round"]``.
        Its reset keys on ``attempts["implement"]``
        so it fires exactly once per implementation pass: a same-cycle
        re-entry (e.g. the ERROR-path RETRY) keeps its round count and its
        Args:
            item: The work item being processed.
            ctx: The stage context.

        """
        if item.pr is not None:
            item.payload.pop("reviewed_pr_head_sha", None)
            arm_outcome = self._require_confirmed_unarmed(item.pr, ctx)
            if arm_outcome is not None:
                return arm_outcome
            if item.state == ENTER:
                thread_outcome = self._route_existing_threads_before_audit(item, ctx)
                if thread_outcome is not None:
                    return thread_outcome
            if item.worktree and not item.payload.get("direct_pr_worktree"):
                item.payload["writer_worktree"] = item.worktree
                item.payload["reviewer_checkout_needed"] = True
                item.worktree = ""
        cycle = item.attempts.get("implement", 0)
        if item.payload.get("pr_review_cycle") != cycle:
            item.payload["pr_review_cycle"] = cycle
            item.payload["pr_review_round"] = 0
            item.payload.pop("review_error_retries", None)
        return None

    @staticmethod
    def _route_existing_threads_before_audit(
        item: WorkItem, ctx: StageContext
    ) -> StageOutcome | None:
        """Route inherited threads to their responsible role before a new audit.

        An unresolved thread lacking a current-head implementation response is
        writer work, not input for another broad review.  Conversely, a
        complete current-head response set enters the detached checkout only
        for reviewer comment validation, where the reviewer may resolve the
        threads or explain why they remain open.
        """
        if item.pr is None:  # guarded by on_enter; keeps type narrowing local
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        entry = PrReviewStage._read_existing_thread_entry(item, ctx)
        if entry is None:
            return None
        if isinstance(entry, StageOutcome):
            return entry
        live_threads, remediation_threads, snapshots = entry

        branch = item.branch or ctx.github.get_pr_head_branch(item.pr)
        if not branch:
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_no_head_branch")
        item.branch = branch
        item.payload["existing_pr"] = True

        if all(bool(snapshot.get("implementation_reply_submitted")) for snapshot in snapshots):
            item.payload[_COMMENT_VALIDATION_ONLY] = True
            return None

        # Scope-retraction remediation needs a base proof that only the
        # detached checkout can derive.  Preserve the safe established path
        # for this exceptional directive instead of sending an unprovable
        # retraction to a writer.
        scope_retraction_paths = _scope_retraction_paths(remediation_threads)
        if scope_retraction_paths is None:
            return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_path_invalid")
        if scope_retraction_paths:
            item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
            item.payload.pop("reviewed_pr_head_sha", None)
            return None

        item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
        item.payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        item.payload["remediation_threads"] = remediation_threads
        item.payload["remediation_thread_snapshots"] = [dict(thread) for thread in live_threads]
        item.payload["unresolved_threads_before_address"] = len(remediation_threads)
        no_go_outcome = PrReviewStage._write_no_go(item, ctx)
        if no_go_outcome is not None:
            if isinstance(no_go_outcome, StageOutcome):
                return no_go_outcome
            return StageOutcome(Disposition.FINISH_FAIL, "reviewed_head_drift")
        return PrReviewStage._fail_back_implementation_remediation(item)

    @staticmethod
    def _read_existing_thread_entry(
        item: WorkItem, ctx: StageContext
    ) -> (
        tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]
        | StageOutcome
        | None
    ):
        """Return a current-head complete thread snapshot for entry routing."""
        if item.pr is None:  # guarded by on_enter; keeps type narrowing local
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        try:
            initial_threads = ctx.github.list_unresolved_review_threads(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not read existing review threads at entry (%s)",
                item.issue,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "review_threads_unavailable")
        if not initial_threads:
            item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
            return None
        bind_outcome = PrReviewStage._bind_current_head_for_negative(item, ctx)
        if bind_outcome is not None:
            return bind_outcome
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            receipts = ctx.github.reviewer_validation_receipts(
                item.pr,
                reviewed_head_sha=reviewed_head,
                threads=live_threads,
            )
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not read existing thread responses at entry (%s)",
                item.issue,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "review_thread_receipts_unavailable")
        if not live_threads:
            # The thread set changed while its negative transition was being
            # bound.  The normal checkout path obtains the only valid clean
            # review proof; do not reuse this negative-only head binding.
            item.payload.pop("reviewed_pr_head_sha", None)
            item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
            return None
        snapshots = _validation_thread_snapshots(live_threads, receipts)
        remediation_threads = _normalize_remediation_threads(live_threads)
        if (
            snapshots is None
            or _validation_receipt_fingerprints(receipts) is None
            or len(remediation_threads) != len(live_threads)
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "review_thread_receipts_invalid")
        return (live_threads, remediation_threads, snapshots)

    def _enter(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ENTER advances to REVIEW_WAIT."""
        return Continue(next_state=REVIEW_WAIT)

    def _adopt_direct_pr_worktree(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Create an isolated checkout for the checkout-bound PR review barrier."""
        if item.pr is None:  # guarded by step(); keeps type narrowing local
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        branch = ctx.github.get_pr_head_branch(item.pr) or item.branch
        if not branch:
            logger.error("pr_review:%s: no head branch for direct PR #%d", item.issue, item.pr)
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_no_head_branch")
        item.branch = branch
        item.payload["existing_pr"] = True
        generation = item.payload.get("direct_pr_worktree_generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_generation_invalid")
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
            kwargs=kwargs,
            descr="direct_pr_review_worktree",
        )
        item.payload["direct_pr_worktree_pending"] = True
        return JobRequest(job, on_done_state=ADOPT_WORKTREE_WAIT)

    def _adopt_worktree_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Advance only from a detached direct-PR checkout for later binding."""
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
        """Refresh review inputs, then bind the checkout before dispatch."""
        # Clear ALL round-scoped payload at submission (stale-result
        # guard, M3 pattern): a failed later round must never replay an
        # earlier round's verdict, threads, or address output.
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
        """Submit review only after the fresh snapshot matches a clean checkout."""
        expected_head = str(item.payload.pop("review_checkout_expected_head", "") or "")
        error = str(item.payload.pop("review_checkout_error", "") or "")
        ready = bool(item.payload.pop("review_checkout_ready", False))
        if error:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_checkout_unavailable"),
            )
        if not ready:
            # A review is a one-shot immutable snapshot.  Do not retry by
            # mutating the PR branch (or repeatedly re-fetching it) here: the
            # next loop item will take a fresh detached snapshot if needed.
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_checkout_head_drift"),
            )
        item.payload["reviewed_pr_head_sha"] = expected_head
        prior_generation = item.payload.get("reviewed_pr_proof_generation", 0)
        if isinstance(prior_generation, bool) or not isinstance(prior_generation, int):
            prior_generation = 0
        item.payload["reviewed_pr_proof_generation"] = prior_generation + 1
        verifications = _prepare_host_checks(item.payload, _worktree_path(item, ctx), expected_head)
        if verifications:
            logger.info(
                "pr_review:%d: requesting %d host verifications",
                _issue_number(item),
                len(verifications),
            )
            item.payload["host_verification_receipts"] = []
            return self._submit_host_verification(item, ctx, verifications[0])
        return self._route_threads_before_broad_review(item, ctx)

    @staticmethod
    def _submit_host_verification(
        item: WorkItem, ctx: StageContext, verification: _HostVerificationSpec
    ) -> JobRequest:
        """Submit one fixed host command from the immutable review plan."""
        # Completion callbacks run before the coordinator installs
        # ``on_done_state``.  Keep an explicit ownership marker instead of
        # inferring this job's type from the current mini-state, which can
        # also be the state that submits the primary review job.
        item.payload[_HOST_VERIFICATION_PENDING] = verification.descr
        return JobRequest(
            BuildTestJob(
                repo=item.repo,
                cwd=_worktree_path(item, ctx),
                argv=verification.argv,
                timeout_s=HOST_VERIFICATION_TIMEOUT_S,
                expected_head_sha=str(item.payload.get("reviewed_pr_head_sha") or ""),
                immutable_source=True,
                descr=verification.descr,
            ),
            on_done_state=HOST_VERIFICATION_WAIT,
        )

    def _submit_review_job(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Create the agent job after the checkout/head barrier succeeds."""
        issue = _issue_number(item)
        round_index = item.payload.get("pr_review_round", 0)
        logger.info(
            "pr_review:%d: requesting review job (round %d, PR #%d)",
            issue,
            round_index,
            item.pr,
        )
        workspace = source_workspace_binding(
            item,
            ctx,
            SourceLane.REVIEW,
            revision=str(item.payload.get("reviewed_pr_head_sha") or ""),
        )
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_pr_review_analysis_prompt,
            cwd=workspace.cwd if workspace else _worktree_path(item, ctx),
            timeout_s=stage_timeout(ctx, "reviewer", pr_reviewer_claude_timeout),
            workspace=workspace,
            session_agent=AGENT_PR_REVIEWER,
            resume_session_id=item.session_ids.get(AGENT_PR_REVIEWER),
            execution_request=ExecutionRequest(
                AgentRole.PR_REVIEWER,
                AgentOperation.PR_REVIEW,
                (
                    SessionLifecycle.RESUME_REQUIRED
                    if AGENT_PR_REVIEWER in item.session_bindings
                    else SessionLifecycle.START_NEW
                ),
            ),
            resume_binding=item.session_bindings.get(AGENT_PR_REVIEWER),
            sandbox="read-only",
            # The normal $athena:pr-review skill is read-only, but its
            # declared workflow uses local Bash helpers and review subagents.
            # Keep that capability on the sole GO/NOGO review job only;
            # validation and difficulty jobs retain WorkerPool's read scope.
            allowed_tools="Read,Glob,Grep,Bash,Skill,Agent,WebFetch",
            # on_enter refreshes diff and body context through the stage
            # adapter before every review cycle.
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": item.issue,
                "pr_diff": item.payload.get("pr_diff", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "pr_description": item.payload.get("pr_description", ""),
                "advise_findings": item.payload.get("advise_findings", ""),
                "host_verifications_json": json.dumps(
                    item.payload.get("host_verification_receipts", []), sort_keys=True
                ),
                "include_nitpicks": bool(
                    getattr(
                        ctx.config,
                        "nitpick",
                        getattr(ctx.config, "include_nitpicks", False),
                    )
                ),
                "review_context_kind": _review_context_kind(item),
            },
            parse=_parse_review_response,  # structural audit parsed in-worker
            descr="review",
        )
        item.payload["review_job_pending"] = True
        return JobRequest(job, on_done_state=VALIDATE_WAIT)

    def _route_threads_before_broad_review(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Route threads appearing during checkout before broad review."""
        if item.pr is None:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "no_pr"),
            )
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        if not is_full_commit_sha(reviewed_head):
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "reviewed_head_unavailable"),
            )
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            receipts = ctx.github.reviewer_validation_receipts(
                item.pr,
                reviewed_head_sha=reviewed_head,
                threads=live_threads,
            )
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not reread threads before broad review (%s)",
                item.issue,
                type(error).__name__,
            )
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_thread_receipts_unavailable"),
            )
        if not live_threads:
            if item.payload.get(_COMMENT_VALIDATION_ONLY):
                # A reply may resolve the last thread while the immutable
                # host checks are running. Keep the already-selected
                # validation-only route instead of opening a second audit.
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

    def _validate_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Perform a fresh review of implementation replies before resolution."""
        issue = _issue_number(item)
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.payload.pop("review_failed", None):
            # The review job itself failed: skip the validate/post/
            # address leg — EVAL's missing-verdict ERROR path handles it
            # without burning a round.
            return Continue(next_state=EVAL)
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            receipts = (
                ctx.github.reviewer_validation_receipts(
                    item.pr,
                    reviewed_head_sha=reviewed_head,
                    threads=live_threads,
                )
                if is_full_commit_sha(reviewed_head)
                else []
            )
            pr_context = ctx.github.pr_review_context(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not fetch validation receipts (%s)",
                item.issue,
                type(error).__name__,
            )
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        metadata_fingerprint = _validation_pr_metadata_fingerprint(pr_context, reviewed_head)
        if metadata_fingerprint is None:
            logger.warning(
                "pr_review:%s: fresh validation metadata did not match reviewed head",
                item.issue,
            )
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        validated_pr_context = cast(dict[str, str], pr_context)
        pr_title = validated_pr_context["pr_title"]
        pr_description = validated_pr_context["pr_description"]
        validation_threads = _validation_thread_snapshots(live_threads, receipts)
        receipt_fingerprints = _validation_receipt_fingerprints(receipts)
        if validation_threads is None or receipt_fingerprints is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["validation_threads"] = validation_threads
        item.payload["validation_receipt_fingerprints"] = receipt_fingerprints
        item.payload["validation_pr_metadata_fingerprint"] = metadata_fingerprint
        item.payload["prior_comments_json"] = json.dumps(
            validation_threads, ensure_ascii=False, sort_keys=True
        )
        logger.info("pr_review:%d: requesting validation job", issue)
        workspace = source_workspace_binding(
            item,
            ctx,
            SourceLane.REVIEW,
            revision=reviewed_head,
        )
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_review_validation_prompt,
            cwd=workspace.cwd if workspace else _worktree_path(item, ctx),
            timeout_s=stage_timeout(ctx, "reviewer", pr_reviewer_claude_timeout),
            workspace=workspace,
            session_agent=AGENT_PR_REVIEWER,
            resume_session_id=item.session_ids.get(AGENT_PR_REVIEWER),
            execution_request=ExecutionRequest(
                AgentRole.PR_REVIEWER,
                AgentOperation.REVIEW_VALIDATE,
                (
                    SessionLifecycle.RESUME_REQUIRED
                    if AGENT_PR_REVIEWER in item.session_bindings
                    else SessionLifecycle.START_NEW
                ),
            ),
            resume_binding=item.session_bindings.get(AGENT_PR_REVIEWER),
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": item.issue,
                "prior_comments_json": item.payload["prior_comments_json"],
                "diff_text": item.payload.get("pr_diff", ""),
                "pr_title": pr_title,
                "pr_description": pr_description,
                "host_verifications_json": json.dumps(
                    item.payload.get("host_verification_receipts", []), sort_keys=True
                ),
                "review_context_kind": _review_context_kind(item),
            },
            descr="validate",
        )
        return JobRequest(job, on_done_state=POST)

    def _host_verification_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit primary review only after its host verification passed."""
        verifications = _payload_host_verification_specs(item.payload)
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        receipts = item.payload.get("host_verification_receipts")
        if not isinstance(receipts, list) or len(receipts) > len(verifications):
            return self._handle_host_verification_failure(
                item,
                ctx,
                None,
                "host_verification_receipt_invalid",
            )
        matched_receipts = cast(list[dict[str, Any]], receipts)
        for verification, receipt in zip(verifications, matched_receipts, strict=False):
            if not _host_verification_receipt_matches(receipt, verification, reviewed_head):
                return self._handle_host_verification_failure(
                    item,
                    ctx,
                    verification,
                    str(receipt.get("error") or "host_verification_receipt_invalid"),
                )
            if receipt["ok"] or receipt.get("status") == "skipped":
                continue
            return self._handle_host_verification_failure(
                item,
                ctx,
                verification,
                str(receipt.get("error") or "host_verification_failed"),
            )
        if len(matched_receipts) < len(verifications):
            return self._submit_host_verification(item, ctx, verifications[len(matched_receipts)])
        if not _host_verification_receipts_match(receipts, verifications, reviewed_head):
            return self._handle_host_verification_failure(
                item,
                ctx,
                None,
                "host_verification_receipt_invalid",
            )
        return self._route_threads_before_broad_review(item, ctx)

    def _handle_host_verification_failure(
        self,
        item: WorkItem,
        ctx: StageContext,
        verification: _HostVerificationSpec | None,
        reason: str,
    ) -> StepResult:
        """Durably reject a failed host test without entering audit retries."""
        receipts = item.payload.get("host_verification_receipts")
        receipt = (
            receipts[-1]
            if isinstance(receipts, list) and receipts and isinstance(receipts[-1], dict)
            else None
        )
        diagnostic = {
            "argv": list(verification.argv) if verification is not None else [],
            "path": ((verification.changed_path or "") if verification is not None else ""),
            "head_sha": str(item.payload.get("reviewed_pr_head_sha") or ""),
            "failure_kind": (
                str(receipt.get("failure_kind") or "unknown")
                if isinstance(receipt, dict)
                else "unknown"
            ),
            "error": reason[:HOST_VERIFICATION_DIAGNOSTIC_MAX],
            "stdout_tail": (
                str(receipt.get("stdout_tail") or "")[-HOST_VERIFICATION_DIAGNOSTIC_MAX:]
                if isinstance(receipt, dict)
                else ""
            ),
            "stderr_tail": (
                str(receipt.get("stderr_tail") or "")[-HOST_VERIFICATION_DIAGNOSTIC_MAX:]
                if isinstance(receipt, dict)
                else ""
            ),
        }
        item.payload["host_verification_failure"] = diagnostic
        no_go_outcome = PrReviewStage._write_no_go(item, ctx)
        if no_go_outcome is not None:
            if isinstance(no_go_outcome, StageOutcome):
                return self._cleanup_review_worktree_then(item, no_go_outcome)
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "reviewed_head_drift"),
            )

        pr_number = cast(int, item.pr)  # _write_no_go rejected a missing PR above.
        if not publish_host_verification_failure(
            ctx.github, pr_number, verification, diagnostic, logger
        ):
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "host_verification_comment_failed"),
            )

        # Only a confirmed fixed-tool validation failure may be repaired by
        # the implementation agent. UV/sandbox/bootstrap errors share a
        # nonzero process status but are operator remediation, not code work.
        failure_kind = receipt.get("failure_kind") if isinstance(receipt, dict) else None
        if failure_kind in {"test", "validation"}:
            detail = (
                "Host verification failed for "
                f"{diagnostic['path']}: {reason}. Investigate and fix the test or "
                "implementation, then rerun the fixed verification command."
            )
            if item.payload.get("existing_pr"):
                item.payload["unaddressed_findings"] = [
                    {"path": diagnostic["path"], "line": None, "body": detail}
                ]
            return Continue(next_state=ADDRESS_WAIT)
        return self._cleanup_review_worktree_then(
            item,
            StageOutcome(Disposition.FINISH_FAIL, "host_verification_failed"),
        )

    def _push_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Hand an interrupted legacy writer state to implementation safely."""
        # This legacy state can be resumed after an interrupted older run.
        # Never let it regain a writer capability in the review stage.
        return self._cleanup_review_worktree_then(
            item,
            self._fail_back_implementation_remediation(item),
        )

    def _compact_reviewer_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Compact the reviewer before the next retry continues its session."""
        if not item.worktree:
            return Continue(next_state=COMPACT_WRITER_WAIT)
        job = CompactJob(
            repo=item.repo,
            issue=_issue_number(item),
            agent=agent_provider(ctx),
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
        return JobRequest(job, on_done_state=COMPACT_WRITER_WAIT)

    def _compact_writer_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Compact the writer before the next retry continues its session."""
        if not item.worktree:
            return Continue(next_state=REVIEW_WAIT)
        session_agent = (
            AGENT_ADDRESS_REVIEW if item.payload.get("existing_pr") else AGENT_IMPLEMENTER
        )
        job = CompactJob(
            repo=item.repo,
            issue=_issue_number(item),
            agent=agent_provider(ctx),
            session_agent=session_agent,
            model=stage_model(ctx, "implementer", implementer_model),
            cwd=_worktree_path(item, ctx),
            timeout_s=stage_timeout(ctx, "implementer", implementer_claude_timeout()),
            session_id=item.session_ids.get(session_agent),
            sandbox="read-only",
            execution_request=ExecutionRequest(
                AgentRole.IMPLEMENTER,
                AgentOperation.COMPACT,
                SessionLifecycle.RESUME_REQUIRED,
            ),
            session_binding=item.session_bindings.get(session_agent),
        )
        return JobRequest(job, on_done_state=REVIEW_WAIT)

    @staticmethod
    def _restore_writer_worktree(item: WorkItem) -> None:
        """Restore the implementation-owned checkout after reviewer cleanup.

        The reviewer always receives a detached disposable checkout.  A writer
        checkout may exist only because an earlier implementation pass created
        it; keeping its path in the item lets the implementation stage resume
        the same branch without making the reviewer a writer or creating a
        second worktree for that branch.
        """
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
        """Remove the detached review snapshot before leaving this stage.

        A reviewer may inspect exactly one fetched PR head.  Its checkout is
        evidence, not a recovery branch, so it must be removed before either
        a writer handoff or a terminal outcome.  We retain the intended stage
        disposition in memory until the removal job completes; cleanup failure
        is terminal so a potentially dirty snapshot is never silently lost.
        """
        review_worktree = item.payload.get("review_worktree")
        if not isinstance(review_worktree, str) or not review_worktree:
            self._restore_writer_worktree(item)
            return outcome
        item.payload["review_worktree_cleanup_outcome"] = outcome.disposition.value
        item.payload["review_worktree_cleanup_note"] = outcome.note
        item.payload["review_worktree_cleanup_done"] = "pending"
        return Continue(next_state=CLEANUP_REVIEW_WORKTREE_WAIT)

    def _cleanup_review_worktree_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Remove the detached reviewer checkout and continue its saved outcome."""
        review_worktree = item.payload.get("review_worktree")
        if not isinstance(review_worktree, str) or not review_worktree:
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_invalid")
        if item.payload.pop("review_worktree_cleanup_error", None):
            item.worktree = review_worktree
            return StageOutcome(Disposition.FINISH_FAIL, "review_worktree_cleanup_failed")
        cleanup_state = item.payload.get("review_worktree_cleanup_done")
        if cleanup_state == "pending":
            expected_head = item.payload.get("review_worktree_expected_head")
            if not is_full_commit_sha(expected_head):
                return StageOutcome(
                    Disposition.FINISH_FAIL, "review_worktree_cleanup_identity_invalid"
                )
            job = GitJob(
                repo=item.repo,
                op="remove_worktree",
                timeout_s=stage_timeout(ctx, "metadata", GIT_JOB_TIMEOUT_S),
                kwargs={
                    "worktree_path": review_worktree,
                    "repo_root": str(ctx.paths.repo_root),
                    "issue_number": item.issue or item.pr or 0,
                    "expected_head": expected_head,
                    "expected_detached": True,
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

    def on_job_done(  # noqa: C901
        self, item: WorkItem, result: JobResult, ctx: StageContext
    ) -> None:
        """Store job results on the item payload (state is still the WAIT state).

        Args:
            item: The work item to update.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.state == POST:
            self._on_reconciliation_done(item, result)
            return
        if item.state == RECOVERY_REPLY_WAIT:
            self._on_reply_handoff_done(item, result)
            return
        if self._consume_review_worktree_cleanup_result(item, result):
            return
        if self._consume_direct_worktree_result(item, result):
            return
        if self._consume_review_checkout_result(item, result):
            return
        if self._consume_host_verification_result(item, result):
            self._store_host_verification_result(item, result)
            return

        review_job_pending = bool(item.payload.pop("review_job_pending", None))
        is_review_result = review_job_pending or item.state == REVIEW_WAIT
        if self._consume_failed_job(item, result, is_review_result):
            return

        if item.state == PUSH_WAIT:
            # Real-commit gate (#1575): commit_push reports whether a commit
            # was actually produced (value/changed True). A no-commit push
            # means the address turn was a phantom fix — EVAL must NOT treat
            # the round as addressed.
            push_receipt = result.value if isinstance(result.value, dict) else {}
            raw_published_head = push_receipt.get("head_sha")
            published_head = raw_published_head if isinstance(raw_published_head, str) else ""
            produced_commit = bool(push_receipt.get("pushed")) and is_full_commit_sha(
                published_head
            )
            if produced_commit:
                remediation_threads = item.payload.get("remediation_threads")
                threads = remediation_threads if isinstance(remediation_threads, list) else []
                snapshots = item.payload.get("remediation_thread_snapshots")
                thread_snapshots = snapshots if isinstance(snapshots, list) else []
                replies = _address_replies(item.payload.get("address_output"), threads)
                reply_contract_failed = bool(threads) and replies is None
                if reply_contract_failed:
                    logger.warning(
                        "pr_review:%s: implementation did not return one reply for every open "
                        "thread; refusing to accept a partial address pass",
                        item.issue,
                    )
                elif replies and item.pr is not None:
                    handoff = _implementation_reply_handoff(
                        published_head,
                        thread_snapshots,
                        replies,
                        secrets.token_hex(16),
                    )
                    if handoff is None:
                        logger.warning(
                            "pr_review:%s: could not preserve the exact implementation "
                            "reply handoff; refusing to infer a replacement response",
                            item.issue,
                        )
                    else:
                        # Keep the exact, already-validated agent output until
                        # GitHub proves every reply. _clear_round_review_state
                        # deliberately does not clear this handoff because the
                        # code commit has already changed the review head.
                        item.payload[_PENDING_IMPLEMENTATION_REPLY_HANDOFF] = handoff
                        item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
                # The old audit and checkout receipt describe the pre-push
                # head. Discard the entire round before EVAL can bind the new
                # head to any implementation-state transition.
                _clear_round_review_state(item)
                item.payload["review_refresh_required"] = True
                if reply_contract_failed:
                    # The code commit may already be durable, but accepting an
                    # incomplete agent transcript would let one supplied open
                    # thread disappear from the implementation handoff. Route
                    # back through the normal bounded implementation recovery.
                    item.payload["address_error"] = True
            else:
                # Preserve the existing no-commit gate while requiring it to
                # re-confirm the unchanged remote head before a negative write.
                item.payload.pop("reviewed_pr_head_sha", None)
            item.payload["push_no_commit"] = not produced_commit
            return

        if is_review_result and result.value is not None:
            self._store_review_result(item, result.value)
        elif item.state == VALIDATE_WAIT and result.value is not None:
            item.payload["validation_result"] = result.value
        elif item.state == ADDRESS_WAIT and result.value is not None:
            item.payload["address_output"] = result.value

    @staticmethod
    def _consume_direct_worktree_result(item: WorkItem, result: JobResult) -> bool:
        """Store a direct-worktree completion when one is pending."""
        if not item.payload.pop("direct_pr_worktree_pending", None):
            return False
        PrReviewStage._on_direct_pr_worktree_done(item, result)
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
        """Store the review checkout barrier result when one is pending."""
        if not item.payload.pop("review_checkout_pending", None):
            return False
        if not result.ok:
            item.payload["review_checkout_error"] = result.error or "checkout job failed"
            return True
        value = result.value
        ready = bool(isinstance(value, dict) and value.get("ready"))
        review_diff = value.get("diff") if isinstance(value, dict) else None
        review_base = value.get("base") if isinstance(value, dict) else None
        changed_paths = value.get("changed_paths") if isinstance(value, dict) else None
        if ready and not isinstance(review_diff, str):
            item.payload["review_checkout_error"] = "checkout job returned no bound diff"
            ready = False
        if ready:
            item.payload["pr_diff"] = review_diff
            if isinstance(changed_paths, list) and all(
                isinstance(path, str) and bool(path) for path in changed_paths
            ):
                item.payload["review_changed_paths"] = list(changed_paths)
            if is_full_commit_sha(review_base):
                item.payload["reviewed_pr_base_sha"] = review_base
        item.payload["review_checkout_ready"] = ready
        return True

    @staticmethod
    def _consume_host_verification_result(item: WorkItem, result: JobResult) -> bool:
        """Claim one fixed host-check completion independent of mini-state."""
        del result
        return item.payload.pop(_HOST_VERIFICATION_PENDING, None) is not None

    @staticmethod
    def _store_host_verification_result(item: WorkItem, result: JobResult) -> None:
        """Append a bounded, head-bound receipt from the fixed host plan."""
        specs = _payload_host_verification_specs(item.payload)
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        receipts = item.payload.get("host_verification_receipts")
        if (
            not specs
            or not is_full_commit_sha(reviewed_head)
            or not isinstance(receipts, list)
            or len(receipts) >= len(specs)
        ):
            item.payload.pop("host_verification_receipts", None)
            return
        spec = specs[len(receipts)]
        result_value = result.value if isinstance(result.value, dict) else {}
        status, platform = _host_verification_result_status(
            result.value, result.ok, result.error, reviewed_head
        )
        receipts.append(
            {
                "argv": list(spec.argv),
                "head_sha": reviewed_head,
                "immutable_source": bool(
                    isinstance(result.value, dict)
                    and result.value.get("head_sha") == reviewed_head
                    and result.value.get("immutable_source") is True
                ),
                "failure_kind": _host_verification_failure_kind(result_value),
                "ok": result.ok,
                "error": result.error or "",
                "platform": platform,
                "status": status,
                "stdout_tail": result.stdout_tail,
                "stderr_tail": result.stderr_tail,
            }
        )

    def _consume_failed_job(
        self, item: WorkItem, result: JobResult, is_review_result: bool
    ) -> bool:
        """Store a failed result and report whether completion handling is done."""
        if result.ok:
            return False
        if is_review_result:
            item.payload["review_failed"] = True
            return True
        self._on_job_failed(item, result)
        return True

    @staticmethod
    def _store_review_result(item: WorkItem, value: object) -> None:
        """Persist one structural reviewer result."""
        if isinstance(value, _ParsedReviewResponse):
            item.payload["review_audit"] = value.audit
            item.payload["review_feedback"] = value.audit.raw_feedback
            item.payload["review_threads"] = [dict(comment) for comment in value.audit.findings]
            return
        if isinstance(value, ReviewAudit):
            item.payload["review_audit"] = value
            item.payload["review_feedback"] = value.raw_feedback
            item.payload["review_threads"] = [dict(comment) for comment in value.findings]
            return
        item.payload["review_audit_failure"] = True

    @staticmethod
    def _on_direct_pr_worktree_done(item: WorkItem, result: JobResult) -> None:
        """Record the exact checkout created for a direct PR review."""
        if not result.ok:
            logger.warning("pr_review:%s: direct PR worktree failed: %s", item.issue, result.error)
            item.worktree = ""
            item.payload["direct_pr_worktree_error"] = result.error or "worktree job failed"
            return
        value = result.value
        if isinstance(value, dict):
            item.worktree = str(value.get("path", ""))
            item.payload["direct_pr_worktree_dirty"] = bool(value.get("dirty"))
            if item.worktree and not item.payload["direct_pr_worktree_dirty"]:
                item.payload["direct_pr_worktree"] = item.worktree
                item.payload["review_worktree"] = item.worktree
        elif isinstance(value, str):
            item.worktree = value
            if item.worktree:
                item.payload["direct_pr_worktree"] = item.worktree
                item.payload["review_worktree"] = item.worktree
        else:
            item.payload["direct_pr_worktree_error"] = "worktree job returned no path"

    @staticmethod
    def _on_job_failed(item: WorkItem, result: JobResult) -> None:
        """Record the state-specific failure outcome for a non-git agent job."""
        logger.warning("pr_review:%s: job failed: %s", item.issue, result.error)
        if item.state == REVIEW_WAIT:
            # EVAL treats the missing audit as reviewer infrastructure failure;
            # the flag lets VALIDATE_WAIT skip the dead round.
            item.payload["review_failed"] = True
        elif item.state == PUSH_WAIT:
            receipt = result.value if isinstance(result.value, dict) else {}
            if receipt.get("scope_retraction_failure") is True:
                item.payload["scope_retraction_failure"] = True
                return
            failure = receipt.get("detached_push_failure")
            if failure in {
                "remote_changed",
                "remote_changed_unrecorded",
                "remote_unchanged",
                "remote_unconfirmed",
                "retry_checkout_changed",
                "retry_checkout_unconfirmed",
            }:
                item.payload["detached_push_failure"] = failure
                source_sha = receipt.get("detached_push_head_sha")
                if is_full_commit_sha(source_sha):
                    item.payload["detached_push_head_sha"] = source_sha
                return
            if item.payload.get("direct_pr_worktree") and item.worktree:
                # A direct-review checkout may hold an address commit even
                # when publication setup itself failed before it could return
                # a classified receipt. Preserve rather than failing back to
                # an agent re-adoption path that could orphan that commit.
                item.payload["detached_push_failure"] = "remote_unconfirmed"
                return
            item.payload["address_error"] = True
        elif item.state == ADDRESS_WAIT:
            item.payload["address_error"] = True

    @staticmethod
    def _on_reconciliation_done(item: WorkItem, result: JobResult) -> None:
        """Store only an exact request-bearing reconciliation receipt."""
        if not result.ok:
            retries = item.payload.get("pr_review_reconciliation_retries", 0)
            if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
                item.payload[_PR_REVIEW_RECEIPT_ERROR] = "invalid"
                return
            retries += 1
            item.payload["pr_review_reconciliation_retries"] = retries
            item.payload[_PR_REVIEW_RECEIPT_ERROR] = (
                "retry" if retries <= REVIEW_ERROR_RETRY_CAP else "failed"
            )
            return
        receipt = result.value
        if not isinstance(receipt, PrReviewReconciled) or receipt.request != item.payload.get(
            _PENDING_GITHUB_REQUEST
        ):
            item.payload[_PR_REVIEW_RECEIPT_ERROR] = "invalid"
            return
        item.payload[_PR_REVIEW_RECEIPT] = receipt

    def _post_apply(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Apply a correlated immutable reconciliation receipt locally."""
        del ctx
        error = item.payload.pop(_PR_REVIEW_RECEIPT_ERROR, None)
        if error == "retry":
            item.state = POST
            return StageOutcome(Disposition.RETRY, "pr_review_reconciliation_retry")
        if error in {"failed", "invalid"}:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        receipt = item.payload.pop(_PR_REVIEW_RECEIPT, None)
        if not isinstance(receipt, PrReviewReconciled) or receipt.request != item.payload.get(
            _PENDING_GITHUB_REQUEST
        ):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload.pop(_PENDING_GITHUB_REQUEST, None)
        item.payload.pop("pr_review_reconciliation_retries", None)
        if receipt.action == "revalidate":
            item.payload.pop("validation_result", None)
            item.payload.pop("validation_threads", None)
            item.payload.pop("validation_receipt_fingerprints", None)
            item.payload.pop("validation_pr_metadata_fingerprint", None)
            return Continue(next_state=VALIDATE_WAIT)
        if receipt.action == "fresh_review":
            item.payload.pop("validation_result", None)
            item.payload.pop("validation_threads", None)
            item.payload.pop("validation_receipt_fingerprints", None)
            item.payload.pop("validation_pr_metadata_fingerprint", None)
            return Continue(next_state=REVIEW_WAIT)
        if receipt.action == "audit_failure":
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)

        posted = receipt.posted_receipts.thaw()
        unresolved = receipt.unresolved_threads.thaw()
        remediation = receipt.remediation_threads.thaw()
        if (
            not isinstance(posted, list)
            or not isinstance(unresolved, list)
            or not isinstance(remediation, list)
        ):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        raw_findings = receipt.request.findings.thaw()
        if not isinstance(raw_findings, list) or not all(
            isinstance(value, dict) for value in raw_findings
        ):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        posted_keys = {
            key for value in posted if isinstance(value, dict) and (key := _finding_key(value))
        }
        item.payload["review_threads"] = [
            dict(value) for value in raw_findings if _finding_key(value) in posted_keys
        ]
        item.payload["posted_thread_ids"] = [
            str(value["id"]) for value in posted if isinstance(value, dict) and "id" in value
        ]
        item.payload["unresolved_threads"] = [dict(value) for value in unresolved]
        item.payload["remediation_threads"] = [dict(value) for value in remediation]
        item.payload["remediation_thread_snapshots"] = [dict(value) for value in unresolved]
        item.payload["unresolved_threads_before_address"] = len(remediation)
        if item.payload.pop(_COMMENT_VALIDATION_ONLY, None):
            item.payload["review_audit"] = ReviewAudit(
                grade="A",
                summary="Reviewer validated the implementation responses to all open threads.",
                findings=(),
                raw_feedback="",
                valid=True,
            )
        return Continue(next_state=ADDRESS_WAIT if remediation else EVAL)

    def _post(  # noqa: C901
        self, item: WorkItem, ctx: StageContext
    ) -> StepResult:
        """Freeze reconciliation inputs and dispatch all GitHub I/O to a worker."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        validation_only = bool(item.payload.get(_COMMENT_VALIDATION_ONLY))
        audit = item.payload.get("review_audit")
        if not validation_only and (not isinstance(audit, ReviewAudit) or not audit.valid):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        if not is_full_commit_sha(reviewed_head):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)

        findings = (
            [] if validation_only else [dict(t) for t in item.payload.get("review_threads") or []]
        )
        item.payload["raw_review_threads"] = findings
        validated_fingerprints = item.payload.get("validation_receipt_fingerprints")
        if validated_fingerprints is not None and not isinstance(validated_fingerprints, dict):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        metadata_fingerprint = item.payload.get("validation_pr_metadata_fingerprint")
        if metadata_fingerprint is not None and not isinstance(metadata_fingerprint, str):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)

        resolved_ids: tuple[str, ...] = ()
        feedback: dict[str, str] = {}
        validation_result = item.payload.get("validation_result")
        # An empty bound fingerprint set proves the validator saw no exact
        # implementation reply receipts.  Its free-form output has no
        # reconciliation authority and must not suppress fresh audit work.
        if validation_result is not None and validated_fingerprints != {}:
            parsed = _parse_validation_result(validation_result)
            validation_is_bound = (
                validation_only
                or validated_fingerprints is not None
                or (metadata_fingerprint is not None)
            )
            if parsed is None or set(parsed) != {"resolved", "unaddressed"}:
                if validation_is_bound:
                    item.payload["review_audit_failure"] = True
                    return Continue(next_state=EVAL)
            else:
                raw_resolved = parsed.get("resolved")
                raw_unaddressed = parsed.get("unaddressed")
                if not isinstance(raw_resolved, list) or not isinstance(raw_unaddressed, list):
                    item.payload["review_audit_failure"] = True
                    return Continue(next_state=EVAL)
                if not all(
                    isinstance(thread_id, str) and thread_id.strip() for thread_id in raw_resolved
                ):
                    item.payload["review_audit_failure"] = True
                    return Continue(next_state=EVAL)
                resolved_ids = tuple(sorted(thread_id.strip() for thread_id in raw_resolved))
                for entry in raw_unaddressed:
                    if not isinstance(entry, dict):
                        item.payload["review_audit_failure"] = True
                        return Continue(next_state=EVAL)
                    thread_id = str(entry.get("thread_id") or entry.get("id") or "").strip()
                    detail = str(entry.get("detail") or "").strip()
                    if not thread_id or not detail or thread_id in feedback:
                        item.payload["review_audit_failure"] = True
                        return Continue(next_state=EVAL)
                    feedback[thread_id] = detail
        request = ReconcilePrReviewRequest(
            issue_number=item.issue,
            pr_number=item.pr,
            reviewed_head_sha=reviewed_head,
            validated_receipt_fingerprints=(
                FrozenJson.snapshot(validated_fingerprints)
                if validated_fingerprints is not None
                else None
            ),
            validated_metadata_fingerprint=metadata_fingerprint,
            resolved_thread_ids=resolved_ids,
            feedback=FrozenJson.snapshot(feedback),
            findings=FrozenJson.snapshot(findings),
            review_diff=str(item.payload.get("pr_diff") or ""),
        )
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        if pending is None:
            item.payload[_PENDING_GITHUB_REQUEST] = request
        elif pending != request:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)).resolve(),
                request=request,
                descr="reconcile_pr_review",
            ),
            on_done_state=POST_APPLY,
        )

    def _address(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Record a NO-GO and hand immutable review findings to the writer.

        This stage owns review evidence only.  It never dispatches a writer
        agent, rebases, commits, or pushes: implementation receives the full
        unresolved-thread snapshot after the detached reviewer checkout has
        been removed.
        """
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        # Review worktrees are immutable evidence. The implementation stage
        # owns the branch writer, its fix commit, and the subsequent
        # [Response] replies; pr_review only records the negative state and
        # hands the complete host thread snapshot back to that writer.
        no_go_outcome = self._write_no_go(item, ctx)
        if isinstance(no_go_outcome, StageOutcome):
            return self._cleanup_review_worktree_then(item, no_go_outcome)
        return self._cleanup_review_worktree_then(
            item,
            self._fail_back_implementation_remediation(item),
        )

    @staticmethod
    def _restart_direct_pr_review(item: WorkItem) -> StageOutcome | None:
        """Preserve a drifted checkout and route the PR through a fresh review."""
        reason = restart_direct_pr_review(item)
        return StageOutcome(Disposition.FINISH_FAIL, reason) if reason is not None else None

    def _recovery_reply_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dispatch one exact recovery-only reply handoff to a worker."""
        if item.issue is None or item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        handoff = item.payload.get(_PENDING_IMPLEMENTATION_REPLY_HANDOFF)
        retries = item.payload.get(
            _REPLY_VISIBILITY_RETRIES,
            0,
        )
        if (
            not isinstance(handoff, dict)
            or isinstance(retries, bool)
            or not isinstance(retries, int)
            or retries < 0
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        try:
            request = DeliverReplyHandoffRequest(
                issue_number=item.issue,
                pr_number=item.pr,
                handoff=FrozenJson.snapshot(handoff),
                visibility_retries=retries,
            )
        except (TypeError, ValueError):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        if pending is None:
            item.payload[_PENDING_GITHUB_REQUEST] = request
        elif pending != request:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)).resolve(),
                request=request,
                descr="recover_implementation_reply_handoff",
            ),
            on_done_state=EVAL,
        )

    @staticmethod
    def _on_reply_handoff_done(item: WorkItem, result: JobResult) -> None:
        """Store only an exact request-bearing recovery receipt."""
        if not result.ok:
            item.payload[_REPLY_HANDOFF_RECEIPT_ERROR] = "retry"
            return
        receipt = result.value
        if not isinstance(receipt, ReplyHandoffAttempted) or receipt.request != item.payload.get(
            _PENDING_GITHUB_REQUEST
        ):
            item.payload[_REPLY_HANDOFF_RECEIPT_ERROR] = "invalid"
            return
        item.payload[_REPLY_HANDOFF_RECEIPT] = receipt

    @staticmethod
    def _consume_reply_handoff_receipt(item: WorkItem) -> str:
        """Apply a correlated immutable recovery receipt to local payload."""
        return consume_reply_handoff_receipt(item, _PENDING_GITHUB_REQUEST)
