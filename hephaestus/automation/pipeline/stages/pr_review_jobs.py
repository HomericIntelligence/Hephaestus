# This mixin consumes the stage thread namespace by design.
# ruff: noqa: F403, F405
import json
from pathlib import Path
from typing import cast

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.automation.prompts.pr_review import (
    build_bounded_pr_review_analysis_prompt,
    build_bounded_review_anchor_correction_prompt,
    build_bounded_review_validation_prompt,
)
from hephaestus.automation.review_anchors import (
    ReviewAnchorCorrection as ReviewAnchorCorrection,
    ReviewAnchorCorrectionReason,
    normalize_review_finding_records,
    validate_comments_to_diff as _validate_comments_to_diff,
)
from hephaestus.automation.review_audit import parse_review_anchor_correction_response
from hephaestus.automation.review_finding_history import (
    compact_terminal_review_finding_collection,
    empty_review_finding_compacted_outcomes,
    normalize_review_finding_compacted_outcomes,
)

from ..coordinator_sessions import agent_session_lifecycle
from ..diagnostics import redact_diagnostic_text
from ..github_jobs import (
    FrozenJson,
    GitHubJob,
    ReconcilePrReviewRequest,
)
from ..summary import record_review_run
from .base import _reviewed_terminal_pr_outcome, source_workspace_binding, stage_timeout
from .pr_review_diagnostics import publish_host_verification_failure
from .pr_review_findings import (
    _build_review_finding_records as _build_review_finding_records,
    empty_diff_outcome as empty_diff_outcome,
)
from .pr_review_gate import PrReviewGate
from .pr_review_history import (
    _carry_review_finding_records as _carry_review_finding_records,
    _review_finding_history_has_capacity as _review_finding_history_has_capacity,
)
from .pr_review_receipts import store_host_verification_result
from .pr_review_recovery import (
    _PENDING_FINDING_RECOVERY as _PENDING_FINDING_RECOVERY,
    _PENDING_GITHUB_REQUEST as _PENDING_GITHUB_REQUEST,
    PrReviewRecoveryMixin,
)
from .pr_review_repository_validation import PrReviewRepositoryValidationMixin
from .pr_review_scope_expansion import PrReviewScopeExpansionMixin
from .pr_review_threads import *
from .pr_review_threads import POST_APPLY
from .pr_review_verification import (
    _repository_validation_prompt_json,
)

_ANCHOR_CORRECTION_JOB_PENDING = "review_anchor_correction_job_pending"
_ANCHOR_CORRECTION_RESULT = "review_anchor_correction_result"


class PrReviewJobs(
    PrReviewRepositoryValidationMixin, PrReviewScopeExpansionMixin, PrReviewRecoveryMixin
):
    """Own review worktrees, validation jobs, and result handoffs."""

    @staticmethod
    def _fail_back_implementation_remediation(item: WorkItem) -> StageOutcome:
        """Route unresolved review work back to implementation."""
        item.payload["implementation_remediation"] = True
        return StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Hydrate review inputs, require an unarmed PR, and reset the review round.

        The counter resets once per implementation pass.
        """
        for key in (
            "repository_validation_source_request",
            "repository_validation_source_result",
            "repository_validation_runtime_request",
            "repository_validation_runtime_result",
            "repository_validation_attempt",
            "repository_validation_ci_request",
            "repository_validation_local_request",
            "repository_validation_failure",
            "host_verification_workspace",
            "host_capability_request",
            "host_capability_result",
            "host_capability_failure",
            "host_capability_verification",
        ):
            item.payload.pop(key, None)
        if item.pr is not None:
            item.payload.pop("reviewed_pr_head_sha", None)
            item.payload.pop("reviewed_pr_node_id", None)
            arm_outcome = self._require_confirmed_unarmed(item.pr, ctx)
            if arm_outcome is not None:
                return arm_outcome
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
        entry = PrReviewJobs._read_existing_thread_entry(item, ctx)
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
            item.payload.pop("reviewed_pr_node_id", None)
            return None

        item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
        item.payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        item.payload["remediation_threads"] = remediation_threads
        item.payload["remediation_thread_snapshots"] = [dict(thread) for thread in live_threads]
        item.payload["unresolved_threads_before_address"] = len(remediation_threads)
        no_go_outcome = PrReviewGate._write_no_go(item, ctx)
        if no_go_outcome is not None:
            if isinstance(no_go_outcome, StageOutcome):
                return no_go_outcome
            return StageOutcome(Disposition.FINISH_FAIL, "reviewed_head_drift")
        return PrReviewJobs._fail_back_implementation_remediation(item)

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
        bind_outcome = PrReviewGate._bind_current_head_for_negative(item, ctx)
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
            item.payload.pop("reviewed_pr_node_id", None)
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
            expected_repository=f"{ctx.org}/{item.repo}",
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
        # Clear round-scoped state. A later failed round must not replay
        # an earlier verdict, thread set, or address output.
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

    def _submit_review_job(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Create the agent job after the checkout/head barrier succeeds."""
        pending_recovery = self._prepare_pending_finding_recovery(item, ctx)
        if pending_recovery is not None:
            return pending_recovery
        issue = _issue_number(item)
        try:
            has_capacity = _review_finding_history_has_capacity(item)
        except ValueError:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_finding_history_invalid"),
            )
        if not has_capacity:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_finding_history_full"),
            )
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
                "repository_validation_json": _repository_validation_prompt_json(
                    item, ctx.config.org
                ),
                "host_verifications_json": json.dumps(
                    item.payload.get("host_verification_receipts", []), sort_keys=True
                ),
                "host_verification_bootstrap_json": item.payload.get(
                    "host_verification_bootstrap_json", ""
                ),
                "anchor_corrections_json": json.dumps(
                    item.payload.get("review_anchor_corrections", []),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "include_nitpicks": ctx.config.nitpick,
                "review_context_kind": _review_context_kind(item),
                "reviewer_provider": reviewer_agent,
            },
            parse=_parse_review_response,  # structural audit parsed in-worker
            descr="review",
        )
        item.payload["review_job_pending"] = True
        return JobRequest(job, on_done_state=VALIDATE_WAIT)

    def _route_threads_before_broad_review(  # noqa: C901
        self, item: WorkItem, ctx: StageContext
    ) -> StepResult:
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
        pending_recovery = self._prepare_pending_finding_recovery(item, ctx)
        if pending_recovery is not None:
            return pending_recovery
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
        if item.payload.pop("scope_dependency_force_fresh_review", False):
            empty_diff = empty_diff_outcome(item)
            if empty_diff:
                return self._cleanup_review_worktree_then(item, empty_diff)
            item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
            return self._submit_review_job(item, ctx)
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
        if item.payload.get("explicit_pr_review"):
            empty_diff = empty_diff_outcome(item)
            if empty_diff:
                return self._cleanup_review_worktree_then(item, empty_diff)
            item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
            return self._submit_review_job(item, ctx)
        if all(bool(snapshot.get("implementation_reply_submitted")) for snapshot in snapshots):
            item.payload[_COMMENT_VALIDATION_ONLY] = True
            return Continue(next_state=VALIDATE_WAIT)
        item.payload.pop(_COMMENT_VALIDATION_ONLY, None)
        item.payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        item.payload["remediation_threads"] = remediation_threads
        item.payload["remediation_thread_snapshots"] = [dict(thread) for thread in live_threads]
        item.payload["unresolved_threads_before_address"] = len(remediation_threads)
        return self._handoff_implementation(item, ctx)

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
        if not item.payload.get(_COMMENT_VALIDATION_ONLY) and not item.payload.get(
            "review_anchor_correction_complete"
        ):
            correction = self._prepare_anchor_correction(item, ctx)
            if correction is not None:
                return correction
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
            if terminal := _reviewed_terminal_pr_outcome(item, ctx):
                return terminal
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
                "pr_title": pr_title,
                "pr_description": pr_description,
                "repository_validation_json": _repository_validation_prompt_json(
                    item, ctx.config.org
                ),
                "host_verifications_json": json.dumps(
                    item.payload.get("host_verification_receipts", []), sort_keys=True
                ),
                "review_context_kind": _review_context_kind(item),
            },
            descr="validate",
        )
        return JobRequest(job, on_done_state=POST)

    def _prepare_anchor_correction(self, item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Partition all findings and submit one correction job when necessary."""
        audit = item.payload.get("review_audit")
        if not isinstance(audit, ReviewAudit) or not audit.valid:
            return None
        try:
            validation = _validate_comments_to_diff(
                [dict(finding) for finding in audit.findings],
                str(item.payload.get("pr_diff") or ""),
            )
        except (TypeError, ValueError):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        valid = [dict(finding) for finding in validation.valid]
        blocking = [
            finding
            for finding in valid
            if str(finding.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES
        ]
        advisory = [finding for finding in valid if finding not in blocking]
        item.payload["review_valid_findings"] = valid
        item.payload["review_threads"] = blocking
        item.payload["review_advisory_findings"] = advisory
        if not validation.corrections:
            try:
                current_records = _build_review_finding_records(
                    source_head=str(item.payload.get("reviewed_pr_head_sha") or ""),
                    initial_valid=valid,
                    corrections=[],
                    corrected_inline=[],
                    corrected_audit=[],
                    not_publishable=[],
                )
                item.payload["review_finding_records"] = _carry_review_finding_records(
                    item, current_records
                )
                item.payload["carried_review_finding_records"] = [
                    dict(record) for record in item.payload["review_finding_records"]
                ]
                item.payload["carried_review_finding_compacted_outcomes"] = dict(
                    item.payload["review_finding_compacted_outcomes"]
                )
            except ValueError:
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
            item.payload["review_anchor_correction_complete"] = True
            return None
        records = [
            {
                "finding": dict(correction.finding),
                "finding_id": correction.finding_id,
                "path": correction.path,
                "line": correction.line,
                "side": correction.side,
                "reason": correction.reason,
            }
            for correction in validation.corrections
        ]
        item.payload["review_anchor_corrections"] = records
        workspace = source_workspace_binding(
            item,
            ctx,
            SourceLane.REVIEW,
            revision=str(item.payload.get("reviewed_pr_head_sha") or ""),
        )
        reviewer_agent = agent_provider(ctx, "reviewer")
        item.payload[_ANCHOR_CORRECTION_JOB_PENDING] = True
        return JobRequest(
            AgentJob(
                repo=item.repo,
                issue=_issue_number(item),
                agent=reviewer_agent,
                model=stage_model(ctx, "reviewer", reviewer_model),
                prompt_builder=build_bounded_review_anchor_correction_prompt,
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
                    "invalid_findings_json": json.dumps(
                        records, ensure_ascii=False, sort_keys=True
                    ),
                    "diff_text": item.payload.get("pr_diff", ""),
                    "review_context_kind": _review_context_kind(item),
                },
                descr="correct_review_anchors",
            ),
            on_done_state=ANCHOR_CORRECTION_WAIT,
        )

    def _anchor_correction_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Apply one correction response and never submit a second correction turn."""
        del ctx
        raw_records = item.payload.get("review_anchor_corrections")
        if not isinstance(raw_records, list):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        corrections: list[ReviewAnchorCorrection] = []
        try:
            for record in raw_records:
                if not isinstance(record, dict):
                    raise ValueError("invalid correction record")
                finding = record.get("finding")
                if not isinstance(finding, dict):
                    raise ValueError("invalid correction finding")
                corrections.append(
                    ReviewAnchorCorrection(
                        finding=dict(finding),
                        finding_id=str(record.get("finding_id") or ""),
                        path=str(record.get("path") or ""),
                        line=cast(int | None, record.get("line")),
                        side=str(record.get("side") or ""),
                        reason=cast(ReviewAnchorCorrectionReason, record.get("reason")),
                    )
                )
        except (TypeError, ValueError):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        response = item.payload.pop(_ANCHOR_CORRECTION_RESULT, None)
        parsed = (
            parse_review_anchor_correction_response(response, tuple(corrections))
            if response is not None
            else None
        )
        initial_valid = [dict(value) for value in item.payload.get("review_valid_findings", [])]
        valid = [
            finding
            for finding in initial_valid
            if str(finding.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES
        ]
        advisory: list[dict[str, object]] = [
            finding for finding in initial_valid if finding not in valid
        ]
        not_publishable: list[dict[str, object]] = []
        corrected: list[dict[str, object]] = []
        if parsed is None:
            not_publishable = [dict(correction.finding) for correction in corrections]
        else:
            corrected_validation = _validate_comments_to_diff(
                [dict(finding) for finding in parsed.inline_findings],
                str(item.payload.get("pr_diff") or ""),
                preserve_finding_ids=True,
            )
            valid.extend(dict(finding) for finding in corrected_validation.valid)
            invalid_ids = {correction.finding_id for correction in corrected_validation.corrections}
            not_publishable.extend(
                dict(finding)
                for finding in parsed.inline_findings
                if str(finding.get("finding_id") or "") in invalid_ids
            )
            # This review path has no durable non-inline publication surface.
            # Keep an audit selection as a bounded, not-publishable finding
            # until a supported surface exists.
            not_publishable.extend(dict(finding) for finding in parsed.audit_findings)
            not_publishable.extend(dict(finding) for finding in parsed.not_publishable_findings)
            corrected = [dict(finding) for finding in corrected_validation.valid]
        item.payload["review_threads"] = valid
        item.payload["review_advisory_findings"] = advisory
        item.payload["review_not_publishable_findings"] = not_publishable
        item.payload["review_corrected_findings"] = corrected
        try:
            current_records = _build_review_finding_records(
                source_head=str(item.payload.get("reviewed_pr_head_sha") or ""),
                initial_valid=initial_valid,
                corrections=corrections,
                corrected_inline=corrected,
                corrected_audit=[],
                not_publishable=not_publishable,
            )
            item.payload["review_finding_records"] = _carry_review_finding_records(
                item, current_records
            )
            item.payload["carried_review_finding_records"] = [
                dict(record) for record in item.payload["review_finding_records"]
            ]
            item.payload["carried_review_finding_compacted_outcomes"] = dict(
                item.payload["review_finding_compacted_outcomes"]
            )
        except ValueError:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["review_anchor_correction_attempted"] = True
        item.payload["review_anchor_correction_complete"] = True
        return Continue(next_state=VALIDATE_WAIT)

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
        """Separate a recoverable runner fault from a source validation failure."""
        receipts = item.payload.get("host_verification_receipts")
        receipt = (
            receipts[-1]
            if isinstance(receipts, list) and receipts and isinstance(receipts[-1], dict)
            else None
        )
        diagnostic: dict[str, object] = {
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
        failure_kind = receipt.get("failure_kind") if isinstance(receipt, dict) else None
        # A runner fault is not evidence of a source defect. Keep PR verdict
        # labels unchanged so a later run can retry the same reviewed head.
        if failure_kind == "runner" and (
            isinstance(receipt, dict)
            and receipt.get("head_sha") == diagnostic["head_sha"]
            and receipt.get("source_head_mismatch") is not True
        ):
            diagnostic["labels_unchanged"] = True
            pr_number = cast(int, item.pr)
            if not publish_host_verification_failure(
                ctx.github, pr_number, verification, diagnostic, logger
            ):
                return self._cleanup_review_worktree_then(
                    item,
                    StageOutcome(Disposition.BLOCKED, "host_verification_comment_failed"),
                )
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.BLOCKED, "host_verification_runner_blocked"),
            )
        no_go_outcome = PrReviewGate._write_no_go(item, ctx)
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
            return self._handoff_implementation(item, ctx)
        return self._cleanup_review_worktree_then(
            item,
            StageOutcome(Disposition.FINISH_FAIL, "host_verification_failed"),
        )

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
            expected_head = item.payload.get("review_worktree_expected_head")
            if not is_full_commit_sha(expected_head):
                return StageOutcome(
                    Disposition.FINISH_FAIL, "review_worktree_cleanup_identity_invalid"
                )
            job = GitJob(
                repo=item.repo,
                op="remove_worktree",
                timeout_s=stage_timeout(ctx, "metadata", GIT_JOB_TIMEOUT_S),
                expected_repository=f"{ctx.org}/{item.repo}",
                kwargs={
                    "worktree_path": review_worktree,
                    "repo_root": str(ctx.paths.repo_root),
                    "issue_number": item.issue or item.pr or 0,
                    "expected_head": expected_head,
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

    def on_job_done(  # noqa: C901
        self, item: WorkItem, result: JobResult, ctx: StageContext
    ) -> None:
        """Store one completed job result for the current review wait state."""
        if self._consume_host_capability_result(item, result, ctx):
            return
        if self._consume_repository_validation_preparation(item, result):
            return
        if self._consume_repository_validation_ci_result(item, result):
            return
        if self._consume_repository_validation_local_result(item, result):
            return
        if self._consume_scope_expansion_result(item, result):
            return
        if self._consume_review_worktree_cleanup_result(item, result):
            return
        if item.payload.get(_PENDING_FINDING_RECOVERY):
            self._on_reconciliation_done(item, result)
            return
        if item.payload.pop(_ANCHOR_CORRECTION_JOB_PENDING, None):
            item.payload[_ANCHOR_CORRECTION_RESULT] = result.value if result.ok else None
            return
        if item.state == POST:
            self._on_reconciliation_done(item, result)
            return
        if self._consume_direct_worktree_result(item, result):
            return
        if self._consume_review_checkout_result(item, result):
            return
        if self._consume_host_verification_result(item, result):
            self._store_host_verification_result(item, result)
            return
        if self._consume_scope_dependency_result(item, result):
            return
        review_job_pending = bool(item.payload.pop("review_job_pending", None))
        is_review_result = review_job_pending or item.state == REVIEW_WAIT
        if self._consume_failed_job(item, result, is_review_result):
            return
        if is_review_result and result.value is not None:
            valid_review_result = self._store_review_result(item, result.value)
            if (
                valid_review_result
                and review_job_pending
                and item.payload.get("explicit_pr_review")
            ):
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
        PrReviewJobs._on_direct_pr_worktree_done(item, result)
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
    def _consume_host_verification_result(item: WorkItem, result: JobResult) -> bool:
        """Claim one fixed host-check completion independent of mini-state."""
        del result
        return item.payload.pop(_HOST_VERIFICATION_PENDING, None) is not None

    _store_host_verification_result = staticmethod(store_host_verification_result)

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
    def _store_review_result(item: WorkItem, value: object) -> bool:
        """Persist one structural reviewer result and report if it is valid."""
        if isinstance(value, _ParsedReviewResponse):
            item.payload["review_audit"] = value.audit
            item.payload["review_feedback"] = value.audit.raw_feedback
            item.payload["review_threads"] = [dict(comment) for comment in value.audit.findings]
            return value.audit.valid
        if isinstance(value, ReviewAudit):
            item.payload["review_audit"] = value
            item.payload["review_feedback"] = value.raw_feedback
            item.payload["review_threads"] = [dict(comment) for comment in value.findings]
            return value.valid
        item.payload["review_audit_failure"] = True
        return False

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
        if isinstance(audit, ReviewAudit):
            scope_preparation = self._prepare_scope_expansion_before_post(item, ctx, audit)
            if scope_preparation is not None:
                return scope_preparation
        findings = (
            [] if validation_only else [dict(t) for t in item.payload.get("review_threads") or []]
        )
        try:
            finding_records = [
                dict(record)
                for record in normalize_review_finding_records(
                    item.payload.get("review_finding_records", [])
                )
            ]
            compacted_outcomes = normalize_review_finding_compacted_outcomes(
                item.payload.get(
                    "review_finding_compacted_outcomes",
                    empty_review_finding_compacted_outcomes(),
                ),
                retained_finding_ids=[record["finding_id"] for record in finding_records],
            )
            if validation_only and not finding_records and not compacted_outcomes["identities"]:
                finding_records = [
                    dict(record)
                    for record in normalize_review_finding_records(
                        item.payload.get("carried_review_finding_records", [])
                    )
                ]
                compacted_outcomes = normalize_review_finding_compacted_outcomes(
                    item.payload.get(
                        "carried_review_finding_compacted_outcomes",
                        empty_review_finding_compacted_outcomes(),
                    ),
                    retained_finding_ids=[record["finding_id"] for record in finding_records],
                )
        except ValueError:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        if validation_only and all(record["status"] != "pending" for record in finding_records):
            try:
                normalized_records, compacted_outcomes = compact_terminal_review_finding_collection(
                    finding_records,
                    compacted_outcomes,
                )
                finding_records = [dict(record) for record in normalized_records]
            except ValueError:
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
        if validation_only:
            item.payload["review_finding_records"] = finding_records
            item.payload["review_finding_compacted_outcomes"] = compacted_outcomes
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
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        deadline_s = (
            pending.deadline_s
            if isinstance(pending, ReconcilePrReviewRequest)
            else operation_deadline_after(stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S))
        )
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
            finding_records=FrozenJson.snapshot(finding_records),
            review_diff=str(item.payload.get("pr_diff") or ""),
            deadline_s=deadline_s,
            compacted_outcomes=FrozenJson.snapshot(compacted_outcomes),
        )
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

    def _handoff_implementation(self, item: WorkItem, ctx: StageContext) -> StepResult:
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
