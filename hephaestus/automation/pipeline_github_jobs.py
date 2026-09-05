"""Production dispatcher for closed worker-owned GitHub operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, assert_never

from hephaestus.automation.pipeline.github_jobs import (
    AppendReplyJournalRequest,
    DeliverReplyHandoffRequest,
    EnsureScopeExpansionChildrenRequest,
    FrozenJson,
    GitHubJob,
    GitHubReceipt,
    MergeWaitCycleCompleted,
    PrReviewReconciled,
    ReconcilePrReviewRequest,
    ReconcileScopeExpansionDependenciesRequest,
    RecoverReplyJournalRequest,
    ReplyJournalAppended,
    ReplyJournalRecovered,
    RunMergeWaitCycleRequest,
    ScopeExpansionChildrenEnsured,
    ScopeExpansionDependenciesReconciled,
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
            case EnsureScopeExpansionChildrenRequest():
                return self._ensure_scope_expansion_children(job.request, github)
            case ReconcileScopeExpansionDependenciesRequest():
                return self._reconcile_scope_expansion_dependencies(job.request, github)
            case unknown:
                return assert_never(unknown)
        raise AssertionError("unreachable closed GitHub request dispatch")

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
        missing_projection = _without_duplicate_live_findings(projection, live_by_id)
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

        def operation_boundary() -> str | None:
            if request.cancellation.is_set():
                return "merge_cycle_cancelled"
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

        def conversation_safety(policy: object) -> str | None:
            try:
                threads = github.list_unresolved_review_threads(request.pr_number)
            except Exception:
                return "review_threads_unavailable"
            if threads:
                return "unresolved_review_threads"
            protected = getattr(policy, "conversation_resolution_enforced", None)
            return None if protected is True else "conversation_resolution_required"

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

        boundary = operation_boundary()
        if boundary is not None:
            return complete(boundary)
        admitted = admit()
        if isinstance(admitted, str):
            return complete(admitted)
        state, _ = admitted
        base_branch = state.get("baseRefName")
        if not isinstance(base_branch, str) or not base_branch:
            return complete("pr_state_unverified")
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
        unsafe = conversation_safety(policy)
        if unsafe is not None:
            return complete(unsafe)

        try:
            readiness = github.gh_pr_merge_readiness(request.pr_number)
        except Exception:
            return complete("merge_readiness_unavailable")
        readiness_status, fingerprint = readiness_outcome(readiness, park_if_ready=False)
        if readiness_status is not None:
            return complete(readiness_status, fingerprint=fingerprint)

        try:
            checks_green = github.required_checks_pass_for_head(
                request.reviewed_head_sha,
                policy,
                deadline_s=request.deadline_s,
                cancellation=request.cancellation,
            )
        except Exception:
            checks_green = False
        if checks_green is not True:
            return complete("required_checks_not_green")

        # Complete all mutable GitHub traversals before final admission. The
        # returned admission then binds the immediate conditional PUT.
        unsafe = conversation_safety(policy)
        if unsafe is not None:
            return complete(unsafe)

        boundary = operation_boundary()
        if boundary is not None:
            return complete(boundary)
        admitted = admit()
        if isinstance(admitted, str):
            return complete(admitted)

        try:
            result = github.merge_pr_if_head(
                request.pr_number,
                request.reviewed_head_sha,
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
