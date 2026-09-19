"""Own repository-validation preparation and evidence handoffs for PR review."""

from __future__ import annotations

import secrets
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.automation.pipeline_github_review_validation import (
    comet_ci_check_ids,
    comet_local_check_ids,
)
from hephaestus.automation.source_worktree import SourceWorkspaceError

from ..github_jobs import GitHubJob, ReadRepositoryValidationCIRequest, RepositoryValidationCIRead
from ..host_capabilities import CapabilityRequestTarget, HostCapabilityRead, HostCapabilityReceipt
from ..jobs import BuildTestJob, GitJob, HostCapabilityJob, JobResult
from ..repository_validation import (
    RepositoryValidationAttempt,
    RepositoryValidationExecution,
    RepositoryValidationGap,
    RepositoryValidationInvocation,
    RepositoryValidationLocalRead,
    begin_validation_request,
    consume_validation_result,
)
from ..repository_validation_preparation import (
    RepositoryValidationRuntimeRead,
    RepositoryValidationRuntimeRequest,
    RepositoryValidationSourceRead,
    RepositoryValidationSourceRequest,
)
from .base import (
    Disposition,
    JobRequest,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    source_workspace_binding,
    stage_timeout,
)
from .pr_review_diagnostics import publish_host_verification_failure
from .pr_review_repository_validation_state import _repository_validation_coverage
from .pr_review_round_state import _clear_host_execution_state
from .pr_review_threads import (
    _HOST_VERIFICATION_PENDING,
    GIT_JOB_TIMEOUT_S,
    HOST_CAPABILITY_WAIT,
    HOST_VERIFICATION_TIMEOUT_S,
    HOST_VERIFICATION_WAIT,
    REPOSITORY_VALIDATION_CI_WAIT,
    REPOSITORY_VALIDATION_RUNTIME_WAIT,
    REPOSITORY_VALIDATION_SOURCE_WAIT,
    _issue_number,
    _PrReviewHost,
    _worktree_path,
    logger,
)
from .pr_review_verification import (
    _HostVerificationSpec,
    _review_change_records,
    _review_changed_paths,
)
from .repo import is_full_commit_sha


def _store_review_source_manifest(
    item: WorkItem, value: dict[str, object], paths: tuple[str, ...]
) -> bool:
    """Retain status records only when both base identities agree."""
    records = _review_change_records(value.get("change_records"), paths)
    diff_base = value.get("diff_base_sha")
    target_base = value.get("target_base_sha")
    head = value.get("head")
    if (
        records is None
        or not is_full_commit_sha(head)
        or head != item.payload.get("review_checkout_expected_head")
        or not is_full_commit_sha(diff_base)
        or diff_base != value.get("base")
        or not is_full_commit_sha(target_base)
        or target_base != item.payload.get("pr_base_sha")
    ):
        return False
    if item.repo.casefold() == "comet":
        try:
            for _, path in records:
                path.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return False
    item.payload["review_change_records"] = records
    item.payload["review_diff_base_sha"] = diff_base
    item.payload["review_target_base_sha"] = target_base
    return True


def _check_repository_preparation_current(
    item: WorkItem, value: RepositoryValidationSourceRead | RepositoryValidationRuntimeRead
) -> None:
    """Require the same live review identity at callback and job submission."""
    if type(value) not in {RepositoryValidationSourceRead, RepositoryValidationRuntimeRead}:
        raise ValueError("The preparation result type is invalid.")
    if replace(value) != value:
        raise ValueError("The preparation result changed.")
    request = value.request
    if isinstance(request, RepositoryValidationSourceRequest):
        repository, issue, pr = request.repository, request.issue_number, request.pr_number
        workspace, head, base = request.workspace, request.reviewed_head, request.reviewed_base
        generation = request.generation
        paths = _review_changed_paths(item.payload.get("review_changed_paths"))
        records = _review_change_records(item.payload.get("review_change_records"), paths or ())
        if (
            records != request.changes
            or item.payload.get("review_diff_base_sha") != request.diff_base_sha
            or item.payload.get("review_target_base_sha") != base
        ):
            raise ValueError("The source manifest changed before its callback.")
    else:
        invocation = request.invocation
        attempt = item.payload.get("repository_validation_attempt")
        if (
            type(attempt) is not RepositoryValidationAttempt
            or attempt.pending != invocation
            or attempt.plan != invocation.plan
            or attempt.generation != invocation.generation
            or attempt.request_nonce != invocation.request_nonce
        ):
            raise ValueError("The runtime invocation no longer belongs to the attempt.")
        plan = invocation.plan
        repository, issue, pr = plan.repository, plan.issue_number, plan.pr_number
        workspace, head, base = plan.source_workspace, plan.reviewed_head, plan.reviewed_base
        generation = invocation.generation
    if (
        repository.casefold() != f"llm360/{item.repo.casefold()}"
        or issue != item.issue
        or pr != item.pr
        or str(workspace.cwd) != item.worktree
        or head != item.payload.get("reviewed_pr_head_sha")
        or head != item.payload.get("pr_head_sha")
        or base != item.payload.get("pr_base_sha")
        or generation != item.payload.get("reviewed_pr_proof_generation")
        or item.payload.get("repository_validation_failure")
    ):
        raise ValueError("The live review identity changed.")


class PrReviewRepositoryValidationMixin(_PrReviewHost):
    """Keep worker preparation separate from validation evidence."""

    def _host_capability_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Release only the fixed execution owned by a valid capability result."""
        request = item.payload.get("host_capability_request")
        value = item.payload.get("host_capability_result")
        verification = item.payload.get("host_capability_verification")
        try:
            if type(request) is not CapabilityRequestTarget:
                raise ValueError("The capability request is missing.")
            self._check_capability_request_current(request, item, ctx)
            if type(value) is not HostCapabilityRead or value.request != request:
                raise ValueError("The capability result is missing or unowned.")
            replace(value)
            if (
                item.payload.get("host_capability_failure")
                or value.failure
                or value.receipt is None
                or not value.receipt.available
                or type(verification) is not _HostVerificationSpec
            ):
                raise ValueError("The required capability is unavailable.")
        except (AttributeError, TypeError, ValueError):
            return self._block_host_capability(item, ctx)
        for key in (
            "host_capability_request",
            "host_capability_result",
            "host_capability_failure",
            "host_capability_verification",
        ):
            item.payload.pop(key, None)
        item.payload[_HOST_VERIFICATION_PENDING] = verification.descr
        return JobRequest(
            BuildTestJob(
                item.repo,
                request.checkout_path,
                verification.argv,
                HOST_VERIFICATION_TIMEOUT_S,
                expected_head_sha=request.expected_head_sha,
                immutable_source=True,
                descr=verification.descr,
            ),
            on_done_state=HOST_VERIFICATION_WAIT,
        )

    @staticmethod
    def _block_host_capability(item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Report a recoverable runner gap without changing any source verdict."""
        value = item.payload.get("host_capability_result")
        receipt = value.receipt if type(value) is HostCapabilityRead else None
        if type(receipt) is not HostCapabilityReceipt:
            receipt = None
        if receipt is not None:
            try:
                replace(receipt)
            except (AttributeError, TypeError, ValueError):
                receipt = None
        verification = item.payload.get("host_capability_verification")
        if type(verification) is not _HostVerificationSpec:
            verification = None
        details = asdict(receipt) if receipt is not None else {}
        diagnostic = {
            **details,
            "head_sha": str(item.payload.get("reviewed_pr_head_sha") or ""),
            "failure_kind": "runner",
            "capability_failure": True,
            "labels_unchanged": True,
            "argv": list(verification.argv) if verification is not None else [],
            "error": (value.failure if type(value) is HostCapabilityRead else "")
            or details.get("token")
            or str(
                item.payload.get("host_capability_failure")
                or "host_capability_evidence_unavailable"
            ),
            "failed_step": details.get("failed_step", "source"),
        }
        item.payload["host_verification_failure"] = diagnostic
        published = item.pr is not None and publish_host_verification_failure(
            ctx.github,
            item.pr,
            verification,
            diagnostic,
            logger,
        )
        return StageOutcome(
            Disposition.BLOCKED,
            "host_capability_blocked" if published else "host_capability_comment_failed",
        )

    @staticmethod
    def _check_capability_request_current(
        request: CapabilityRequestTarget, item: WorkItem, ctx: StageContext
    ) -> None:
        """Require the source and attempt that still own this callback."""
        replace(request)
        generation = item.payload.get("reviewed_pr_proof_generation")
        if (
            request.repository.casefold() != f"{ctx.org}/{item.repo}".casefold()
            or request.issue_number != item.issue
            or request.pr_number != item.pr
            or request.workspace != item.payload.get("host_verification_workspace")
            or request.expected_head_sha != item.payload.get("reviewed_pr_head_sha")
            or type(generation) is not int
            or request.generation != generation
        ):
            raise ValueError("The capability request no longer owns the current review.")

    def _consume_host_capability_result(
        self, item: WorkItem, result: JobResult, ctx: StageContext
    ) -> bool:
        """Keep stale or invalid capability results out of generic review handling."""
        pending = item.payload.get("host_capability_request")
        value = result.value
        if type(value) is not HostCapabilityRead:
            if pending is None:
                return False
            item.payload["host_capability_failure"] = "capability_callback_invalid"
            return True
        if type(pending) is not CapabilityRequestTarget or value.request != pending:
            return True
        if item.payload.get("host_capability_result") is not None:
            return True
        try:
            self._check_capability_request_current(pending, item, ctx)
        except (AttributeError, TypeError, ValueError):
            return True
        try:
            replace(value)
            if result.interrupted and value.failure != "operation_cancelled":
                raise ValueError("The capability operation was interrupted.")
            expected_ok = (
                value.receipt is not None and value.receipt.available and not value.failure
            )
            if result.ok is not expected_ok:
                raise ValueError("The capability outcome is contradictory.")
        except (AttributeError, TypeError, ValueError):
            item.payload["host_capability_failure"] = "capability_callback_invalid"
            return True
        item.payload["host_capability_result"] = value
        return True

    @staticmethod
    def _submit_host_verification(
        item: WorkItem, ctx: StageContext, verification: _HostVerificationSpec
    ) -> JobRequest:
        """Record capability ownership before the worker can inspect its source."""
        workspace = item.payload.get("host_verification_workspace")
        if type(workspace) is not WorkspaceBinding or workspace.reusable_root is None:
            raise ValueError("The host verification source binding is missing.")
        if item.payload.get("host_capability_request") is not None:
            raise ValueError("A capability request is already pending.")
        request = CapabilityRequestTarget(
            repository=f"{ctx.org}/{item.repo}",
            issue_number=_issue_number(item),
            pr_number=cast(int, item.pr),
            repository_root=workspace.reusable_root,
            checkout_path=_worktree_path(item, ctx),
            expected_head_sha=str(item.payload.get("reviewed_pr_head_sha") or ""),
            phase="pr_review",
            purpose="scratch",
            request_id=secrets.token_hex(16),
            workspace=workspace,
            generation=item.payload["reviewed_pr_proof_generation"],
        )
        job = HostCapabilityJob(
            request.repository,
            request,
            HOST_VERIFICATION_TIMEOUT_S,
            deadline_s=operation_deadline_after(HOST_VERIFICATION_TIMEOUT_S),
        )
        item.payload["host_capability_request"] = request
        item.payload["host_capability_verification"] = verification
        return JobRequest(job, on_done_state=HOST_CAPABILITY_WAIT)

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
            # A review is one immutable snapshot. Do not mutate the PR branch
            # or re-fetch it; the next item takes a fresh detached snapshot.
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(Disposition.FINISH_FAIL, "review_checkout_head_drift"),
            )
        normalized_paths = _review_changed_paths(item.payload.get("review_changed_paths"))
        if normalized_paths is None:
            return self._cleanup_review_worktree_then(
                item,
                StageOutcome(
                    Disposition.FINISH_FAIL,
                    "review_checkout_path_manifest_invalid",
                ),
            )
        item.payload["review_changed_paths"] = list(normalized_paths)
        item.payload["reviewed_pr_head_sha"] = expected_head
        item.payload["reviewed_pr_node_id"] = item.payload.get("pr_node_id")
        prior_generation = item.payload.get("reviewed_pr_proof_generation", 0)
        if isinstance(prior_generation, bool) or not isinstance(prior_generation, int):
            prior_generation = 0
        item.payload["reviewed_pr_proof_generation"] = prior_generation + 1
        try:
            workspace = source_workspace_binding(
                item, ctx, SourceLane.REVIEW, revision=expected_head
            )
        except (RuntimeError, SourceWorkspaceError):
            return StageOutcome(Disposition.FINISH_FAIL, "review_source_binding_failed")
        if ctx.org.casefold() == "llm360" and item.repo.casefold() == "comet":
            return self._start_repository_validation(item, ctx, workspace)
        _clear_host_execution_state(item)
        return self._route_threads_before_broad_review(item, ctx)

    def _start_repository_validation(
        self, item: WorkItem, ctx: StageContext, workspace: WorkspaceBinding
    ) -> StepResult:
        """Submit source inspection without source I/O on the coordinator."""
        if item.payload.get("repository_validation_source_request") is not None:
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_source_pending")
        timeout = min(120, stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S))
        try:
            paths = _review_changed_paths(item.payload.get("review_changed_paths"))
            records = _review_change_records(item.payload.get("review_change_records"), paths or ())
            if records is None:
                raise ValueError("The checkout manifest is invalid.")
            request = RepositoryValidationSourceRequest(
                "LLM360/comet",
                item.issue,
                cast(int, item.pr),
                workspace,
                str(item.payload.get("reviewed_pr_head_sha") or ""),
                str(item.payload.get("pr_base_sha") or ""),
                str(item.payload.get("review_diff_base_sha") or ""),
                records,
                item.payload["reviewed_pr_proof_generation"],
                secrets.token_hex(16),
                operation_deadline_after(timeout),
            )
            if item.payload.get("review_target_base_sha") != request.reviewed_base:
                raise ValueError("The target base changed.")
            job = GitJob(
                item.repo,
                "prepare_repository_validation",
                timeout,
                expected_repository=request.repository,
                deadline_s=request.deadline_s,
                workspace=workspace,
                repository_validation_preparation=request,
                descr="review_repository_validation_source",
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            item.payload["repository_validation_failure"] = "validation_source_plan_invalid"
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_source_gap")
        item.payload["repository_validation_source_request"] = request
        return JobRequest(job, on_done_state=REPOSITORY_VALIDATION_SOURCE_WAIT)

    def _repository_validation_source_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Create the evidence attempt only after owned source preparation."""
        value = item.payload.pop("repository_validation_source_result", None)
        try:
            _check_repository_preparation_current(item, value)
        except (AttributeError, KeyError, TypeError, ValueError):
            item.payload["repository_validation_failure"] = "validation_preparation_invalid"
        if (
            type(value) is not RepositoryValidationSourceRead
            or value.plan is None
            or item.payload.get("repository_validation_failure")
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_source_gap")
        plan = value.plan
        attempt = RepositoryValidationAttempt(
            plan, value.request.generation, value.request.request_nonce
        )
        item.payload["repository_validation_attempt"] = attempt
        item.payload["reviewed_pr_base_sha"] = plan.reviewed_base
        if not plan.execution_allowed:
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_profile_gap")
        checks = comet_ci_check_ids(plan.checks)
        if not checks:
            return self._repository_validation_ci_wait(item, ctx)
        attempt, invocation = begin_validation_request(attempt, "ci", checks)
        request = ReadRepositoryValidationCIRequest(
            invocation,
            str(item.payload.get("pr_head_branch") or ""),
            operation_deadline_after(min(120, stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S))),
        )
        item.payload["repository_validation_attempt"] = attempt
        item.payload["repository_validation_ci_request"] = request
        return JobRequest(
            GitHubJob(
                item.repo, Path(ctx.paths.repo_root), request, "review_repository_validation_ci"
            ),
            on_done_state=REPOSITORY_VALIDATION_CI_WAIT,
        )

    def _repository_validation_ci_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Complete coverage or submit one uncovered locally eligible check."""
        coverage = _repository_validation_coverage(item)
        if coverage.status == "complete":
            return self._route_threads_before_broad_review(item, ctx)
        if any(gap.reason != "validation_check_uncovered" for gap in coverage.gaps):
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_incomplete")
        attempt = item.payload.get("repository_validation_attempt")
        if type(attempt) is not RepositoryValidationAttempt or not coverage.uncovered_check_ids:
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_incomplete")
        eligible = comet_local_check_ids(attempt.plan.checks)
        if not set(coverage.uncovered_check_ids) <= set(eligible):
            item.payload["repository_validation_failure"] = "local_check_ineligible"
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_local_unavailable")
        return self._submit_repository_validation_local(
            item, ctx, attempt, coverage.uncovered_check_ids[0]
        )

    @staticmethod
    def _submit_repository_validation_local(
        item: WorkItem, ctx: StageContext, attempt: RepositoryValidationAttempt, check_id: str
    ) -> StepResult:
        """Reserve one invocation before worker runtime admission."""
        if (
            attempt.pending is not None
            or item.payload.get("repository_validation_runtime_request") is not None
            or item.payload.get("repository_validation_local_request") is not None
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_local_pending")
        plan = attempt.plan
        check = next(check for check in plan.checks if check.check_id == check_id)
        timeout = min(120, stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S))
        attempt, invocation = begin_validation_request(attempt, "local", (check_id,))
        request = RepositoryValidationRuntimeRequest(invocation, operation_deadline_after(timeout))
        job = BuildTestJob(
            repo=item.repo,
            cwd=plan.source_workspace.cwd,
            argv=check.argv,
            timeout_s=timeout,
            expected_head_sha=plan.reviewed_head,
            immutable_source=True,
            descr="review_repository_validation_runtime",
            repository_validation_preparation=request,
        )
        item.payload["repository_validation_attempt"] = attempt
        item.payload["repository_validation_runtime_request"] = request
        return JobRequest(job, on_done_state=REPOSITORY_VALIDATION_RUNTIME_WAIT)

    @staticmethod
    def _repository_validation_runtime_wait(item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit execution only after the worker returns owned runtime metadata."""
        del ctx
        value = item.payload.pop("repository_validation_runtime_result", None)
        try:
            _check_repository_preparation_current(item, value)
        except (AttributeError, KeyError, TypeError, ValueError):
            item.payload["repository_validation_failure"] = "validation_preparation_invalid"
        if (
            type(value) is not RepositoryValidationRuntimeRead
            or value.execution is None
            or item.payload.get("repository_validation_failure")
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "repository_validation_local_unavailable")
        execution = value.execution
        check = next(
            check for check in execution.plan.checks if check.check_id == execution.check_id
        )
        item.payload["repository_validation_local_request"] = execution
        return JobRequest(
            BuildTestJob(
                repo=item.repo,
                cwd=execution.plan.source_workspace.cwd,
                argv=check.argv,
                timeout_s=HOST_VERIFICATION_TIMEOUT_S,
                expected_head_sha=execution.plan.reviewed_head,
                immutable_source=True,
                descr="review_repository_validation_local",
                repository_validation=execution,
            ),
            on_done_state=REPOSITORY_VALIDATION_CI_WAIT,
        )

    @staticmethod
    def _consume_repository_validation_preparation(item: WorkItem, result: JobResult) -> bool:
        """Reject unowned preparation before any generic callback can use it."""
        value = result.value
        source_key = "repository_validation_source_request"
        runtime_key = "repository_validation_runtime_request"
        result_type: type[RepositoryValidationSourceRead] | type[RepositoryValidationRuntimeRead]
        if isinstance(value, RepositoryValidationSourceRead):
            key, result_type = source_key, RepositoryValidationSourceRead
        elif isinstance(value, RepositoryValidationRuntimeRead):
            key, result_type = runtime_key, RepositoryValidationRuntimeRead
        elif item.payload.get(source_key) is not None:
            key, result_type = source_key, RepositoryValidationSourceRead
        elif item.payload.get(runtime_key) is not None:
            key, result_type = runtime_key, RepositoryValidationRuntimeRead
        else:
            return False
        pending = item.payload.get(key)
        try:
            if (
                pending is None
                or type(value) is not result_type
                or replace(value) != value
                or value.request != pending
            ):
                raise ValueError("The preparation callback has no current success authority.")
            _check_repository_preparation_current(item, value)
            if type(value) is RepositoryValidationRuntimeRead and value.failure and not result.ok:
                attempt = item.payload["repository_validation_attempt"]
                invocation = value.request.invocation
                item.payload["repository_validation_attempt"] = consume_validation_result(
                    attempt,
                    invocation,
                    (),
                    (
                        RepositoryValidationGap(
                            invocation.check_ids[0], "local_runtime_unavailable"
                        ),
                    ),
                )
                item.payload.pop(key, None)
                item.payload["repository_validation_failure"] = "local_runtime_unavailable"
                return True
            if not result.ok or result.interrupted or value.failure:
                raise ValueError("The preparation job did not succeed.")
        except (AttributeError, KeyError, TypeError, ValueError):
            item.payload["repository_validation_failure"] = "validation_preparation_invalid"
            return True
        item.payload.pop(key, None)
        item.payload[key.replace("_request", "_result")] = value
        return True

    @staticmethod
    def _consume_repository_validation_local_result(item: WorkItem, result: JobResult) -> bool:
        """Consume local evidence before state assignment and retain terminal gaps."""
        pending = item.payload.get("repository_validation_local_request")
        value = result.value
        if pending is None and not isinstance(value, RepositoryValidationLocalRead):
            return False
        attempt = item.payload.get("repository_validation_attempt")
        if type(attempt) is not RepositoryValidationAttempt:
            item.payload["repository_validation_failure"] = "validation_attempt_missing"
            return True
        try:
            if type(value) is not RepositoryValidationLocalRead or replace(value) != value:
                raise ValueError("The local callback is invalid.")
            if pending is not None and (
                type(pending) is not RepositoryValidationExecution or value.execution != pending
            ):
                raise ValueError("The local callback does not match the pending request.")
            execution = value.execution
            invocation = RepositoryValidationInvocation(
                execution.plan,
                execution.attempt_generation,
                execution.request_nonce,
                "local",
                (execution.check_id,),
            )
            if result.interrupted or (
                not result.ok and value.receipt is not None and value.receipt.status == "success"
            ):
                raise ValueError("The local job did not establish successful execution.")
            attempt = consume_validation_result(
                attempt,
                invocation,
                (value.receipt,) if value.receipt is not None else (),
                (value.gap,) if value.gap is not None else (),
            )
        except (AttributeError, TypeError, ValueError):
            attempt = consume_validation_result(attempt, None, ())
        item.payload["repository_validation_attempt"] = attempt
        if attempt.pending is None:
            item.payload.pop("repository_validation_local_request", None)
        return True

    @staticmethod
    def _consume_repository_validation_ci_result(item: WorkItem, result: JobResult) -> bool:
        """Consume owned CI callbacks before the coordinator assigns a state."""
        pending = item.payload.get("repository_validation_ci_request")
        receipt = result.value
        if pending is None and not isinstance(receipt, RepositoryValidationCIRead):
            return False
        attempt = item.payload.get("repository_validation_attempt")
        if type(attempt) is not RepositoryValidationAttempt:
            item.payload["repository_validation_failure"] = "validation_attempt_missing"
            return True
        if type(pending) is ReadRepositoryValidationCIRequest and (
            not result.ok or type(receipt) is not RepositoryValidationCIRead
        ):
            reason = "ci_callback_job_failed" if not result.ok else "ci_callback_result_missing"
            attempt = consume_validation_result(
                attempt, pending.invocation, (), (RepositoryValidationGap("*", reason),)
            )
        elif type(receipt) is RepositoryValidationCIRead and (
            pending is None or receipt.request == pending
        ):
            attempt = consume_validation_result(
                attempt, receipt.request.invocation, receipt.receipts, receipt.gaps
            )
        else:
            attempt = consume_validation_result(attempt, None, ())
        item.payload["repository_validation_attempt"] = attempt
        if attempt.pending is None:
            item.payload.pop("repository_validation_ci_request", None)
        return True

    @staticmethod
    def _consume_review_checkout_result(item: WorkItem, result: JobResult) -> bool:
        """Store the review checkout barrier result when one is pending."""
        if not item.payload.pop("review_checkout_pending", None):
            return False
        item.payload["review_checkout_ready"] = False
        for key in ("review_change_records", "review_diff_base_sha", "review_target_base_sha"):
            item.payload.pop(key, None)
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
        normalized_paths = _review_changed_paths(changed_paths)
        if ready and normalized_paths is None:
            item.payload["review_checkout_error"] = (
                "checkout job returned no bound path manifest"
                if changed_paths is None
                else "checkout job returned an invalid path manifest"
            )
            ready = False
        if (
            ready
            and normalized_paths is not None
            and isinstance(value, dict)
            and ("change_records" in value or item.repo.casefold() == "comet")
            and not _store_review_source_manifest(item, value, normalized_paths)
        ):
            item.payload["review_checkout_error"] = (
                "checkout job returned an invalid source manifest"
            )
            ready = False
        if ready and normalized_paths is not None:
            item.payload["pr_diff"] = review_diff
            item.payload["review_changed_paths"] = list(normalized_paths)
            if is_full_commit_sha(review_base):
                item.payload["reviewed_pr_base_sha"] = review_base
        item.payload["review_checkout_ready"] = ready
        return True
