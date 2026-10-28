"""Production dispatcher for closed worker-owned GitHub operations."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from concurrent.futures import CancelledError
from dataclasses import asdict, dataclass
from threading import Event
from typing import Any, assert_never

from hephaestus.automation.comment_identity import CommentAliasConflictError
from hephaestus.automation.current_plan import read_current_plan
from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.automation.pipeline.admission import parse_publication_scope_files
from hephaestus.automation.pipeline.github_jobs import (
    CurrentPlanScopeRead,
    DirtyDirectPrStateRead,
    FrozenJson,
    GitHubJob,
    GitHubReceipt,
    InspectDirtyDirectPrStateRequest,
    InspectRebaseConflictRequest,
    MergeWaitCycleCompleted,
    PrReviewReconciled,
    RateBudgetRead,
    ReadCurrentPlanScopeRequest,
    ReadRateBudgetRequest,
    ReadRepositoryValidationCIRequest,
    RebaseConflictInspected,
    ReconcilePrReviewRequest,
    RepositoryValidationCIRead,
    RunMergeWaitCycleRequest,
)
from hephaestus.automation.pipeline.repository_validation import RepositoryValidationGap
from hephaestus.automation.pipeline.scope_retraction import is_safe_scope_retraction_path
from hephaestus.automation.pipeline.stages.base import StageGitHub
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.pipeline_github_transport import rate_limit_remaining
from hephaestus.automation.review_anchors import place_findings
from hephaestus.automation.review_journal import CommentJournalReadError, PlanDiscoveryStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineGitHubJobRunner:
    """Dispatch closed requests through a fresh repository-scoped accessor."""

    org: str
    dry_run: bool
    gh_timeout: int = 120

    def run(
        self,
        job: GitHubJob,
        *,
        shutdown: Event | None = None,
        deadline_s: float | None = None,
    ) -> GitHubReceipt:
        """Execute one request without sharing a coordinator/client instance."""
        if isinstance(
            job.request,
            (
                InspectDirtyDirectPrStateRequest,
                InspectRebaseConflictRequest,
                ReadCurrentPlanScopeRequest,
                ReadRepositoryValidationCIRequest,
            ),
        ) and (job.request.repository.casefold() != f"{self.org}/{job.repo}".casefold()):
            raise ValueError("dirty direct request repository does not match the runner")
        github = PipelineGitHub(
            self.org,
            repo=job.repo,
            dry_run=self.dry_run,
            repo_root=job.repo_root,
            gh_timeout=self.gh_timeout,
        )
        request_deadline_s = (
            job.request.deadline_s
            if isinstance(
                job.request,
                (
                    ReconcilePrReviewRequest,
                    ReadRateBudgetRequest,
                    ReadCurrentPlanScopeRequest,
                    ReadRepositoryValidationCIRequest,
                    RunMergeWaitCycleRequest,
                ),
            )
            else operation_deadline_after(self.gh_timeout)
        )
        operation_deadline_s = operation_deadline_after(self.gh_timeout)
        for bound in (deadline_s, request_deadline_s):
            if bound is not None:
                operation_deadline_s = min(operation_deadline_s, bound)
        try:
            with github.operation_deadline(operation_deadline_s, shutdown=shutdown):
                return self._run_request(job, github)
        except (subprocess.TimeoutExpired, CancelledError) as error:
            if not isinstance(job.request, ReadRepositoryValidationCIRequest):
                raise
            reason = (
                "ci_request_deadline"
                if isinstance(error, subprocess.TimeoutExpired)
                else "ci_request_cancelled"
            )
            return RepositoryValidationCIRead(
                job.request, gaps=(RepositoryValidationGap("*", reason),)
            )

    @staticmethod
    def _inspect_rebase_conflict(
        request: InspectRebaseConflictRequest,
        github: StageGitHub,
    ) -> RebaseConflictInspected:
        """Bracket fresh GO and conflict reads with exact PR identity reads."""

        def identity_matches(state: object) -> bool:
            return isinstance(state, dict) and (
                state.get("state") == "OPEN"
                and state.get("headRefOid") == request.reviewed_head_sha
                and state.get("baseRefOid") == request.base_sha
                and state.get("baseRefName") == "main"
                and "autoMergeRequest" in state
                and state["autoMergeRequest"] is None
            )

        try:
            before = github.gh_pr_state(request.pr_number)
            if not identity_matches(before):
                return RebaseConflictInspected(request, False, "rebase PR identity changed")
            has_go, has_no_go = github.pr_has_implementation_state_label(request.pr_number)
            if not has_go or has_no_go:
                return RebaseConflictInspected(request, False, "rebase PR is not exclusive GO")
            readiness = github.gh_pr_merge_readiness(request.pr_number)
            if not isinstance(readiness, dict) or (
                readiness.get("state") != "OPEN"
                or readiness.get("headRefOid") != request.reviewed_head_sha
                or readiness.get("baseRefName") != "main"
                or "autoMergeRequest" not in readiness
                or readiness["autoMergeRequest"] is not None
                or not (
                    readiness.get("mergeable") == "CONFLICTING"
                    or readiness.get("mergeStateStatus") in {"DIRTY", "CONFLICTING"}
                )
            ):
                return RebaseConflictInspected(request, False, "rebase conflict is not confirmed")
            after = github.gh_pr_state(request.pr_number)
            if not identity_matches(after):
                return RebaseConflictInspected(request, False, "rebase PR identity changed")
            final_go, final_no_go = github.pr_has_implementation_state_label(request.pr_number)
            if not final_go or final_no_go:
                return RebaseConflictInspected(request, False, "rebase PR is not exclusive GO")
        except Exception:
            return RebaseConflictInspected(request, False, "rebase live state is unavailable")
        return RebaseConflictInspected(request, True, "exact GO conflict confirmed")

    def _run_request(self, job: GitHubJob, github: PipelineGitHub) -> GitHubReceipt:
        """Dispatch one closed request inside its operation deadline."""
        match job.request:
            case InspectRebaseConflictRequest():
                return self._inspect_rebase_conflict(job.request, github)
            case ReadCurrentPlanScopeRequest():
                return self._read_current_plan_scope(job.request, github)
            case ReadRepositoryValidationCIRequest():
                return github.read_repository_validation_ci(job.request)
            case ReadRateBudgetRequest():
                facts = rate_limit_remaining(call=github._deadline_gh_call)
                return RateBudgetRead(
                    request=job.request,
                    remaining=facts[0] if facts is not None else None,
                    reset_epoch=facts[1] if facts is not None else None,
                )
            case InspectDirtyDirectPrStateRequest():
                return _read_dirty_direct_state(job.request, github)
            case ReconcilePrReviewRequest():
                return self._reconcile_pr_review(job.request, github)
            case RunMergeWaitCycleRequest():
                return self._run_merge_wait_cycle(job.request, github)
            case unknown:
                return assert_never(unknown)
        raise AssertionError("unreachable closed GitHub request dispatch")

    @staticmethod
    def _read_current_plan_scope(
        request: ReadCurrentPlanScopeRequest, github: PipelineGitHub
    ) -> CurrentPlanScopeRead:
        """Return scope only after a complete current-plan read."""
        plan = read_current_plan(request.issue_number, github).plan
        if plan.status is PlanDiscoveryStatus.IDENTITY_CONFLICT:
            raise CommentAliasConflictError(plan.error or "plan identity conflict")
        if plan.status is PlanDiscoveryStatus.READ_ERROR:
            raise CommentJournalReadError(plan.error or "plan scope read failed")
        if plan.status is not PlanDiscoveryStatus.FOUND or plan.plan_text is None:
            raise ValueError("current plan scope is unavailable")
        declared = sorted(parse_publication_scope_files(plan.plan_text))
        usable = []
        for path in declared:
            if is_safe_scope_retraction_path(path):
                usable.append(path)
            else:
                logger.warning(
                    "plan scope for #%s declares an unusable path %r; ignoring it",
                    request.issue_number,
                    path,
                )
        return CurrentPlanScopeRead(
            request=request,
            paths=tuple(usable),
            plan_sha256=hashlib.sha256(plan.plan_text.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _reconcile_pr_review(
        request: ReconcilePrReviewRequest,
        github: Any,
    ) -> PrReviewReconciled:
        """Apply thread decisions, post the findings, and read GitHub back.

        GitHub is the record: the receipt carries the threads GitHub lists after
        the post, not the host's own bookkeeping.
        """
        github.apply_review_thread_decisions(
            request.pr_number,
            resolved_thread_ids=request.resolved_thread_ids,
            feedback=request.feedback.thaw(),
        )
        findings = request.findings.thaw()
        findings = findings if isinstance(findings, list) else []
        inline, body_only = place_findings(
            [finding for finding in findings if isinstance(finding, dict)],
            request.review_diff,
        )
        if inline or body_only or request.summary:
            github.post_review(request.pr_number, inline, body_only, request.summary)
        return PrReviewReconciled(
            request=request,
            unresolved_threads=FrozenJson.snapshot(
                github.list_unresolved_review_threads(request.pr_number)
            ),
            body_only_findings=FrozenJson.snapshot(body_only),
        )

    @staticmethod
    def _run_merge_wait_cycle(  # noqa: C901
        request: RunMergeWaitCycleRequest,
        github: Any,
    ) -> MergeWaitCycleCompleted:
        """Admit an open, GO-labelled pull request and enable native auto-merge."""
        terminal_merge_sha: str | None = None

        def complete(
            outcome: str,
            *,
            attempted: bool = False,
            can_retry: bool = False,
            merge_sha: str | None = None,
            unresolved_thread_count: int = 0,
        ) -> MergeWaitCycleCompleted:
            return MergeWaitCycleCompleted(
                request=request,
                outcome=outcome,
                attempted=attempted,
                retryable=can_retry,
                merge_sha=merge_sha,
                unresolved_thread_count=unresolved_thread_count,
            )

        def terminal(state: object) -> str | None:
            if not isinstance(state, dict):
                return None
            lifecycle = str(state.get("state") or "").upper()
            if lifecycle == "MERGED" or state.get("mergedAt"):
                return "merged"
            if lifecycle == "CLOSED":
                return "closed"
            return None

        def merge_sha_from_state(state: object) -> str | None:
            """Return a validated server merge commit from terminal PR state."""
            merge_commit = state.get("mergeCommit") if isinstance(state, dict) else None
            merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
            if not isinstance(merge_sha, str):
                return None
            normalized = merge_sha.casefold()
            if len(normalized) not in (40, 64) or any(
                character not in "0123456789abcdef" for character in normalized
            ):
                return None
            return normalized

        def operation_boundary() -> str | None:
            if request.cancellation.is_set():
                return "merge_cycle_cancelled"
            return None

        def admit() -> dict[str, object] | str:
            """Admit an open pull request carrying the exclusive GO label.

            The head is deliberately not compared. The approval label is the
            contract, and GitHub merges whatever is current.
            """
            nonlocal terminal_merge_sha
            try:
                state = github.gh_pr_state(request.pr_number)
            except Exception:
                return "pr_state_unavailable"
            terminal_outcome = terminal(state)
            if terminal_outcome is not None:
                terminal_merge_sha = merge_sha_from_state(state)
                return terminal_outcome
            if state is None:
                return "pr_state_unavailable"
            if not isinstance(state, dict):
                return "pr_state_unverified"
            if state.get("state") != "OPEN" or "autoMergeRequest" not in state:
                return "pr_state_unverified"
            try:
                has_go, has_no_go = github.pr_has_implementation_state_label(request.pr_number)
            except Exception:
                return "implementation_state_unavailable"
            if has_no_go and state.get("autoMergeRequest") is not None:
                github.disable_auto_merge(request.pr_number)
            if not has_go or has_no_go:
                return "not_implementation_go"
            return state

        boundary = operation_boundary()
        if boundary is not None:
            return complete(boundary)
        state = admit()
        if isinstance(state, str):
            return complete(state, merge_sha=terminal_merge_sha)
        if request.auto_merge_armed:
            # The auto-merge is already armed. Observe the pull request
            # without enabling it again. GitHub owns the merge from here.
            blocked_threads = _blocked_unresolved_review_thread_count(github, request.pr_number)
            if blocked_threads:
                # GitHub reports the pull request blocked and lists open review
                # threads. Name the count: the merge cannot happen until the
                # review loop answers and resolves those threads.
                return complete(
                    "merge_blocked_by_unresolved_threads",
                    unresolved_thread_count=blocked_threads,
                )
            return complete("auto_merge_enabled", attempted=False)
        # The implementation-GO label is the whole merge contract. GitHub owns
        # everything after this: branch protection, the required checks, any merge
        # queue, and a rebase when the merge reports a real conflict. A
        # conflicting pull request is not a failure here; GitHub surfaces it.
        node_id = state.get("id")
        try:
            result = github.enable_auto_merge(
                request.pr_number,
                pull_request_id=node_id if isinstance(node_id, str) else None,
                deadline_s=request.deadline_s,
                cancellation=request.cancellation,
            )
        except Exception:
            return complete("auto_merge_transport_error", attempted=True, can_retry=True)
        if result.dry_run:
            return complete("auto_merge_dry_run", attempted=True)
        if result.enabled:
            # Auto-merge is armed. GitHub owns the merge and its required checks.
            return complete("auto_merge_enabled", attempted=True)
        if result.transport_error:
            return complete("auto_merge_transport_error", attempted=True, can_retry=True)
        if result.malformed:
            return complete("auto_merge_malformed", attempted=True)
        return complete("auto_merge_not_enabled", attempted=True, can_retry=True)


def _blocked_unresolved_review_thread_count(github: Any, pr_number: int) -> int:
    """Return the open review-thread count when GitHub reports the PR blocked.

    A failed read, a state other than ``BLOCKED``, or an empty thread list
    returns zero. The merge cycle then keeps its observe-and-poll behavior: an
    unreadable block is never reported as a review-thread block.
    """
    try:
        readiness = github.gh_pr_merge_readiness(pr_number)
        if not isinstance(readiness, dict) or readiness.get("mergeStateStatus") != "BLOCKED":
            return 0
        threads = github.list_unresolved_review_threads(pr_number)
    except Exception as error:
        logger.warning(
            "merge_wait: PR #%s: could not check the review-thread block (%s); "
            "continuing to poll the merge",
            pr_number,
            type(error).__name__,
        )
        return 0
    if not isinstance(threads, list):
        return 0
    return len(threads)


def _read_dirty_direct_state(
    request: InspectDirtyDirectPrStateRequest, github: StageGitHub
) -> DirtyDirectPrStateRead:
    """Read complete PR and actor-owned plan evidence through one fresh accessor."""
    branches = github.open_prs_for_branch(request.branch)
    issue_pr = github.find_pr_for_issue(request.issue_number)
    comments = github.issue_comments(request.issue_number)
    issue = github.gh_issue_json(request.issue_number)
    issue_number = issue.get("number")
    if type(issue_number) is not int or issue_number != request.issue_number:
        raise RuntimeError("dirty direct issue identity evidence is incomplete")
    labels = issue.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels
    ):
        raise RuntimeError("dirty direct issue label evidence is incomplete")
    state = issue.get("state")
    if not isinstance(state, str):
        raise RuntimeError("dirty direct issue state evidence is incomplete")
    return DirtyDirectPrStateRead(
        repository=request.repository,
        issue_number=request.issue_number,
        branch=request.branch,
        branch_prs=tuple(branches),
        issue_pr_number=issue_pr,
        plan_journal=FrozenJson.snapshot([asdict(comment) for comment in comments]),
        issue_state=state,
        issue_labels=tuple(label["name"] for label in labels),
    )
