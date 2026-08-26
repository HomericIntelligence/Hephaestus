"""Production dispatcher for closed worker-owned GitHub operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, assert_never

from hephaestus.automation.merge_authorization import (
    MergeAuthorization,
    MergeAuthorizationStatus,
    resolve_merge_authorization,
)
from hephaestus.automation.pipeline.github_jobs import (
    AppendReplyJournalRequest,
    DeliverReplyHandoffRequest,
    FrozenJson,
    GitHubJob,
    GitHubReceipt,
    MergeWaitCycleCompleted,
    PrReviewReconciled,
    ReconcilePrReviewRequest,
    RecoverReplyJournalRequest,
    ReplyJournalAppended,
    ReplyJournalRecovered,
    RunMergeWaitCycleRequest,
)
from hephaestus.automation.pipeline.reply_handoff import (
    attempt_reply_handoff,
    journaled_implementation_reply_handoff,
)
from hephaestus.automation.pipeline.stages.base import StageGitHub
from hephaestus.automation.pipeline_github import PipelineGitHub


@dataclass(frozen=True)
class PipelineGitHubJobRunner:
    """Dispatch closed requests through a fresh repository-scoped accessor."""

    org: str
    dry_run: bool
    gh_timeout: int = 120

    def run(self, job: GitHubJob) -> GitHubReceipt:
        """Execute one request without sharing a coordinator/client instance."""
        github: StageGitHub = PipelineGitHub(
            self.org,
            repo=job.repo,
            dry_run=self.dry_run,
            repo_root=job.repo_root,
            gh_timeout=self.gh_timeout,
        )
        match job.request:
            case RecoverReplyJournalRequest():
                threads = job.request.threads.thaw()
                if not isinstance(threads, list):  # constructor guard; keeps narrowing explicit
                    raise ValueError("recovery threads must be a list")
                handoff = journaled_implementation_reply_handoff(
                    github.issue_comments(job.request.issue_number),
                    pr_number=job.request.pr_number,
                    threads=threads,
                )
                return ReplyJournalRecovered(
                    request=job.request,
                    handoff=FrozenJson.snapshot(handoff) if handoff is not None else None,
                )
            case AppendReplyJournalRequest():
                github.append_issue_comment(
                    job.request.issue_number,
                    job.request.marker,
                    job.request.body,
                )
                return ReplyJournalAppended(request=job.request)
            case DeliverReplyHandoffRequest():
                return attempt_reply_handoff(job.request, github)
            case ReconcilePrReviewRequest():
                return self._reconcile_pr_review(job.request, github)
            case RunMergeWaitCycleRequest():
                return self._run_merge_wait_cycle(job.request, github)
            case unknown:
                return assert_never(unknown)
        raise AssertionError("unreachable closed GitHub request dispatch")

    @staticmethod
    def _reconcile_pr_review(  # noqa: C901
        request: ReconcilePrReviewRequest,
        github: Any,
    ) -> PrReviewReconciled:
        """Run fresh receipt reconciliation, publication, and late-thread readback."""
        from hephaestus.automation.pipeline.stages.pr_review_threads import (
            _durable_thread_id,
            _is_postable_finding,
            _normalize_remediation_threads,
            _validation_pr_metadata_fingerprint,
            _validation_receipt_fingerprints,
            _without_duplicate_live_findings,
        )
        from hephaestus.automation.prompts.pr_review import BLOCKING_SEVERITIES

        def receipt(
            action: str,
            *,
            posted: Any = (),
            unresolved: Any = (),
            remediation: Any = (),
        ) -> PrReviewReconciled:
            return PrReviewReconciled(
                request=request,
                action=action,  # type: ignore[arg-type]
                posted_receipts=FrozenJson.snapshot(list(posted)),
                unresolved_threads=FrozenJson.snapshot(list(unresolved)),
                remediation_threads=FrozenJson.snapshot(list(remediation)),
            )

        live_for_reconciliation = github.list_unresolved_review_threads(request.pr_number)
        validation_receipts = github.reviewer_validation_receipts(
            request.pr_number,
            reviewed_head_sha=request.reviewed_head_sha,
            threads=live_for_reconciliation,
        )
        pr_context = github.pr_review_context(request.pr_number)
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
                or not all(isinstance(value, str) and value.strip() for value in feedback.values())
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
        findings = _without_duplicate_live_findings(raw_findings, live_by_id)
        findings = [
            finding
            for finding in findings
            if str(finding.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES
        ]
        if any(not _is_postable_finding(finding) for finding in findings):
            return receipt("audit_failure")
        posted_receipts = (
            list(
                github.post_review_threads(
                    request.pr_number,
                    findings,
                    expected_head_sha=request.reviewed_head_sha,
                    review_diff=request.review_diff,
                )
            )
            if findings
            else []
        )
        if len(posted_receipts) != len(findings):
            return receipt("audit_failure")
        live_threads = github.list_unresolved_review_threads(request.pr_number)
        remediation_threads = _normalize_remediation_threads(live_threads)
        if len(remediation_threads) != len(live_threads):
            return receipt("audit_failure")
        return receipt(
            "apply",
            posted=posted_receipts,
            unresolved=live_threads,
            remediation=remediation_threads,
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

        def admit() -> tuple[dict[str, object], str] | str:
            try:
                state = github.gh_pr_state(request.pr_number)
            except Exception:
                return "pr_state_unavailable"
            terminal_outcome = terminal(state)
            if terminal_outcome is not None:
                return terminal_outcome
            if state is None:
                return "pr_state_unavailable"
            if not isinstance(state, dict):
                return "pr_state_unverified"
            if state.get("autoMergeRequest") is not None:
                return "auto_merge_already_armed"
            if state.get("state") != "OPEN" or "autoMergeRequest" not in state:
                return "pr_state_unverified"
            if state.get("baseRefName") != "main":
                return "non_main_base"
            try:
                has_go, has_no_go = github.pr_has_implementation_state_label(request.pr_number)
            except Exception:
                return "implementation_state_unavailable"
            if not has_go or has_no_go:
                return "not_implementation_go"
            head = str(state.get("headRefOid") or "")
            if not head:
                return "missing_pr_head"
            if head != request.reviewed_head_sha:
                return "reviewed_head_drift"
            return state, head

        def conversation_safety(base_branch: str) -> str | None:
            try:
                threads = github.list_unresolved_review_threads(request.pr_number)
            except Exception:
                return "review_threads_unavailable"
            if threads:
                return "unresolved_review_threads"
            try:
                protected = github.base_branch_requires_conversation_resolution(
                    request.pr_number,
                    base_branch,
                )
            except Exception:
                return "conversation_resolution_unavailable"
            return None if protected else "conversation_resolution_required"

        def readiness_outcome(
            state: object,
            *,
            park_if_ready: bool,
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
            if readiness_head != request.reviewed_head_sha:
                return "readiness_wait", fingerprint
            if status in requestable and mergeable == "MERGEABLE":
                if park_if_ready or request.declined_readiness_fingerprint == fingerprint:
                    return "readiness_wait", fingerprint
                return None, fingerprint
            if status in conflicting or mergeable == "CONFLICTING":
                return "merge_conflicting", fingerprint
            if status == "BEHIND":
                return "post_review_rebase_required", fingerprint
            if status not in retryable and mergeable != "UNKNOWN":
                return "merge_readiness_unknown", fingerprint
            return "readiness_wait", fingerprint

        admitted = admit()
        if isinstance(admitted, str):
            return complete(admitted)
        state, _ = admitted
        base_branch = state.get("baseRefName")
        if not isinstance(base_branch, str) or not base_branch:
            return complete("pr_state_unverified")
        unsafe = conversation_safety(base_branch)
        if unsafe is not None:
            return complete(unsafe)

        def authorization() -> MergeAuthorization | str:
            """Resolve the trusted exact-head operator approval."""
            try:
                repository = github._repo_slug
                if not isinstance(repository, str) or not repository:
                    raise RuntimeError("repository identity is unavailable")
                resolution = resolve_merge_authorization(
                    github.merge_authorization_reviews(request.pr_number),
                    repository=repository,
                    pr_number=request.pr_number,
                    head_sha=request.reviewed_head_sha,
                    automation_login=github._viewer_login(),
                    permission_for_actor=github.repository_permission_for_actor,
                )
            except Exception:
                return "merge_authorization_unavailable"
            if resolution.status is not MergeAuthorizationStatus.AUTHORIZED:
                return f"merge_authorization_{resolution.status.value}"
            if resolution.authorization is None:
                return "merge_authorization_unavailable"
            return resolution.authorization

        initial_authorization = authorization()
        if isinstance(initial_authorization, str):
            return complete(initial_authorization)
        try:
            readiness = github.gh_pr_merge_readiness(request.pr_number)
        except Exception:
            return complete("merge_readiness_unavailable")
        readiness_status, fingerprint = readiness_outcome(readiness, park_if_ready=False)
        if readiness_status is not None:
            return complete(readiness_status, fingerprint=fingerprint)

        # Read all authority-bearing facts again immediately before the PUT.
        admitted = admit()
        if isinstance(admitted, str):
            return complete(admitted)
        state, _ = admitted
        base_branch = state.get("baseRefName")
        if not isinstance(base_branch, str) or not base_branch:
            return complete("pr_state_unverified")
        unsafe = conversation_safety(base_branch)
        if unsafe is not None:
            return complete(unsafe)

        final_authorization = authorization()
        if isinstance(final_authorization, str):
            return complete(final_authorization)
        if final_authorization != initial_authorization:
            return complete("merge_authorization_changed")

        try:
            result = github.merge_pr_if_head(
                request.pr_number,
                request.reviewed_head_sha,
                final_authorization,
            )
        except Exception:
            return complete("merge_request_transport_error", attempted=True, can_retry=True)
        if result.dry_run:
            return complete("conditional_merge_dry_run", attempted=True)
        if result.malformed:
            return complete("merge_result_malformed", attempted=True)
        if result.transport_error or result.status is None:
            admitted = admit()
            if isinstance(admitted, str):
                return complete(admitted, attempted=True)
            return complete("merge_not_ready", attempted=True, can_retry=True)
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
            admitted = admit()
            if isinstance(admitted, str):
                return complete(admitted, attempted=True)
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
            admitted = admit()
            if isinstance(admitted, str):
                return complete(admitted, attempted=True)
            readiness_status, fingerprint = readiness_outcome(readiness, park_if_ready=True)
            return complete(
                readiness_status or "readiness_wait",
                attempted=True,
                fingerprint=fingerprint,
            )
        return complete(f"merge_http_{result.status}", attempted=True)
