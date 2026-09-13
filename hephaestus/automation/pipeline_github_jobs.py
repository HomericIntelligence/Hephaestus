"""Production dispatcher for closed worker-owned GitHub operations."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from threading import Event
from typing import Any, Literal, assert_never

from hephaestus.automation.comment_identity import CommentAliasConflictError
from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.automation.pipeline.admission import parse_publication_scope_files
from hephaestus.automation.pipeline.github_jobs import (
    AdoptedRemediationPrStateRead,
    AppendReplyJournalRequest,
    CurrentPlanScopeRead,
    DeliverReplyHandoffRequest,
    DirtyDirectPrStateRead,
    EnsureScopeExpansionChildrenRequest,
    FrozenJson,
    GitHubJob,
    GitHubReceipt,
    InspectAdoptedRemediationPrStateRequest,
    InspectDirtyDirectPrStateRequest,
    InspectRebaseConflictRequest,
    InspectRebaseReviewRequest,
    MergeWaitCycleCompleted,
    PrReviewReconciled,
    PublishRebaseReviewRequest,
    RateBudgetRead,
    ReadCurrentPlanScopeRequest,
    ReadRateBudgetRequest,
    RebaseConflictInspected,
    RebaseReviewInspected,
    RebaseReviewPublished,
    ReconcilePrReviewRequest,
    ReconcileScopeExpansionDependenciesRequest,
    RecoverPendingReviewFindingsRequest,
    RecoverRemediationReplyJournalRequest,
    RecoverReplyJournalRequest,
    RemediationReplyJournalRecovered,
    ReplyJournalAppended,
    ReplyJournalRecovered,
    RunMergeWaitCycleRequest,
    ScopeExpansionChildrenEnsured,
    ScopeExpansionDependenciesReconciled,
)
from hephaestus.automation.pipeline.merge_wait_admission import (
    MergeWaitAdmissionSnapshot,
    validate_merge_wait_admission,
)
from hephaestus.automation.pipeline.reply_handoff import (
    attempt_reply_handoff,
    journaled_implementation_remediation_reply_handoff,
    journaled_implementation_reply_handoff,
)
from hephaestus.automation.pipeline.scope_retraction import normalize_scope_retraction_paths
from hephaestus.automation.pipeline.stages.base import StageGitHub
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.pipeline_github_check_policy import EffectiveMergePolicy
from hephaestus.automation.pipeline_github_transport import rate_limit_remaining
from hephaestus.automation.remediation_prepublication import (
    remove_prepublication_receipt,
)
from hephaestus.automation.review_journal import CommentJournalReadError, PlanDiscoveryStatus


def _request_threads(value: FrozenJson, label: str) -> list[Any]:
    """Decode the validated immutable thread list at the worker boundary."""
    threads = value.thaw()
    if not isinstance(threads, list):
        raise ValueError(f"{label} threads must be a list")
    return threads


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
                PublishRebaseReviewRequest,
                InspectRebaseReviewRequest,
                InspectAdoptedRemediationPrStateRequest,
                ReadCurrentPlanScopeRequest,
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
                    RecoverRemediationReplyJournalRequest,
                    AppendReplyJournalRequest,
                    DeliverReplyHandoffRequest,
                    ReconcilePrReviewRequest,
                    RecoverPendingReviewFindingsRequest,
                    ReadRateBudgetRequest,
                    ReadCurrentPlanScopeRequest,
                    RunMergeWaitCycleRequest,
                ),
            )
            else operation_deadline_after(self.gh_timeout)
        )
        operation_deadline_s = operation_deadline_after(self.gh_timeout)
        for bound in (deadline_s, request_deadline_s):
            if bound is not None:
                operation_deadline_s = min(operation_deadline_s, bound)
        with github.operation_deadline(operation_deadline_s, shutdown=shutdown):
            return self._run_request(job, github)

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

    @staticmethod
    def _publish_rebase_review(
        request: PublishRebaseReviewRequest, github: StageGitHub
    ) -> RebaseReviewPublished:
        """Keep publication bound to the original live source and GO state."""
        record = request.record

        def admitted() -> bool:
            state = github.gh_pr_state(record.pr_number)
            return isinstance(state, dict) and (
                state.get("state") == "OPEN"
                and state.get("headRefOid") == record.source_head_sha
                and state.get("baseRefOid") == record.target_base_sha
                and state.get("baseRefName") == "main"
                and "autoMergeRequest" in state
                and state["autoMergeRequest"] is None
                and github.pr_has_implementation_state_label(record.pr_number) == (True, False)
            )

        try:
            if not admitted():
                return RebaseReviewPublished(request, False)
            github.publish_review_rebase_record(record)
            if github.read_review_rebase_record(record.pr_number) != record or not admitted():
                return RebaseReviewPublished(request, False)
        except Exception:
            return RebaseReviewPublished(request, False)
        return RebaseReviewPublished(request, True)

    @staticmethod
    def _inspect_rebase_review(
        request: InspectRebaseReviewRequest, github: StageGitHub
    ) -> RebaseReviewInspected:
        """Read the original audit and resulting identity twice without writes."""
        record = request.record
        try:
            for _ in range(2):
                state = github.gh_pr_state(record.pr_number)
                if not isinstance(state, dict) or (
                    state.get("state") != "OPEN"
                    or state.get("headRefOid") != record.resulting_head_sha
                    or state.get("baseRefOid") != record.target_base_sha
                    or state.get("baseRefName") != "main"
                    or "autoMergeRequest" not in state
                    or state["autoMergeRequest"] is not None
                    or github.pr_has_implementation_state_label(record.pr_number) != (True, False)
                    or github.read_review_rebase_record(record.pr_number) != record
                ):
                    return RebaseReviewInspected(request, False)
        except Exception:
            return RebaseReviewInspected(request, False)
        return RebaseReviewInspected(request, True)

    def _run_request(  # noqa: C901 -- Keep the closed request set in one exhaustive dispatch.
        self, job: GitHubJob, github: PipelineGitHub
    ) -> GitHubReceipt:
        """Dispatch one closed request inside its operation deadline."""
        match job.request:
            case InspectRebaseConflictRequest():
                return self._inspect_rebase_conflict(job.request, github)
            case PublishRebaseReviewRequest():
                return self._publish_rebase_review(job.request, github)
            case InspectRebaseReviewRequest():
                return self._inspect_rebase_review(job.request, github)
            case ReadCurrentPlanScopeRequest():
                return self._read_current_plan_scope(job.request, github)
            case ReadRateBudgetRequest():
                facts = rate_limit_remaining(call=github._deadline_gh_call)
                return RateBudgetRead(
                    request=job.request,
                    remaining=facts[0] if facts is not None else None,
                    reset_epoch=facts[1] if facts is not None else None,
                )
            case InspectAdoptedRemediationPrStateRequest():
                return _read_adopted_remediation_state(job.request, github)
            case InspectDirtyDirectPrStateRequest():
                return _read_dirty_direct_state(job.request, github)
            case RecoverReplyJournalRequest():
                threads = _request_threads(job.request.threads, "recovery")
                handoff = journaled_implementation_reply_handoff(
                    github.issue_comments(job.request.issue_number),
                    pr_number=job.request.pr_number,
                    threads=threads,
                )
                return ReplyJournalRecovered(
                    request=job.request,
                    handoff=FrozenJson.snapshot(handoff) if handoff is not None else None,
                )
            case RecoverRemediationReplyJournalRequest():
                threads = _request_threads(job.request.threads, "remediation recovery")
                handoff = journaled_implementation_remediation_reply_handoff(
                    github.issue_comments(job.request.pr_number),
                    repository=job.request.repository,
                    issue_number=job.request.issue_number,
                    pr_number=job.request.pr_number,
                    branch=job.request.branch,
                    current_remote_head=job.request.current_remote_head,
                    threads=threads,
                )
                return RemediationReplyJournalRecovered(
                    request=job.request,
                    handoff=FrozenJson.snapshot(handoff) if handoff is not None else None,
                )
            case AppendReplyJournalRequest():
                return self._append_reply_journal(job, job.request, github)
            case DeliverReplyHandoffRequest():
                return attempt_reply_handoff(job.request, github)
            case ReconcilePrReviewRequest():
                return self._reconcile_pr_review(job.request, github)
            case RecoverPendingReviewFindingsRequest():
                return self._recover_pending_review_findings(job.request, github)
            case RunMergeWaitCycleRequest():
                return self._run_merge_wait_cycle(job.request, github)
            case EnsureScopeExpansionChildrenRequest():
                return self._ensure_scope_expansion_children(job.request, github)
            case ReconcileScopeExpansionDependenciesRequest():
                return self._reconcile_scope_expansion_dependencies(job.request, github)
            case unknown:
                return assert_never(unknown)
        raise AssertionError("unreachable closed GitHub request dispatch")

    @staticmethod
    def _read_current_plan_scope(
        request: ReadCurrentPlanScopeRequest, github: PipelineGitHub
    ) -> CurrentPlanScopeRead:
        """Return scope only after a complete current-plan read."""
        plan = github.discover_plan(request.issue_number)
        if plan.status is PlanDiscoveryStatus.IDENTITY_CONFLICT:
            raise CommentAliasConflictError(plan.error or "plan identity conflict")
        if plan.status is PlanDiscoveryStatus.READ_ERROR:
            raise CommentJournalReadError(plan.error or "plan scope read failed")
        if plan.status is not PlanDiscoveryStatus.FOUND or plan.plan_text is None:
            raise ValueError("current plan scope is unavailable")
        return CurrentPlanScopeRead(
            request=request,
            paths=tuple(sorted(parse_publication_scope_files(plan.plan_text))),
            plan_sha256=hashlib.sha256(plan.plan_text.encode("utf-8")).hexdigest(),
        )

    def _append_reply_journal(
        self, job: GitHubJob, request: AppendReplyJournalRequest, github: PipelineGitHub
    ) -> ReplyJournalAppended:
        """Publish the current receipt before removing its local pending record."""
        github.append_issue_comment(request.issue_number, request.marker, request.body)
        if request.prepublication_receipt_sha256 is not None and not self.dry_run:
            remove_prepublication_receipt(
                repo_root=job.repo_root,
                pr_number=request.issue_number,
                expected_review_input_sha256=request.prepublication_receipt_sha256,
            )
        return ReplyJournalAppended(request=request)

    @staticmethod
    def _reconcile_scope_expansion_dependencies(  # noqa: C901
        request: ReconcileScopeExpansionDependenciesRequest,
        github: Any,
    ) -> ScopeExpansionDependenciesReconciled:
        """Classify all durable child dependencies for one exact source head."""
        from hephaestus.automation.pipeline.scope_expansion_records import (
            SCOPE_EXPANSION_LIFECYCLE_MARKER_PREFIX,
            parse_scope_expansion_child_body,
            parse_scope_expansion_lifecycle_comment,
            render_scope_expansion_blocking_review,
            render_scope_expansion_lifecycle_comment,
            scope_expansion_blocking_review_marker,
            scope_expansion_lifecycle_marker,
        )
        from hephaestus.automation.pipeline.stages.pr_review_threads import (
            _durable_thread_id,
            _normalize_remediation_threads,
            _scope_retraction_paths,
            _validation_receipt_fingerprints,
            _validation_thread_snapshots,
            _without_duplicate_live_findings,
        )

        state = github.gh_pr_state(request.pr_number)
        if (
            not isinstance(state, dict)
            or state.get("state") != "OPEN"
            or "autoMergeRequest" not in state
            or state.get("autoMergeRequest") is not None
            or state.get("headRefOid") != request.source_head_sha
            or state.get("baseRefName") != "main"
        ):
            raise RuntimeError("source pull request state changed")
        repo = getattr(github, "_repo_slug", None) or getattr(github, "repo", None)
        if not isinstance(repo, str) or not repo:
            raise RuntimeError("repository identity is unavailable")
        records: dict[str, Any] = {}
        malformed = False
        for comment in github.issue_comments(request.pr_number):
            if not getattr(comment, "viewer_did_author", False):
                continue
            body = getattr(comment, "body", "")
            if not isinstance(body, str) or not body.startswith(
                SCOPE_EXPANSION_LIFECYCLE_MARKER_PREFIX
            ):
                continue
            record = parse_scope_expansion_lifecycle_comment(body)
            if (
                record is None
                or record.repository != repo.lower()
                or record.parent_issue != request.issue_number
                or record.pr_number != request.pr_number
            ):
                malformed = True
                continue
            prior = records.get(record.digest)
            if prior is not None:
                malformed = True
                continue
            records[record.digest] = record

        def receipt(
            status: str,
            child_numbers: list[int] | None = None,
            merge_shas: list[str] | None = None,
            retraction_threads: list[dict[str, Any]] | None = None,
            retraction_snapshots: list[dict[str, Any]] | None = None,
        ) -> ScopeExpansionDependenciesReconciled:
            return ScopeExpansionDependenciesReconciled(
                request=request,
                status=status,  # type: ignore[arg-type]
                child_issue_numbers=tuple(child_numbers or ()),
                merge_shas=tuple(merge_shas or ()),
                retraction_threads=FrozenJson.snapshot(retraction_threads or []),
                retraction_snapshots=FrozenJson.snapshot(retraction_snapshots or []),
            )

        if malformed:
            return receipt("operator_required")
        if not records:
            return receipt("none")
        if not bool(getattr(github, "dry_run", False)):
            github.mark_pr_implementation_no_go(request.pr_number)
        has_go, has_no_go = github.pr_has_implementation_state_label(request.pr_number)
        if has_go or not has_no_go:
            return receipt("operator_required")
        first_record = next(iter(records.values()))
        if any(
            record.retraction_findings != first_record.retraction_findings
            or record.review_diff != first_record.review_diff
            for record in records.values()
        ):
            return receipt("operator_required")
        projection = [dict(finding) for finding in first_record.retraction_findings]
        if projection and any(not _scope_retraction_paths([finding]) for finding in projection):
            return receipt("operator_required")
        child_numbers: list[int] = []
        bound_children: list[tuple[Any, int, str, Any]] = []
        for record in records.values():
            child_number = record.child_issue_number
            if child_number is None:
                child_marker = f"<!-- hephaestus-scope-expansion-child:{record.digest} -->"
                first = github.issues_with_marker(child_marker)
                second = github.issues_with_marker(child_marker)
                first_numbers = [issue.get("number") for issue in first if isinstance(issue, dict)]
                second_numbers = [
                    issue.get("number") for issue in second if isinstance(issue, dict)
                ]
                if (
                    len(first_numbers) != 1
                    or first_numbers != second_numbers
                    or not isinstance(first_numbers[0], int)
                    or first_numbers[0] <= 0
                ):
                    return receipt("operator_required", child_numbers)
                child_number = first_numbers[0]
            if child_number in child_numbers:
                return receipt("operator_required", child_numbers)
            child_numbers.append(child_number)
            child = github.gh_issue_json(child_number)
            child_state = str(child.get("state") or "").upper()
            child_record = parse_scope_expansion_child_body(child.get("body"))
            if (
                child_record is None
                or child_record.digest != record.digest
                or child_record.repository != repo.lower()
                or child_record.parent_issue != request.issue_number
                or child_record.pr_number != request.pr_number
                or (
                    child_record.child_issue_number is not None
                    and child_record.child_issue_number != child_number
                )
            ):
                return receipt("operator_required", child_numbers)
            lifecycle_marker = scope_expansion_lifecycle_marker(
                repo, request.issue_number, child_record.expansion
            )
            current_record = record
            if record.state == "pending-child" or (
                record.state == "pending-review" and record.merge_sha is None
            ):
                if record.reviewed_head_sha != request.source_head_sha:
                    return receipt("operator_required", child_numbers)
                github.upsert_issue_comment(
                    request.pr_number,
                    lifecycle_marker,
                    render_scope_expansion_lifecycle_comment(
                        repository=repo,
                        parent_issue=request.issue_number,
                        pr_number=request.pr_number,
                        reviewed_head_sha=record.reviewed_head_sha,
                        expansion=child_record.expansion,
                        state="pending-review",
                        child_issue_number=child_number,
                        retraction_findings=record.retraction_findings,
                        review_diff=record.review_diff,
                    ),
                )
                blocking_marker = scope_expansion_blocking_review_marker(
                    repo, request.issue_number, child_record.expansion
                )
                github.post_scope_expansion_blocking_review(
                    request.pr_number,
                    body=render_scope_expansion_blocking_review(
                        repository=repo,
                        parent_issue=request.issue_number,
                        pr_number=request.pr_number,
                        reviewed_head_sha=record.reviewed_head_sha,
                        child_issue_number=child_number,
                        expansion=child_record.expansion,
                    ),
                    marker=blocking_marker,
                )
                github.upsert_issue_comment(
                    request.pr_number,
                    lifecycle_marker,
                    render_scope_expansion_lifecycle_comment(
                        repository=repo,
                        parent_issue=request.issue_number,
                        pr_number=request.pr_number,
                        reviewed_head_sha=record.reviewed_head_sha,
                        expansion=child_record.expansion,
                        state="blocked",
                        child_issue_number=child_number,
                        retraction_findings=record.retraction_findings,
                        review_diff=record.review_diff,
                    ),
                )
                lifecycle_readback = [
                    parse_scope_expansion_lifecycle_comment(getattr(comment, "body", ""))
                    for comment in github.issue_comments(request.pr_number)
                    if getattr(comment, "viewer_did_author", False)
                    and str(getattr(comment, "body", "")).startswith(lifecycle_marker)
                ]
                if (
                    len(lifecycle_readback) != 1
                    or lifecycle_readback[0] is None
                    or lifecycle_readback[0].state != "blocked"
                    or lifecycle_readback[0].child_issue_number != child_number
                    or lifecycle_readback[0].digest != record.digest
                    or lifecycle_readback[0].retraction_findings != record.retraction_findings
                    or lifecycle_readback[0].review_diff != record.review_diff
                ):
                    return receipt("operator_required", child_numbers)
                current_record = lifecycle_readback[0]
            bound_children.append((current_record, child_number, child_state, child_record))
        live_threads = github.list_unresolved_review_threads(request.pr_number)
        live_by_id = {
            thread_id: thread
            for thread in live_threads
            if (thread_id := _durable_thread_id(thread)) is not None
        }
        try:
            missing_projection = _without_duplicate_live_findings(projection, live_by_id)
        except ValueError:
            return receipt("operator_required")
        if missing_projection:
            if first_record.reviewed_head_sha != request.source_head_sha:
                return receipt("operator_required")
            posted = github.post_review_threads(
                request.pr_number,
                missing_projection,
                expected_head_sha=request.source_head_sha,
                review_diff=first_record.review_diff,
            )
            if len(posted) != len(missing_projection):
                return receipt("operator_required")
            live_threads = github.list_unresolved_review_threads(request.pr_number)
        validation_receipts = github.reviewer_validation_receipts(
            request.pr_number,
            reviewed_head_sha=request.source_head_sha,
            threads=live_threads,
        )
        normalized_threads = _normalize_remediation_threads(live_threads)
        snapshots = _validation_thread_snapshots(live_threads, validation_receipts)
        if (
            len(normalized_threads) != len(live_threads)
            or snapshots is None
            or _validation_receipt_fingerprints(validation_receipts) is None
        ):
            return receipt("operator_required")
        pending_retractions: list[dict[str, Any]] = []
        pending_snapshots: list[dict[str, Any]] = []
        for thread, snapshot in zip(normalized_threads, snapshots, strict=True):
            paths = _scope_retraction_paths([thread])
            if paths is None:
                return receipt("operator_required")
            if paths and not snapshot.get("implementation_reply_submitted"):
                pending_retractions.append(dict(thread))
                pending_snapshots.append(dict(snapshot))
        if pending_retractions:
            return receipt(
                "retraction_required",
                child_numbers,
                retraction_threads=pending_retractions,
                retraction_snapshots=pending_snapshots,
            )
        clear_projection = bool(projection)
        merge_shas: list[str] = []
        parked = False
        operator_required = False
        sync_required = False
        for record, child_number, child_state, child_record in bound_children:
            lifecycle_marker = scope_expansion_lifecycle_marker(
                repo, request.issue_number, child_record.expansion
            )
            record_retractions = () if clear_projection else record.retraction_findings
            record_review_diff = "" if clear_projection else record.review_diff
            if clear_projection:
                github.upsert_issue_comment(
                    request.pr_number,
                    lifecycle_marker,
                    render_scope_expansion_lifecycle_comment(
                        repository=repo,
                        parent_issue=request.issue_number,
                        pr_number=request.pr_number,
                        reviewed_head_sha=record.reviewed_head_sha,
                        expansion=child_record.expansion,
                        state="blocked",
                        child_issue_number=child_number,
                    ),
                )
            evidence = github.merged_scope_expansion_pr(
                child_number, source_pr_number=request.pr_number
            )
            if evidence is None:
                if child_state == "OPEN":
                    parked = True
                else:
                    operator_required = True
                continue
            merge_sha = evidence.get("merge_sha")
            if not isinstance(merge_sha, str) or not github.commit_is_ancestor(merge_sha, "main"):
                operator_required = True
                continue
            merge_shas.append(merge_sha)
            if not github.commit_is_ancestor(merge_sha, request.source_head_sha):
                sync_required = True
            if record.state != "pending-review" or record.merge_sha != merge_sha:
                lifecycle_body = render_scope_expansion_lifecycle_comment(
                    repository=repo,
                    parent_issue=request.issue_number,
                    pr_number=request.pr_number,
                    reviewed_head_sha=request.source_head_sha,
                    expansion=child_record.expansion,
                    state="pending-review",
                    child_issue_number=child_number,
                    merge_sha=merge_sha,
                    retraction_findings=record_retractions,
                    review_diff=record_review_diff,
                )
                github.upsert_issue_comment(
                    request.pr_number,
                    scope_expansion_lifecycle_marker(
                        repo, request.issue_number, child_record.expansion
                    ),
                    lifecycle_body,
                )
        if operator_required:
            return receipt("operator_required", child_numbers, merge_shas)
        final_state = github.gh_pr_state(request.pr_number)
        final_has_go, final_has_no_go = github.pr_has_implementation_state_label(request.pr_number)
        if (
            not isinstance(final_state, dict)
            or final_state.get("state") != "OPEN"
            or "autoMergeRequest" not in final_state
            or final_state.get("autoMergeRequest") is not None
            or final_state.get("headRefOid") != request.source_head_sha
            or final_has_go
            or not final_has_no_go
        ):
            raise RuntimeError("source pull request state changed during reconciliation")
        if parked:
            return receipt("parked", child_numbers, merge_shas)
        if sync_required:
            return receipt("sync_required", child_numbers, merge_shas)
        return receipt("fresh_review", child_numbers, merge_shas)

    @staticmethod
    def _ensure_scope_expansion_children(  # noqa: C901
        request: EnsureScopeExpansionChildrenRequest,
        github: Any,
    ) -> ScopeExpansionChildrenEnsured:
        """Ensure one durable child issue per expansion and record the source block."""
        from hephaestus.automation.pipeline.scope_expansion_records import (
            parse_scope_expansion_child_body,
            parse_scope_expansion_lifecycle_comment,
            render_scope_expansion_blocking_review,
            render_scope_expansion_child_body,
            render_scope_expansion_lifecycle_comment,
            scope_expansion_blocking_review_marker,
            scope_expansion_child_marker,
            scope_expansion_lifecycle_marker,
        )
        from hephaestus.automation.scope_expansion_domain import (
            ScopeExpansion,
            scope_expansion_digest,
        )

        def repository() -> str:
            value = getattr(github, "_repo_slug", None)
            if isinstance(value, str) and value:
                return value
            value = getattr(github, "repo", None)
            return value if isinstance(value, str) else ""

        repo = repository()
        if not isinstance(repo, str) or not repo:
            raise RuntimeError("repository identity is unavailable")

        def require_source_head() -> None:
            state = github.gh_pr_state(request.pr_number)
            if not isinstance(state, dict) or state.get("state") != "OPEN":
                raise RuntimeError("source pull request is not open")
            if "autoMergeRequest" not in state or state.get("autoMergeRequest") is not None:
                raise RuntimeError("source pull request is armed or unverified")
            if state.get("headRefOid") != request.reviewed_head_sha:
                raise RuntimeError("source pull request reviewed head changed")

        require_source_head()
        dry_run = bool(getattr(github, "dry_run", False))
        if not dry_run:
            github.mark_pr_implementation_no_go(request.pr_number)
            require_source_head()
            has_go, has_no_go = github.pr_has_implementation_state_label(request.pr_number)
            if has_go or not has_no_go:
                raise RuntimeError("exclusive implementation-no-go state was not confirmed")
        child_issue_numbers: list[int] = []
        overall_status: Literal["blocked", "operator_required", "dry_run"] = "blocked"
        raw_retractions = request.retraction_findings.thaw()
        if not isinstance(raw_retractions, list):
            raise RuntimeError("retraction projection is invalid")
        retraction_projection = tuple(
            dict(finding) for finding in raw_retractions if isinstance(finding, dict)
        )
        for expansion in request.scope_expansions:
            if not isinstance(expansion, ScopeExpansion):
                raise TypeError("scope_expansions must contain scope-expansion records")
            child_marker = scope_expansion_child_marker(repo, request.issue_number, expansion)
            digest = scope_expansion_digest(repo, request.issue_number, expansion)
            lifecycle_marker = scope_expansion_lifecycle_marker(
                repo, request.issue_number, expansion
            )
            blocking_marker = scope_expansion_blocking_review_marker(
                repo,
                request.issue_number,
                expansion,
            )
            lifecycle_records = []
            malformed_lifecycle = False
            for comment in github.issue_comments(request.pr_number):
                if not getattr(comment, "viewer_did_author", False):
                    continue
                body = getattr(comment, "body", "")
                if not isinstance(body, str) or not body.startswith(lifecycle_marker):
                    continue
                record = parse_scope_expansion_lifecycle_comment(body)
                if (
                    record is None
                    or record.repository != repo.lower()
                    or record.parent_issue != request.issue_number
                    or record.pr_number != request.pr_number
                    or record.reviewed_head_sha != request.reviewed_head_sha
                ):
                    malformed_lifecycle = True
                    continue
                lifecycle_records.append(record)
            if malformed_lifecycle or len(lifecycle_records) > 1:
                overall_status = "operator_required"
                continue
            new_intent = not lifecycle_records
            if new_intent:
                if dry_run:
                    overall_status = "dry_run"
                    continue
                pending_body = render_scope_expansion_lifecycle_comment(
                    repository=repo,
                    parent_issue=request.issue_number,
                    pr_number=request.pr_number,
                    reviewed_head_sha=request.reviewed_head_sha,
                    expansion=expansion,
                    state="pending-child",
                    retraction_findings=retraction_projection,
                    review_diff=request.review_diff,
                )
                github.upsert_issue_comment(request.pr_number, lifecycle_marker, pending_body)
                require_source_head()
                readback = [
                    parse_scope_expansion_lifecycle_comment(getattr(comment, "body", ""))
                    for comment in github.issue_comments(request.pr_number)
                    if getattr(comment, "viewer_did_author", False)
                    and str(getattr(comment, "body", "")).startswith(lifecycle_marker)
                ]
                if len(readback) != 1 or readback[0] is None:
                    overall_status = "operator_required"
                    continue

            first_children = github.issues_with_marker(child_marker)
            second_children = github.issues_with_marker(child_marker)
            first_numbers = [
                child.get("number") for child in first_children if isinstance(child, dict)
            ]
            second_numbers = [
                child.get("number") for child in second_children if isinstance(child, dict)
            ]
            if first_numbers != second_numbers or len(first_numbers) > 1:
                overall_status = "operator_required"
                continue
            if not first_numbers:
                if not new_intent:
                    overall_status = "operator_required"
                    continue
                child_body = render_scope_expansion_child_body(
                    repository=repo,
                    parent_issue=request.issue_number,
                    pr_number=request.pr_number,
                    reviewed_head_sha=request.reviewed_head_sha,
                    expansion=expansion,
                )
                child_issue_number = github.create_issue(expansion.title, child_body)
            else:
                child_issue_number = first_numbers[0]
            if not isinstance(child_issue_number, int) or child_issue_number <= 0:
                overall_status = "dry_run"
                continue
            child_issue_numbers.append(child_issue_number)
            child = github.gh_issue_json(child_issue_number)
            child_record = parse_scope_expansion_child_body(child.get("body"))
            if (
                child_record is None
                or child_record.digest != digest
                or child_record.repository != repo.lower()
                or child_record.parent_issue != request.issue_number
                or child_record.pr_number != request.pr_number
                or child_record.reviewed_head_sha != request.reviewed_head_sha
                or child_record.expansion != expansion
                or (
                    child_record.child_issue_number is not None
                    and child_record.child_issue_number != child_issue_number
                )
            ):
                overall_status = "operator_required"
                continue
            prior_record = lifecycle_records[0] if lifecycle_records else None
            blocking_complete = (
                prior_record is not None
                and prior_record.state == "blocked"
                and prior_record.child_issue_number == child_issue_number
            )
            blocking_body = render_scope_expansion_blocking_review(
                repository=repo,
                parent_issue=request.issue_number,
                pr_number=request.pr_number,
                reviewed_head_sha=request.reviewed_head_sha,
                child_issue_number=child_issue_number,
                expansion=expansion,
            )
            if not blocking_complete:
                pending_review_body = render_scope_expansion_lifecycle_comment(
                    repository=repo,
                    parent_issue=request.issue_number,
                    pr_number=request.pr_number,
                    reviewed_head_sha=request.reviewed_head_sha,
                    expansion=expansion,
                    state="pending-review",
                    child_issue_number=child_issue_number,
                    retraction_findings=retraction_projection,
                    review_diff=request.review_diff,
                )
                github.upsert_issue_comment(
                    request.pr_number, lifecycle_marker, pending_review_body
                )
                require_source_head()
                github.post_scope_expansion_blocking_review(
                    request.pr_number,
                    body=blocking_body,
                    marker=blocking_marker,
                )
                blocked_body = render_scope_expansion_lifecycle_comment(
                    repository=repo,
                    parent_issue=request.issue_number,
                    pr_number=request.pr_number,
                    reviewed_head_sha=request.reviewed_head_sha,
                    expansion=expansion,
                    state="blocked",
                    child_issue_number=child_issue_number,
                    retraction_findings=retraction_projection,
                    review_diff=request.review_diff,
                )
                github.upsert_issue_comment(request.pr_number, lifecycle_marker, blocked_body)
                readback = [
                    parse_scope_expansion_lifecycle_comment(getattr(comment, "body", ""))
                    for comment in github.issue_comments(request.pr_number)
                    if getattr(comment, "viewer_did_author", False)
                    and str(getattr(comment, "body", "")).startswith(lifecycle_marker)
                ]
                if (
                    len(readback) != 1
                    or readback[0] is None
                    or readback[0].state != "blocked"
                    or readback[0].child_issue_number != child_issue_number
                ):
                    overall_status = "operator_required"
                    continue
            elif prior_record is not None and (
                prior_record.retraction_findings != retraction_projection
                or prior_record.review_diff != request.review_diff
            ):
                require_source_head()
                github.upsert_issue_comment(
                    request.pr_number,
                    lifecycle_marker,
                    render_scope_expansion_lifecycle_comment(
                        repository=repo,
                        parent_issue=request.issue_number,
                        pr_number=request.pr_number,
                        reviewed_head_sha=request.reviewed_head_sha,
                        expansion=expansion,
                        state="blocked",
                        child_issue_number=child_issue_number,
                        retraction_findings=retraction_projection,
                        review_diff=request.review_diff,
                    ),
                )
                require_source_head()
                projection_readback = [
                    parse_scope_expansion_lifecycle_comment(getattr(comment, "body", ""))
                    for comment in github.issue_comments(request.pr_number)
                    if getattr(comment, "viewer_did_author", False)
                    and str(getattr(comment, "body", "")).startswith(lifecycle_marker)
                ]
                if (
                    len(projection_readback) != 1
                    or projection_readback[0] is None
                    or projection_readback[0].state != "blocked"
                    or projection_readback[0].child_issue_number != child_issue_number
                    or projection_readback[0].retraction_findings != retraction_projection
                    or projection_readback[0].review_diff != request.review_diff
                ):
                    overall_status = "operator_required"
                    continue
            child_state = str(child.get("state") or "").upper()
            evidence = github.merged_scope_expansion_pr(
                child_issue_number, source_pr_number=request.pr_number
            )
            if evidence is None:
                if child_state != "OPEN":
                    overall_status = "operator_required"
            else:
                merge_sha = evidence.get("merge_sha")
                if not isinstance(merge_sha, str) or not github.commit_is_ancestor(
                    merge_sha, "main"
                ):
                    overall_status = "operator_required"
        if not dry_run:
            require_source_head()
            has_go, has_no_go = github.pr_has_implementation_state_label(request.pr_number)
            if has_go or not has_no_go:
                raise RuntimeError("exclusive implementation-no-go state was not confirmed")
        return ScopeExpansionChildrenEnsured(
            request=request,
            status=overall_status,
            child_issue_numbers=tuple(child_issue_numbers),
        )

    @staticmethod
    def _recover_pending_review_findings(
        request: RecoverPendingReviewFindingsRequest,
        github: Any,
    ) -> PrReviewReconciled:
        """Recover saved publications without reviewer-response reconciliation."""
        return PipelineGitHubJobRunner._reconcile_pr_review(request, github)

    @staticmethod
    def _reconcile_pr_review(  # noqa: C901
        request: ReconcilePrReviewRequest | RecoverPendingReviewFindingsRequest,
        github: Any,
    ) -> PrReviewReconciled:
        """Run fresh receipt reconciliation, publication, and late-thread readback."""
        from hephaestus.automation.github_api.diff import (
            _validate_comments_to_diff,
            compact_terminal_review_finding_collection,
            empty_review_finding_compacted_outcomes,
            normalize_review_finding_collection,
            normalize_review_finding_records,
            review_finding_collection_payload,
        )
        from hephaestus.automation.pipeline.stages.pr_review_threads import (
            _durable_thread_id,
            _finding_content_key,
            _finding_key,
            _is_postable_finding,
            _normalize_remediation_threads,
            _validation_pr_metadata_fingerprint,
            _validation_receipt_fingerprints,
            _without_duplicate_live_findings,
        )
        from hephaestus.automation.prompts.pr_review import (
            SEVERITY_MARKER_PREFIX,
            VALID_SEVERITIES,
        )

        def receipt(
            action: str,
            *,
            posted: Any = (),
            unresolved: Any = (),
            remediation: Any = (),
            corrections: Any = (),
            unpublishable: Any = (),
            final_finding_records: Any = None,
            final_compacted_outcomes: Any = None,
        ) -> PrReviewReconciled:
            return PrReviewReconciled(
                request=request,
                action=action,  # type: ignore[arg-type]
                posted_receipts=FrozenJson.snapshot(list(posted)),
                unresolved_threads=FrozenJson.snapshot(list(unresolved)),
                remediation_threads=FrozenJson.snapshot(list(remediation)),
                anchor_corrections=FrozenJson.snapshot(list(corrections)),
                unpublishable_findings=FrozenJson.snapshot(list(unpublishable)),
                final_finding_records=(
                    None
                    if final_finding_records is None
                    else FrozenJson.snapshot(list(final_finding_records))
                ),
                final_compacted_outcomes=(
                    None
                    if final_compacted_outcomes is None
                    else FrozenJson.snapshot(dict(final_compacted_outcomes))
                ),
            )

        def correction_data(value: object) -> dict[str, object] | None:
            """Convert one typed correction into a bounded JSON record."""
            finding = getattr(value, "finding", None)
            path = getattr(value, "path", None)
            line = getattr(value, "line", None)
            side = getattr(value, "side", None)
            reason = getattr(value, "reason", None)
            finding_id = getattr(value, "finding_id", None)
            if (
                not isinstance(finding, dict)
                or not isinstance(path, str)
                or (line is not None and (not isinstance(line, int) or isinstance(line, bool)))
                or not isinstance(side, str)
                or not isinstance(reason, str)
                or reason
                not in (
                    "reviewed_diff_unavailable",
                    "path_not_in_diff",
                    "line_not_in_diff",
                    "unsupported_side",
                )
                or not isinstance(finding_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", finding_id) is None
            ):
                return None
            return {
                "finding": dict(finding),
                "finding_id": finding_id,
                "path": path,
                "line": line,
                "side": side,
                "reason": reason,
            }

        def correction_records(value: object) -> list[dict[str, object]] | None:
            """Validate one complete collection of typed corrections."""
            if not isinstance(value, (list, tuple)):
                return None
            records = [correction_data(entry) for entry in value]
            if any(record is None for record in records):
                return None
            return [record for record in records if record is not None]

        pr_context = github.pr_review_context(request.pr_number)
        if (
            not isinstance(pr_context, dict)
            or pr_context.get("pr_head_sha") != request.reviewed_head_sha
        ):
            return receipt("fresh_review")
        publication_only = isinstance(request, RecoverPendingReviewFindingsRequest)
        review_diff = request.review_diff
        if isinstance(request, ReconcilePrReviewRequest):
            live_for_reconciliation = github.list_unresolved_review_threads(request.pr_number)
            validation_receipts = github.reviewer_validation_receipts(
                request.pr_number,
                reviewed_head_sha=request.reviewed_head_sha,
                threads=live_for_reconciliation,
            )
            live_fingerprints = _validation_receipt_fingerprints(validation_receipts)
            if live_fingerprints is None:
                return receipt("audit_failure")
            validated_fingerprints = (
                request.validated_receipt_fingerprints.thaw()
                if request.validated_receipt_fingerprints is not None
                else None
            )
            live_metadata = _validation_pr_metadata_fingerprint(
                pr_context,
                request.reviewed_head_sha,
            )
            metadata_guard_expected = request.validated_receipt_fingerprints is not None or (
                request.validated_metadata_fingerprint is not None
            )
            if metadata_guard_expected and (
                live_metadata is None or request.validated_metadata_fingerprint != live_metadata
            ):
                return receipt("revalidate")
            if validated_fingerprints is not None and validated_fingerprints != live_fingerprints:
                return receipt("revalidate")

            if validation_receipts:
                expected_ids = {_durable_thread_id(entry) for entry in validation_receipts}
                feedback = request.feedback.thaw()
                if not isinstance(feedback, dict) or None in expected_ids:
                    return receipt("audit_failure")
                feedback_ids = set(feedback)
                resolved_ids = set(request.resolved_thread_ids)
                if (
                    resolved_ids & feedback_ids
                    or resolved_ids | feedback_ids != expected_ids
                    or not all(
                        isinstance(value, str) and value.strip() for value in feedback.values()
                    )
                ):
                    return receipt("audit_failure")
                reconciliation = github.reconcile_reviewer_validated_threads(
                    request.pr_number,
                    reviewed_head_sha=request.reviewed_head_sha,
                    receipts=validation_receipts,
                    resolved_thread_ids=resolved_ids,
                    feedback=feedback,
                )
                completed_ids = set(reconciliation.resolved_thread_ids) | set(
                    reconciliation.feedback_thread_ids
                )
                if not completed_ids.issubset(expected_ids):
                    return receipt("audit_failure")
                if reconciliation.blocked_thread_ids:
                    return receipt("fresh_review")

        live_before_post = github.list_unresolved_review_threads(request.pr_number)
        live_by_id = {
            thread_id: thread
            for thread in live_before_post
            if (thread_id := _durable_thread_id(thread)) is not None
        }
        raw_findings = request.findings.thaw()
        if not isinstance(raw_findings, list) or not all(
            isinstance(finding, dict) for finding in raw_findings
        ):
            return receipt("audit_failure")
        raw_records = request.finding_records.thaw()
        raw_compacted_outcomes = (
            None if request.compacted_outcomes is None else request.compacted_outcomes.thaw()
        )
        if publication_only and raw_compacted_outcomes == empty_review_finding_compacted_outcomes():
            raw_compacted_outcomes = None
        try:
            finding_records, compacted_outcomes = normalize_review_finding_collection(
                raw_records,
                compacted_outcomes=raw_compacted_outcomes,
            )
        except ValueError:
            return receipt("audit_failure")
        pending_finding_ids = {
            str(record["finding_id"]) for record in finding_records if record["status"] == "pending"
        }

        def recovery_key(value: dict[str, object]) -> tuple[object, str] | None:
            """Bind visible publication evidence to content and severity."""
            key = _finding_key(value)
            severity = str(value.get("severity") or "").strip().lower()
            body = value.get("body")
            marker_lines = (
                [
                    line.strip()
                    for line in body.splitlines()
                    if line.strip().startswith(SEVERITY_MARKER_PREFIX)
                ]
                if isinstance(body, str)
                else []
            )
            if marker_lines:
                if len(marker_lines) != 1 or not marker_lines[0].endswith("-->"):
                    return None
                marker_severity = (
                    marker_lines[0]
                    .removeprefix(SEVERITY_MARKER_PREFIX)
                    .removesuffix("-->")
                    .strip()
                    .lower()
                )
                if marker_severity not in VALID_SEVERITIES or (
                    severity in VALID_SEVERITIES and severity != marker_severity
                ):
                    return None
                severity = marker_severity
            return None if key is None or severity not in VALID_SEVERITIES else (key, severity)

        def is_owned_exact_head_thread(value: dict[str, object]) -> bool:
            """Return whether the root review comment proves this actor and head."""
            comments = value.get("comments")
            if not isinstance(comments, list) or not comments:
                return False
            root = comments[0]
            return (
                isinstance(root, dict)
                and root.get("body") == value.get("body")
                and root.get("viewer_did_author") is True
                and root.get("review_commit_sha") == request.reviewed_head_sha
                and isinstance(root.get("review_id"), str)
                and bool(str(root["review_id"]).strip())
                and root.get("review_state") == "COMMENTED"
            )

        all_live_finding_identities = {
            identity
            for thread in live_by_id.values()
            if (identity := _finding_content_key(thread)) is not None
        }
        invalid_live_finding_identities = {
            identity
            for thread in live_by_id.values()
            if (identity := _finding_content_key(thread)) is not None
            and recovery_key(thread) is None
        }
        all_live_recoveries = {
            (identity, recovery)
            for thread in live_by_id.values()
            if (identity := _finding_content_key(thread)) is not None
            if (recovery := recovery_key(thread)) is not None
        }
        owned_live_finding_keys = {
            recovery
            for thread in live_by_id.values()
            if is_owned_exact_head_thread(thread) and (recovery := recovery_key(thread)) is not None
        }
        visible_pending_ids: set[str] = set()
        pending_keys: dict[str, tuple[object, str]] = {}
        for record in finding_records:
            if record["status"] != "pending":
                continue
            if record.get("publication_head", record["source_head"]) != request.reviewed_head_sha:
                return receipt("audit_failure")
            anchor = record["final_anchor"]
            if not isinstance(anchor, dict):
                return receipt("audit_failure")
            pending_key = recovery_key(
                {
                    "path": anchor["path"],
                    "line": anchor["line"],
                    "side": anchor["side"],
                    "body": record["body"],
                    "severity": record["severity"],
                    "scope_retraction_paths": record.get("scope_retraction_paths"),
                }
            )
            pending_identity = _finding_content_key(
                {
                    "path": anchor["path"],
                    "line": anchor["line"],
                    "side": anchor["side"],
                    "body": record["body"],
                }
            )
            if pending_key is None or pending_identity is None:
                return receipt("audit_failure")
            finding_id = str(record["finding_id"])
            pending_keys[finding_id] = pending_key
            if pending_identity in invalid_live_finding_identities:
                return receipt("audit_failure")
            conflicting_live_identity = any(
                identity == pending_identity and recovery != pending_key
                for identity, recovery in all_live_recoveries
            )
            if conflicting_live_identity:
                return receipt("audit_failure")
            if pending_key in owned_live_finding_keys:
                visible_pending_ids.add(finding_id)
            elif pending_identity in all_live_finding_identities:
                return receipt("audit_failure")
        if visible_pending_ids and not publication_only:
            finding_records = normalize_review_finding_records(
                [
                    {
                        **record,
                        "status": (
                            "corrected"
                            if str(record["finding_id"]) in visible_pending_ids
                            and record["status"] == "pending"
                            and record["reason"] is not None
                            else "published"
                            if str(record["finding_id"]) in visible_pending_ids
                            and record["status"] == "pending"
                            else record["status"]
                        ),
                    }
                    for record in finding_records
                ]
            )
        inline_records = {
            str(record["finding_id"]): record
            for record in finding_records
            if record["surface"] == "inline"
        }
        finding_ids = [str(finding.get("finding_id") or "") for finding in raw_findings]
        findings_by_id = {str(finding.get("finding_id") or ""): finding for finding in raw_findings}
        missing_pending_ids = pending_finding_ids - visible_pending_ids
        if publication_only and missing_pending_ids and not review_diff:
            return receipt("revalidate")
        if (
            len(set(finding_ids)) != len(finding_ids)
            or not set(finding_ids).issubset(inline_records)
            or not missing_pending_ids.issubset(finding_ids)
            or any(not _is_postable_finding(finding) for finding in raw_findings)
        ):
            return receipt("audit_failure")
        for finding_id in missing_pending_ids:
            finding = findings_by_id[finding_id]
            record = inline_records[finding_id]
            anchor = record["final_anchor"]
            finding_scope_value = finding.get("scope_retraction_paths")
            finding_scope = (
                ()
                if finding_scope_value is None
                else normalize_scope_retraction_paths(finding_scope_value)
            )
            record_scope_value = record.get("scope_retraction_paths")
            record_scope = (
                ()
                if record_scope_value is None
                else normalize_scope_retraction_paths(record_scope_value)
            )
            if (
                not isinstance(anchor, dict)
                or finding_scope is None
                or record_scope is None
                or finding_scope != record_scope
                or recovery_key(finding) != pending_keys[finding_id]
                or str(finding.get("path") or "").strip() != anchor["path"]
                or finding.get("line") != anchor["line"]
                or str(finding.get("side") or "").strip().upper() != anchor["side"]
                or str(finding.get("severity") or "").strip().lower() != record["severity"]
                or str(finding.get("body") or "").strip() != record["body"]
                or str(finding.get("evidence") or "").strip() != str(record.get("evidence") or "")
            ):
                return receipt("audit_failure")
        findings_to_validate = (
            [
                finding
                for finding in raw_findings
                if str(finding.get("finding_id") or "") in missing_pending_ids
            ]
            if publication_only
            else raw_findings
        )
        validation = _validate_comments_to_diff(
            findings_to_validate,
            review_diff,
            preserve_finding_ids=True,
        )
        if validation.corrections or len(validation.valid) != len(findings_to_validate):
            return receipt("audit_failure")
        try:
            findings = _without_duplicate_live_findings(list(validation.valid), live_by_id)
        except ValueError:
            return receipt("audit_failure")
        posting_ids = {str(finding["finding_id"]) for finding in findings}
        prepublication_records = normalize_review_finding_records(
            [
                {
                    **record,
                    "status": "pending",
                }
                if record["surface"] == "inline" and str(record["finding_id"]) in posting_ids
                else record
                for record in finding_records
            ]
        )
        if not publication_only:
            github.persist_review_finding_journal(
                request.pr_number,
                request.reviewed_head_sha,
                (
                    review_finding_collection_payload(prepublication_records, compacted_outcomes)
                    if compacted_outcomes["identities"]
                    else prepublication_records
                ),
            )
        publication = (
            github.post_review_threads(
                request.pr_number,
                findings,
                expected_head_sha=request.reviewed_head_sha,
                review_diff=review_diff,
            )
            if findings
            else []
        )
        posted_receipts = list(publication)
        raw_corrections = getattr(publication, "corrections", ())
        raw_unpublishable = getattr(publication, "unpublishable", raw_corrections)
        corrections = correction_records(raw_corrections)
        unpublishable = correction_records(raw_unpublishable)
        if corrections is None or unpublishable is None:
            return receipt("audit_failure")
        validated_findings = getattr(publication, "validated_findings", findings)
        if not isinstance(validated_findings, (list, tuple)) or len(posted_receipts) != len(
            validated_findings
        ):
            return receipt(
                "audit_failure",
                corrections=corrections,
                unpublishable=unpublishable,
            )
        if publication_only:
            proven_ids = posting_ids | visible_pending_ids
            proven_outcomes = {
                str(record["finding_id"]): (
                    "corrected" if record["reason"] is not None else "published"
                )
                for record in prepublication_records
                if str(record["finding_id"]) in proven_ids and record["status"] == "pending"
            }
            final_finding_records, compacted_outcomes = compact_terminal_review_finding_collection(
                prepublication_records,
                compacted_outcomes,
                proven_outcomes=proven_outcomes,
            )
        else:
            final_finding_records = normalize_review_finding_records(
                [
                    {
                        **record,
                        "status": ("corrected" if record["reason"] is not None else "published"),
                    }
                    if str(record["finding_id"]) in posting_ids and record["status"] == "pending"
                    else record
                    for record in prepublication_records
                ]
            )
        if publication_only or prepublication_records != final_finding_records:
            github.persist_review_finding_journal(
                request.pr_number,
                request.reviewed_head_sha,
                (
                    review_finding_collection_payload(final_finding_records, compacted_outcomes)
                    if publication_only or compacted_outcomes["identities"]
                    else final_finding_records
                ),
            )
        live_threads = github.list_unresolved_review_threads(request.pr_number)
        remediation_threads = _normalize_remediation_threads(live_threads)
        if len(remediation_threads) != len(live_threads):
            return receipt("audit_failure")
        return receipt(
            "apply",
            posted=posted_receipts,
            unresolved=live_threads,
            remediation=remediation_threads,
            corrections=corrections,
            unpublishable=unpublishable,
            final_finding_records=final_finding_records,
            final_compacted_outcomes=compacted_outcomes,
        )

    @staticmethod
    def _run_merge_wait_cycle(  # noqa: C901
        request: RunMergeWaitCycleRequest,
        github: Any,
    ) -> MergeWaitCycleCompleted:
        """Run admission, readiness, one conditional merge, and reconciliation."""
        requestable = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})
        retryable = frozenset({"BEHIND", "BLOCKED", "UNKNOWN"})
        conflicting = frozenset({"CONFLICTING", "DIRTY"})
        terminal_merge_sha: str | None = None

        def complete(
            outcome: str,
            *,
            attempted: bool = False,
            fingerprint: tuple[str, ...] | None = None,
            can_retry: bool = False,
            merge_sha: str | None = None,
        ) -> MergeWaitCycleCompleted:
            return MergeWaitCycleCompleted(
                request=request,
                outcome=outcome,
                attempted=attempted,
                readiness_fingerprint=fingerprint,
                retryable=can_retry,
                merge_sha=merge_sha,
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

        def rebase_record_outcome() -> str | None:
            """Reject a changed record or initial audit before merge admission."""
            if request.rebase_record is None:
                return None
            try:
                live_record = github.read_review_rebase_record(request.pr_number)
            except Exception:
                return "rebase_review_record_changed"
            return None if live_record == request.rebase_record else "rebase_review_record_changed"

        def admit(
            initial: MergeWaitAdmissionSnapshot | None = None,
        ) -> tuple[dict[str, object], MergeWaitAdmissionSnapshot] | str:
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
            if state.get("autoMergeRequest") is not None:
                return "auto_merge_already_armed"
            if state.get("state") != "OPEN" or "autoMergeRequest" not in state:
                return "pr_state_unverified"
            try:
                has_go, has_no_go = github.pr_has_implementation_state_label(request.pr_number)
            except Exception:
                return "implementation_state_unavailable"
            if not has_go or has_no_go:
                return "not_implementation_go"
            try:
                repository = github.verified_repository_default_branch()
            except Exception:
                return "default_branch_unavailable"
            snapshot = validate_merge_wait_admission(
                state,
                repository,
                request.merge_head_sha,
                initial=initial,
            )
            if isinstance(snapshot, str):
                return snapshot
            return state, snapshot

        def conversation_safety(policy: object) -> str | None:
            try:
                threads = github.list_unresolved_review_threads(request.pr_number)
            except Exception:
                return "review_threads_unavailable"
            if threads:
                return "unresolved_review_threads"
            protected = getattr(policy, "conversation_resolution_enforced", None)
            return None if protected is True else "conversation_resolution_required"

        def policy_safety(
            policy: object,
            snapshot: MergeWaitAdmissionSnapshot,
        ) -> str | None:
            """Require one server-enforced merge route before mutable reads."""
            if not isinstance(policy, EffectiveMergePolicy):
                return "merge_policy_unavailable"
            if (
                policy.base_branch != snapshot.base_branch
                or policy.default_branch != snapshot.repository.default_branch
            ):
                return "merge_policy_unavailable"
            if any(
                isinstance(ruleset_id, bool) or not isinstance(ruleset_id, int) or ruleset_id <= 0
                for ruleset_id in policy.bypassable_ruleset_ids
            ):
                return "merge_policy_unavailable"
            if policy.bypassable_ruleset_ids and not policy.merge_queue_required:
                return "merge_policy_bypassable"
            if not policy.merge_queue_required and not policy.strict_update_enforced:
                return "merge_policy_not_strict"
            return conversation_safety(policy)

        def readiness_outcome(
            state: object,
            *,
            park_if_ready: bool,
            merge_queue_required: bool = False,
        ) -> tuple[str | None, tuple[str, ...] | None]:
            terminal_outcome = terminal(state)
            if terminal_outcome is not None:
                return terminal_outcome, None
            if state is None or not isinstance(state, dict):
                return "merge_readiness_unavailable", None
            if state.get("autoMergeRequest") is not None:
                return "auto_merge_already_armed", None
            readiness_head = state.get("headRefOid")
            if not isinstance(readiness_head, str) or not readiness_head:
                return "merge_readiness_unavailable", None
            status = str(state.get("mergeStateStatus") or "").upper()
            mergeable = str(state.get("mergeable") or "").upper()
            fingerprint = (
                readiness_head,
                str(request.proof_generation),
                mergeable,
                status,
            )
            if readiness_head != request.merge_head_sha:
                return "readiness_wait", fingerprint
            if status in requestable and mergeable == "MERGEABLE":
                if park_if_ready or request.declined_readiness_fingerprint == fingerprint:
                    return "readiness_wait", fingerprint
                return None, fingerprint
            if merge_queue_required and status == "BLOCKED" and mergeable == "MERGEABLE":
                return None, fingerprint
            if status in conflicting or mergeable == "CONFLICTING":
                return "merge_conflicting", fingerprint
            if status == "BEHIND":
                return "readiness_wait", fingerprint
            if status not in retryable and mergeable != "UNKNOWN":
                return "merge_readiness_unknown", fingerprint
            return "readiness_wait", fingerprint

        boundary = operation_boundary()
        if boundary is not None:
            return complete(boundary)
        admitted = admit()
        if isinstance(admitted, str):
            return complete(admitted, merge_sha=terminal_merge_sha)
        record_status = rebase_record_outcome()
        if record_status is not None:
            return complete(record_status)
        _, initial_snapshot = admitted
        if request.queue_admitted:
            return complete("merge_queue_wait")
        base_branch = initial_snapshot.base_branch
        try:
            policy = github.effective_merge_policy(
                request.pr_number,
                base_branch,
                deadline_s=request.deadline_s,
                cancellation=request.cancellation,
            )
        except Exception:
            policy = None
        if policy is None:
            return complete("merge_policy_unavailable")
        unsafe = policy_safety(policy, initial_snapshot)
        if unsafe is not None:
            return complete(unsafe)

        try:
            readiness = github.gh_pr_merge_readiness(request.pr_number)
        except Exception:
            return complete("merge_readiness_unavailable")
        readiness_status, fingerprint = readiness_outcome(
            readiness,
            park_if_ready=False,
            merge_queue_required=policy.merge_queue_required,
        )
        if readiness_status is not None:
            return complete(readiness_status, fingerprint=fingerprint)

        try:
            checks_green = github.required_checks_pass_for_head(
                request.merge_head_sha,
                policy,
                deadline_s=request.deadline_s,
                cancellation=request.cancellation,
            )
        except Exception:
            checks_green = False
        if checks_green is not True:
            return complete("required_checks_not_green")

        # Complete all mutable GitHub traversals before final admission. The
        # returned admission binds the immediate policy-selected request.
        try:
            current_policy = github.effective_merge_policy(
                request.pr_number,
                base_branch,
                deadline_s=request.deadline_s,
                cancellation=request.cancellation,
            )
        except Exception:
            current_policy = None
        if not isinstance(current_policy, EffectiveMergePolicy) or current_policy != policy:
            return complete("merge_policy_unavailable")
        unsafe = policy_safety(current_policy, initial_snapshot)
        if unsafe is not None:
            return complete(unsafe)

        record_status = rebase_record_outcome()
        if record_status is not None:
            return complete(record_status)

        boundary = operation_boundary()
        if boundary is not None:
            return complete(boundary)
        admitted = admit(initial_snapshot)
        if isinstance(admitted, str):
            return complete(admitted, merge_sha=terminal_merge_sha)
        final_state, _ = admitted

        try:
            result = github.merge_pr_if_head(
                request.pr_number,
                request.merge_head_sha,
                policy=current_policy,
                pull_request_id=final_state.get("id"),
                deadline_s=request.deadline_s,
                cancellation=request.cancellation,
            )
        except Exception:
            return complete("merge_request_transport_error", attempted=True, can_retry=True)
        if result.dry_run:
            return complete("conditional_merge_dry_run", attempted=True)
        if result.malformed:
            return complete("merge_result_malformed", attempted=True)
        if result.transport_error or result.status is None:
            admitted = admit(initial_snapshot)
            if isinstance(admitted, str):
                return complete(admitted, attempted=True, merge_sha=terminal_merge_sha)
            return complete("merge_not_ready", attempted=True, can_retry=True)
        if getattr(result, "queued", False):
            return complete("merge_queued", attempted=True)
        if result.status == 200:
            if result.body is None or result.body.get("merged") is not True:
                return complete("merge_not_merged", attempted=True)
            merge_sha = result.body.get("sha")
            if not (
                isinstance(merge_sha, str)
                and len(merge_sha) in (40, 64)
                and all(character in "0123456789abcdef" for character in merge_sha)
            ):
                # Older GitHub-compatible transports omit the merge SHA. The
                # non-wave path remains compatible; MergeWaitStage rejects
                # this result when a durable wave receipt requires the proof.
                merge_sha = None
            try:
                final_state = github.gh_pr_state(request.pr_number)
            except Exception:
                return complete("merge_reconciliation_unavailable", attempted=True)
            return complete(
                terminal(final_state) or "merge_not_merged",
                attempted=True,
                merge_sha=merge_sha,
            )
        if result.status == 409:
            admitted = admit(initial_snapshot)
            if isinstance(admitted, str):
                return complete(admitted, attempted=True, merge_sha=terminal_merge_sha)
            return complete("merge_409_without_head_drift", attempted=True)
        if result.status == 405:
            try:
                readiness = github.gh_pr_merge_readiness(request.pr_number)
            except Exception:
                return complete("merge_readiness_unavailable", attempted=True)
            terminal_outcome = terminal(readiness)
            if terminal_outcome is not None:
                return complete(terminal_outcome, attempted=True)
            if not isinstance(readiness, dict):
                return complete("merge_readiness_unavailable", attempted=True)
            if readiness.get("autoMergeRequest") is not None:
                return complete("auto_merge_already_armed", attempted=True)
            admitted = admit(initial_snapshot)
            if isinstance(admitted, str):
                return complete(admitted, attempted=True, merge_sha=terminal_merge_sha)
            readiness_status, fingerprint = readiness_outcome(readiness, park_if_ready=True)
            return complete(
                readiness_status or "readiness_wait",
                attempted=True,
                fingerprint=fingerprint,
            )
        return complete(f"merge_http_{result.status}", attempted=True)


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


def _read_adopted_remediation_state(
    request: InspectAdoptedRemediationPrStateRequest, github: StageGitHub
) -> AdoptedRemediationPrStateRead:
    """Require stable open origin PR facts around the complete thread read."""
    from hephaestus.automation.remediation_recovery import RemediationReviewInput

    def pins() -> tuple[str, str, str, bool, int | None]:
        state = github.gh_pr_state(request.pr_number)
        if not isinstance(state, dict):
            raise ValueError("adopted PR state is unavailable")
        lifecycle = state.get("state")
        head = state.get("headRefOid")
        branch = github.get_pr_head_branch(request.pr_number)
        writable = github.pr_head_is_writable(request.pr_number)
        carrier = github.find_pr_for_issue(request.issue_number)
        if type(writable) is not bool or type(carrier) is not int:
            raise ValueError("adopted PR identity is invalid")
        if (lifecycle, head, branch, writable, carrier) != (
            "OPEN",
            request.expected_head,
            request.branch,
            True,
            request.pr_number,
        ):
            raise ValueError("adopted PR identity changed")
        return "OPEN", request.expected_head, request.branch, True, request.pr_number

    before = pins()
    threads = github.list_unresolved_review_threads(request.pr_number)
    canonical = RemediationReviewInput.canonical_thread_snapshot(threads)
    if canonical != request.expected_thread_snapshot_json or pins() != before:
        raise ValueError("adopted PR or thread snapshot changed")
    return AdoptedRemediationPrStateRead(
        request.repository,
        request.issue_number,
        request.pr_number,
        request.branch,
        request.expected_head,
        "OPEN",
        True,
        canonical,
        True,
    )
