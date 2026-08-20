"""Planning stage: generate and verify issue plans (issue #1814).

The single planning control flow as a pipeline stage
(docs/architecture.md §5.2 "planning" is the binding
contract). Since ``hephaestus-plan-issues`` was re-pointed at the pipeline
(#1820) this stage — with :class:`~...plan_review.PlanReviewStage` — is the
only issue-planning implementation:

- States: ENTER -> ADVISE_WAIT -> PLAN_WAIT -> VERIFY.
- Budget: ``plan`` = 2 (max plan attempts per issue); exhaustion ->
  finished(fail).
- Owned label: ``state:needs-plan`` (idempotent, on entry) [durable].
- Plan comment: the PIPELINE posts it (doc section 2: "plan comment =
  durable artifact"). VERIFY upserts ``item.payload["plan_text"]`` via
  ``ctx.github.upsert_plan_comment`` BEFORE the verify/ADVANCE decision
  (journal order: durable write precedes the queue push). The body is
  normalized with an opaque canonical marker by :func:`_normalize_plan_comment`.
  (The legacy content-missing banner and
  "Changes from review" enrichment were dropped with the legacy loop in
  #1820; the pipeline does not apply them.)
- Prompt functions (imported, never re-authored):
  ``prompts/advise.py get_advise_prompt_builder`` and
  ``prompts/planning.py get_plan_prompt`` (composed with the advise
  findings block by :func:`build_plan_prompt`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.agent_config import (
    advise_claude_timeout,
    advise_model,
    plan_reviewer_claude_timeout,
    planner_claude_timeout,
    planner_model,
    reviewer_model,
)
from hephaestus.automation.pipeline.summary import record_summary_action
from hephaestus.automation.prompts._shared import fence_content
from hephaestus.automation.prompts.planning import get_plan_prompt
from hephaestus.automation.protocol import PLAN_REVIEW_CANONICAL_MARKER
from hephaestus.automation.requirements_recovery import (
    OBSOLETE_EXPLANATION_MARKER,
    RECOVERY_PROVENANCE_PREFIX,
    RecoveredRequirements,
    RecoveryDisposition,
    RecoveryReview,
    RecoveryVerdict,
    build_recovery_prompt,
    build_recovery_review_prompt,
    evidence_digest,
    has_contaminated_issue_body,
    is_semantic_disposition_candidate,
    parse_recovered_requirements,
    parse_recovery_provenance,
    parse_recovery_review,
    recovered_requirements_for_source,
    recovered_requirements_json,
    render_obsolete_explanation,
    render_recovered_requirements,
)
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    IssueComment,
    JournalSnapshot,
    PlanDiscoveryStatus,
    current_revision_context,
    is_pending_review,
    journal_snapshot,
    plan_fingerprint,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.session_naming import AGENT_PLAN_REVIEWER, AGENT_PLANNER
from hephaestus.automation.state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    ALL_STATE_LABELS,
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
    apply_plan_state,
    enter_planning_transition,
    is_exclusive_plan_state,
    is_plan_go,
    is_skipped,
)
from hephaestus.prompts import PromptCatalog

from ..plan_journal import (
    PlanRevisionOwnershipError,
    publish_plan_revision,
    reconcile_plan_journal,
)
from .base import (
    AgentJob,
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
    Continue,
    Disposition,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _require_issue_labels,
    agent_provider,
    source_workspace_binding,
    stage_model,
)

logger = logging.getLogger(__name__)


def _closed_issue_entry_outcome(item: WorkItem, ctx: StageContext) -> StageOutcome | None:
    """Fail closed on malformed state and terminalize a fresh close/merge race."""
    if item.issue is None:  # Defensive; on_enter rejects this before calling.
        return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
    issue_snapshot = ctx.github.gh_issue_json(item.issue)
    if not isinstance(issue_snapshot, dict) or issue_snapshot.get("number") != item.issue:
        logger.error("planning:%d: malformed issue snapshot", item.issue)
        return StageOutcome(Disposition.FINISH_FAIL, "malformed issue snapshot")
    issue_state = issue_snapshot.get("state")
    if not isinstance(issue_state, str) or issue_state.upper() not in {"OPEN", "CLOSED"}:
        logger.error("planning:%d: malformed issue state", item.issue)
        return StageOutcome(Disposition.FINISH_FAIL, "malformed issue snapshot state")
    if issue_state.upper() == "OPEN":
        return None
    merged_pr = ctx.github.find_merged_pr_for_issue(item.issue)
    if merged_pr is None:
        logger.error("planning:%d: closed without an exact merged closing PR", item.issue)
        return StageOutcome(
            Disposition.FINISH_FAIL,
            "closed issue has no exact merged closing PR",
        )
    logger.info("planning:%d: closed by merged PR #%d; finishing", item.issue, merged_pr)
    return StageOutcome(Disposition.FINISH_PASS, f"closed by merged PR #{merged_pr}")


def build_plan_prompt(
    issue_number: int,
    issue_title: str = "",
    issue_body: str = "",
    advise_findings: str = "",
    issue_history: str = "",
) -> str:
    """Compose the plan prompt with the issue TASK and advise-findings block.

    Module-level composed builder (NOT a closure): :class:`AgentJob` is
    frozen and prompt builders run in-worker, so the builder must be a
    top-level function receiving everything via ``prompt_kwargs``. Appends a
    "Prior Learnings from Team Knowledge Base" block when advise findings are
    available; the prompt template itself is reused verbatim via
    :func:`get_plan_prompt`.

    Args:
        issue_number: GitHub issue number to plan.
        issue_title: Source issue title.
        issue_body: Source issue body.
        advise_findings: Advise-step findings; empty string means no block.

    Returns:
        The full planner prompt, with the findings block appended when
        ``advise_findings`` is non-empty.

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "planning/context.j2",
        untrusted_notice=fenced.untrusted_notice,
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title or f"Issue #{issue_number}"),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        advise_findings_block=(
            fenced.fence("ADVISE_FINDINGS", advise_findings) if advise_findings else ""
        ),
        issue_history_block=(fenced.fence("ISSUE_HISTORY", issue_history) if issue_history else ""),
        plan_prompt=get_plan_prompt(issue_number),
    )


def _planning_history(comments: Sequence[IssueComment | str]) -> str:
    """Return only current rejected-revision context for a resumed planner."""
    return current_revision_context(comments)


def _refresh_requirements_recovery_context(
    item: WorkItem,
    ctx: StageContext,
) -> bool:
    """Refresh exact issue evidence and return whether semantic recovery is needed."""
    assert item.issue is not None  # noqa: S101 - planning entry validates this
    snapshot = ctx.github.gh_issue_json(item.issue)
    title = snapshot.get("title")
    body = snapshot.get("body")
    body_digest = snapshot.get("bodyDigest")
    if not (
        isinstance(title, str)
        and isinstance(body, str)
        and isinstance(body_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", body_digest)
    ):
        raise RuntimeError("issue requirements snapshot is incomplete")
    contaminated = has_contaminated_issue_body(body)
    recovered = next(
        (
            restored
            for comment in reversed(ctx.github.issue_comments(item.issue))
            if comment.viewer_did_author
            and (restored := recovered_requirements_for_source(comment.body, body_digest))
            is not None
        ),
        None,
    )
    item.payload["issue_source_body"] = body
    if recovered is not None:
        item.payload["issue_body"] = recovered
        item.payload["requirements_recovered_comment"] = True
        item.payload["requires_plan_revision"] = True
        contaminated = False
    else:
        item.payload.pop("requirements_recovered_comment", None)
    semantic_candidate = recovered is None and is_semantic_disposition_candidate(title, body)
    semantic_already_cleared = item.payload.get("requirements_semantic_clear_digest") == body_digest
    required = contaminated or (semantic_candidate and not semantic_already_cleared)
    item.payload["issue_title"] = title
    if recovered is None:
        item.payload["issue_body"] = body
    item.payload["issue_body_digest"] = body_digest
    if required:
        item.payload["requirements_recovery_required"] = True
        item.payload["requirements_recovery_contaminated"] = contaminated
    else:
        item.payload.pop("requirements_recovery_required", None)
        item.payload.pop("requirements_recovery_contaminated", None)
    return required


def _reenter_planning(item: WorkItem) -> None:
    """Request a real stage re-entry on the coordinator's next retry."""
    item.state = "ENTER"
    item.payload["_enter_pending"] = True


def _retry_incomplete_requirements_snapshot(
    item: WorkItem,
    ctx: StageContext,
    reason: str,
) -> StageOutcome:
    """Bound incomplete entry reads and fail closed with plan-no-go."""
    assert item.issue is not None  # noqa: S101 - caller validates this
    live_labels = _require_issue_labels_for_transition(item.issue, ctx)
    if STATE_PLAN_BLOCKED in live_labels:
        return StageOutcome(Disposition.BLOCKED, "plan was blocked during requirements recovery")
    attempt = item.attempts.get("plan", 0) + 1
    item.attempts["plan"] = attempt
    if attempt < ctx.budget("plan"):
        _reenter_planning(item)
        return StageOutcome(
            Disposition.RETRY,
            f"requirements snapshot retry {attempt}/{ctx.budget('plan')}: {reason}",
        )
    if not _apply_recovery_state_label(item, ctx, STATE_PLAN_NO_GO):
        return StageOutcome(Disposition.RETRY, "plan-no-go label was not confirmed")
    return StageOutcome(
        Disposition.FINISH_FAIL,
        f"requirements snapshot exhausted with plan-no-go: {reason}",
    )


def _recovery_revision(item: WorkItem, workspace_revision: str | None) -> str:
    """Return the captured source revision used to bind recovery evidence."""
    candidates = (
        workspace_revision,
        item.payload.get("_impl_source_revision"),
        item.payload.get("_synced_default_branch_sha"),
        item.payload.get("_direct_scope_base_sha"),
    )
    return next(
        (value for value in candidates if isinstance(value, str) and len(value) == 40),
        "0" * 40,
    )


def _clear_recovery_results(item: WorkItem) -> None:
    """Discard one model/reviewer attempt without dropping refreshed evidence."""
    item.payload.pop("recovered_requirements", None)
    item.payload.pop("requirements_recovery_review", None)
    item.payload.pop("requirements_evidence_digest", None)


def _finish_recovery(item: WorkItem) -> None:
    """Clear transient recovery state after a durable disposition."""
    _clear_recovery_results(item)
    item.payload.pop("requirements_recovery_required", None)
    item.payload.pop("requirements_recovery_contaminated", None)


def _reset_plan_review_session(item: WorkItem, ctx: StageContext) -> None:
    """Prevent recovered requirements from resuming an obsolete review cycle."""
    reset_issues = getattr(ctx.config, "reset_plan_review_sessions", None)
    if isinstance(reset_issues, set) and item.issue is not None:
        reset_issues.add(item.issue)


def _apply_recovery_state_label(
    item: WorkItem,
    ctx: StageContext,
    target: str,
    *,
    extra: Sequence[str] = (),
) -> bool:
    """Atomically write and confirm one recovery-owned issue state.

    The caller checks fresh labels immediately before this mutation. A human
    can still change labels between that read and GitHub's atomic edit; that
    unavoidable external race is accepted and the post-write readback fails
    closed when the resulting state is not exclusive.
    """
    assert item.issue is not None  # noqa: S101 - caller validates this
    if target == STATE_SKIP:
        removals = [
            STATE_NEEDS_PLAN,
            STATE_PLAN_NO_GO,
            STATE_PLAN_GO,
            *ALL_IMPLEMENTATION_STATE_LABELS,
        ]
    else:
        _add, removals = apply_plan_state(target)
        removals = [*removals, *ALL_IMPLEMENTATION_STATE_LABELS]
    ctx.github.edit_labels(
        item.issue,
        add=[target, *extra],
        remove=removals,
    )
    live_labels = _require_issue_labels_for_transition(item.issue, ctx)
    if target == STATE_SKIP:
        return STATE_SKIP in live_labels and not set(live_labels).intersection(ALL_STATE_LABELS)
    return is_exclusive_plan_state(live_labels, target) and not set(live_labels).intersection(
        ALL_IMPLEMENTATION_STATE_LABELS
    )


def _retry_requirements_recovery(
    item: WorkItem,
    ctx: StageContext,
    reason: str,
) -> StageOutcome:
    """Retry a correctable recovery once, then preserve plan-no-go."""
    assert item.issue is not None  # noqa: S101 - caller validates this
    live_labels = _require_issue_labels_for_transition(item.issue, ctx)
    if STATE_PLAN_BLOCKED in live_labels:
        return StageOutcome(Disposition.BLOCKED, "plan was blocked during requirements recovery")
    attempt = item.attempts.get("plan", 0) + 1
    item.attempts["plan"] = attempt
    if attempt < ctx.budget("plan"):
        _clear_recovery_results(item)
        item.state = "REQUIREMENTS_RECOVERY_WAIT"
        return StageOutcome(
            Disposition.RETRY,
            f"requirements recovery retry {attempt}/{ctx.budget('plan')}: {reason}",
        )
    if not _apply_recovery_state_label(item, ctx, STATE_PLAN_NO_GO):
        return StageOutcome(Disposition.RETRY, "plan-no-go label was not confirmed")
    return StageOutcome(
        Disposition.FINISH_FAIL,
        f"requirements recovery exhausted with plan-no-go: {reason}",
    )


def _apply_confirmed_semantic_skip(
    item: WorkItem,
    ctx: StageContext,
    proposal: RecoveredRequirements,
    review: RecoveryReview,
) -> StageOutcome | None:
    """Apply a tracker/obsolete skip only after matching independent GO."""
    assert item.issue is not None  # noqa: S101 - caller validates this
    if proposal.disposition is RecoveryDisposition.TRACKER:
        if not _apply_recovery_state_label(item, ctx, STATE_SKIP, extra=("epic",)):
            return StageOutcome(Disposition.RETRY, "tracker skip label was not confirmed")
        record_summary_action(item, "tracker-skipped")
        _finish_recovery(item)
        return StageOutcome(Disposition.SKIP, "skip: independently confirmed tracker")
    if proposal.disposition is RecoveryDisposition.OBSOLETE:
        logger.info(
            "planning:%d: independently confirmed obsolete issue; applying state:skip: %s",
            item.issue,
            review.reason,
        )
        ctx.github.upsert_issue_comment(
            item.issue,
            OBSOLETE_EXPLANATION_MARKER,
            render_obsolete_explanation(review.reason),
        )
        if not _apply_recovery_state_label(item, ctx, STATE_SKIP):
            return StageOutcome(Disposition.RETRY, "obsolete skip label was not confirmed")
        record_summary_action(item, "obsolete-skipped")
        _finish_recovery(item)
        return StageOutcome(Disposition.SKIP, "skip: independently confirmed obsolete")
    return None


def _pending_force_after_semantic_clear(
    item: WorkItem,
    ctx: StageContext,
    source_digest: object,
) -> StageOutcome | None:
    """Return through entry when a false semantic candidate preceded force."""
    force_pending = bool(getattr(ctx.config, "force", False)) and not bool(
        item.payload.get("forced_planning_epoch_started")
    )
    if not force_pending:
        return None
    if isinstance(source_digest, str):
        item.payload["requirements_semantic_clear_digest"] = source_digest
    _reenter_planning(item)
    return StageOutcome(
        Disposition.RETRY,
        "semantic recovery cleared; forced planning remains pending",
    )


def _apply_confirmed_requirements(
    item: WorkItem,
    ctx: StageContext,
    proposal: RecoveredRequirements,
) -> StepResult:
    """Publish confirmed requirements or resume normal planning for a false candidate."""
    assert item.issue is not None  # noqa: S101 - caller validates this
    contaminated = bool(item.payload.get("requirements_recovery_contaminated"))
    if not contaminated:
        source_digest = item.payload.get("issue_body_digest")
        _finish_recovery(item)
        if force_outcome := _pending_force_after_semantic_clear(item, ctx, source_digest):
            return force_outcome
        labels = _require_issue_labels_for_transition(item.issue, ctx)
        if is_exclusive_plan_state(labels, STATE_PLAN_GO):
            return StageOutcome(Disposition.ADVANCE, "existing plan remains approved")
        return Continue(next_state="ADVISE_WAIT" if ctx.config.enable_advise else "PLAN_WAIT")

    # A recovered comment is actor-owned; the source issue body is never edited
    # because GitHub has no compare-and-swap primitive for body replacement.
    if not _apply_recovery_state_label(item, ctx, STATE_PLAN_NO_GO):
        return StageOutcome(Disposition.RETRY, "recovery plan-no-go was not confirmed")
    source_body = str(item.payload.get("issue_source_body", item.payload.get("issue_body")) or "")
    source_digest = str(item.payload.get("issue_body_digest") or "")
    recovered_body = render_recovered_requirements(
        source_body,
        proposal.requirements,
        str(item.payload.get("requirements_evidence_digest") or ""),
        source_digest=source_digest,
    )
    ctx.github.upsert_issue_comment(item.issue, RECOVERY_PROVENANCE_PREFIX, recovered_body)
    item.payload["issue_body"] = proposal.requirements
    item.payload["requirements_recovered_comment"] = True
    item.payload["requires_plan_revision"] = True
    if bool(getattr(ctx.config, "force", False)):
        item.payload["forced_planning_epoch_started"] = True
    item.payload.pop("plan_text", None)
    item.payload.pop("issue_history", None)
    record_summary_action(item, "requirements-recovered")
    _reset_plan_review_session(item, ctx)
    _finish_recovery(item)
    return Continue(next_state="ADVISE_WAIT" if ctx.config.enable_advise else "PLAN_WAIT")


def _apply_requirements_recovery(
    item: WorkItem,
    ctx: StageContext,
) -> StepResult:
    """Apply only an independently confirmed recovery proposal."""
    assert item.issue is not None  # noqa: S101 - caller validates this
    proposal = item.payload.get("recovered_requirements")
    review = item.payload.get("requirements_recovery_review")
    if not isinstance(proposal, RecoveredRequirements) or not isinstance(review, RecoveryReview):
        return _retry_requirements_recovery(item, ctx, "missing typed recovery result")
    if STATE_PLAN_BLOCKED in _require_issue_labels_for_transition(item.issue, ctx):
        return StageOutcome(Disposition.BLOCKED, "plan was blocked during requirements recovery")
    expected_title = item.payload.get("issue_title")
    expected_body = item.payload.get("issue_source_body", item.payload.get("issue_body"))
    expected_digest = item.payload.get("issue_body_digest")
    if all(isinstance(value, str) for value in (expected_title, expected_body, expected_digest)):
        live = ctx.github.gh_issue_json(item.issue)
        if (
            live.get("title") != expected_title
            or live.get("body") != expected_body
            or live.get("bodyDigest") != expected_digest
        ):
            _clear_recovery_results(item)
            try:
                _refresh_requirements_recovery_context(item, ctx)
            except RuntimeError as exc:
                return _retry_incomplete_requirements_snapshot(item, ctx, str(exc))
            _reenter_planning(item)
            return StageOutcome(
                Disposition.RETRY,
                "issue requirements changed after recovery review; refreshed evidence",
            )
    if review.verdict is not RecoveryVerdict.GO or review.disposition is not proposal.disposition:
        return _retry_requirements_recovery(item, ctx, review.reason)
    if skip_outcome := _apply_confirmed_semantic_skip(item, ctx, proposal, review):
        return skip_outcome
    return _apply_confirmed_requirements(item, ctx, proposal)


def _requirements_recovery_step(item: WorkItem, ctx: StageContext) -> StepResult | None:
    """Build or apply one requirements-recovery substate action."""
    assert item.issue is not None  # noqa: S101 - PlanningStage.step validates this
    if item.state == "REQUIREMENTS_RECOVERY_WAIT":
        workspace = source_workspace_binding(item, ctx, SourceLane.IMPLEMENTATION)
        revision = _recovery_revision(item, workspace.revision if workspace is not None else None)
        binding = evidence_digest(
            item.repo,
            item.issue,
            revision,
            str(item.payload.get("issue_title") or ""),
            str(item.payload.get("issue_body") or ""),
        )
        item.payload["requirements_evidence_digest"] = binding
        job = AgentJob(
            repo=item.repo,
            issue=item.issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "planner", planner_model),
            prompt_builder=build_recovery_prompt,
            cwd=workspace.cwd if workspace else ctx.paths.worktree,
            timeout_s=planner_claude_timeout(),
            workspace=workspace,
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            session_agent=AGENT_PLANNER,
            session_key=f"requirements-recovery:{item.issue}:{binding}",
            execution_request=ExecutionRequest(
                AgentRole.PLANNER,
                AgentOperation.PLAN,
                SessionLifecycle.START_NEW,
            ),
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "repository": item.repo,
                "repository_revision": revision,
                "evidence_binding": binding,
            },
            parse=parse_recovered_requirements,
            descr="recover_requirements",
        )
        return JobRequest(job, on_done_state="REQUIREMENTS_RECOVERY_REVIEW_WAIT")

    if item.state == "REQUIREMENTS_RECOVERY_REVIEW_WAIT":
        proposal = item.payload.get("recovered_requirements")
        if not isinstance(proposal, RecoveredRequirements):
            return _retry_requirements_recovery(
                item,
                ctx,
                "requirements planner returned no valid proposal",
            )
        workspace = source_workspace_binding(item, ctx, SourceLane.REVIEW)
        revision = _recovery_revision(item, workspace.revision if workspace is not None else None)
        binding = str(item.payload.get("requirements_evidence_digest") or "")
        job = AgentJob(
            repo=item.repo,
            issue=item.issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=build_recovery_review_prompt,
            cwd=workspace.cwd if workspace else ctx.paths.worktree,
            timeout_s=plan_reviewer_claude_timeout(),
            workspace=workspace,
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            session_agent=AGENT_PLAN_REVIEWER,
            session_key=f"requirements-recovery-review:{item.issue}:{binding}",
            execution_request=ExecutionRequest(
                AgentRole.PLAN_REVIEWER,
                AgentOperation.PLAN_REVIEW,
                SessionLifecycle.START_NEW,
            ),
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "source_body_digest": item.payload.get("issue_body_digest", ""),
                "evidence_binding": binding,
                "proposal_json": recovered_requirements_json(proposal),
                "repository": item.repo,
                "repository_revision": revision,
            },
            parse=parse_recovery_review,
            descr="review_recovered_requirements",
        )
        return JobRequest(job, on_done_state="REQUIREMENTS_RECOVERY_APPLY")

    if item.state == "REQUIREMENTS_RECOVERY_APPLY":
        return _apply_requirements_recovery(item, ctx)
    return None


def _normalize_plan_comment(plan: str, *, revision: int | None = None) -> str:
    """Render the canonical plan comment with its opaque ownership marker."""
    return render_current_plan(plan, revision=revision or 1)


def _is_replan_entry(
    labels: Sequence[str],
    *,
    revision_already_published: bool,
) -> bool:
    """Read replan authorization from labels, never from review prose."""
    return STATE_PLAN_NO_GO in labels and not revision_already_published


def _load_planning_journal(
    item: WorkItem,
    ctx: StageContext,
    labels: Sequence[str],
) -> tuple[list[IssueComment], JournalSnapshot, bool, bool]:
    """Reconcile GitHub, hydrate the work item, and recover replan intent."""
    assert item.issue is not None  # noqa: S101 - on_enter validates the work item first
    comments = reconcile_plan_journal(item.issue, ctx.github)
    snapshot = journal_snapshot(comments)
    if snapshot.current_plan:
        item.payload["plan_text"] = snapshot.current_plan
        item.payload["plan_revision"] = snapshot.revision
    revision_already_published = bool(
        snapshot.current_plan
        and snapshot.current_review_revision == snapshot.revision
        and is_pending_review(snapshot.current_review, revision=snapshot.revision)
    )
    is_replan_entry = _is_replan_entry(
        labels,
        revision_already_published=revision_already_published,
    )
    return (
        comments,
        snapshot,
        is_replan_entry,
        revision_already_published,
    )


def _recovered_successor_is_current(
    comments: Sequence[IssueComment],
    snapshot: JournalSnapshot,
    *,
    source_digest: str,
) -> bool:
    """Return whether recovery provenance binds this exact canonical successor.

    A recovery marker starts a fresh planning epoch.  It stops doing so only
    after its durable successor plan and paired pending review have both been
    recorded.  This lets a newly seeded work item resume review without
    trusting an unrelated canonical plan left by the contaminated epoch.
    """
    if not (
        snapshot.current_plan
        and snapshot.current_review_revision == snapshot.revision
        and is_pending_review(snapshot.current_review, revision=snapshot.revision)
    ):
        return False
    current_digest = plan_fingerprint(snapshot.current_plan)
    for comment in reversed(comments):
        if not comment.viewer_did_author:
            continue
        provenance = parse_recovery_provenance(comment.body)
        if provenance is None or provenance.source_digest != source_digest:
            continue
        return (
            provenance.successor_revision == snapshot.revision
            and provenance.successor_plan_digest == current_digest
        )
    return False


def _bind_recovered_successor(
    item: WorkItem,
    ctx: StageContext,
    *,
    plan: str,
    revision: int,
) -> bool:
    """Bind a published plan to its recovered-requirements provenance."""
    assert item.issue is not None  # noqa: S101 - caller validates the work item
    if not item.payload.get("requirements_recovered_comment"):
        return True
    source_digest = item.payload.get("issue_body_digest")
    source_body = item.payload.get("issue_source_body")
    if not isinstance(source_digest, str) or not isinstance(source_body, str):
        return False
    for comment in reversed(ctx.github.issue_comments(item.issue)):
        if not comment.viewer_did_author:
            continue
        provenance = parse_recovery_provenance(comment.body)
        requirements = recovered_requirements_for_source(comment.body, source_digest)
        if provenance is None or requirements is None:
            continue
        ctx.github.upsert_issue_comment(
            item.issue,
            RECOVERY_PROVENANCE_PREFIX,
            render_recovered_requirements(
                source_body,
                requirements,
                provenance.evidence_digest,
                source_digest=source_digest,
                successor_revision=revision,
                successor_plan_digest=plan_fingerprint(plan),
            ),
        )
        return True
    return False


def _plan_is_ready_for_verify(
    snapshot: JournalSnapshot,
    *,
    is_replan_entry: bool,
) -> bool:
    """Return whether restart may verify the canonical plan without another agent."""
    if is_replan_entry:
        return False
    return bool(snapshot.current_plan)


def _write_planning_entry_labels(
    issue_number: int,
    ctx: StageContext,
    labels: Sequence[str],
    *,
    is_replan_entry: bool,
    revision_already_published: bool,
    force_replan: bool = False,
) -> bool:
    """Durably establish the mutually-exclusive planning entry label."""
    expected_state = STATE_NEEDS_PLAN
    if force_replan and is_replan_entry:
        target, remove = apply_plan_state(STATE_PLAN_NO_GO)
        add = [target]
        expected_state = STATE_PLAN_NO_GO
        logger.info(
            "planning:%d: forced revision entry; add %s, remove %s",
            issue_number,
            add,
            remove,
        )
        ctx.github.edit_labels(issue_number, add=add, remove=remove)
    elif force_replan:
        add, remove = enter_planning_transition()
        logger.info("planning:%d: forced entry swap; add %s, remove %s", issue_number, add, remove)
        ctx.github.edit_labels(issue_number, add=add, remove=remove)
    elif STATE_PLAN_NO_GO in labels and revision_already_published:
        add, remove = enter_planning_transition()
        logger.info("planning:%d: entry swap; add %s, remove %s", issue_number, add, remove)
        ctx.github.edit_labels(issue_number, add=add, remove=remove)
    elif is_replan_entry:
        # Keep state:plan-no-go authoritative until a revised canonical plan
        # has actually been published. This removes the crash window where a
        # needs-plan label plus stale rejected plan could be mistaken for a
        # fresh initial planning entry.
        return is_exclusive_plan_state(
            _require_issue_labels_for_transition(issue_number, ctx), STATE_PLAN_NO_GO
        )
    elif STATE_NEEDS_PLAN not in labels or set(labels).intersection(
        ALL_IMPLEMENTATION_STATE_LABELS
    ):
        add, remove = enter_planning_transition()
        logger.info(
            "planning:%d: entering initial planning; add %s, remove legacy states %s",
            issue_number,
            add,
            remove,
        )
        ctx.github.edit_labels(issue_number, add=add, remove=remove)
    if ctx.dry_run:
        return True
    return is_exclusive_plan_state(
        _require_issue_labels_for_transition(issue_number, ctx),
        expected_state,
    )


def _require_issue_labels_for_transition(issue_number: int, ctx: StageContext) -> list[str]:
    """Read fresh label names when confirming a durable state transition."""
    data = ctx.github.gh_issue_json(issue_number)
    return [
        str(label.get("name")) if isinstance(label, dict) else str(label)
        for label in data.get("labels", [])
        if isinstance(label, (dict, str))
    ]


def _mark_published_plan_pending_review(
    issue_number: int,
    ctx: StageContext,
    *,
    was_revision: bool,
) -> bool:
    """Transition a rejected plan only after its replacement is durable."""
    if was_revision:
        add, remove = enter_planning_transition()
        ctx.github.edit_labels(issue_number, add=add, remove=remove)
    return is_exclusive_plan_state(
        _require_issue_labels_for_transition(issue_number, ctx),
        STATE_NEEDS_PLAN,
    )


def _publish_plan_blocked(
    issue_number: int,
    ctx: StageContext,
    *,
    raw_review: str,
    revision: int,
) -> bool:
    """Latch BLOCKED before publishing its required explanatory audit data."""
    ctx.github.edit_labels(
        issue_number,
        add=[STATE_PLAN_BLOCKED],
        remove=[STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO],
    )
    confirmed = is_exclusive_plan_state(
        _require_issue_labels_for_transition(issue_number, ctx),
        STATE_PLAN_BLOCKED,
    )
    if not confirmed:
        return False
    ctx.github.upsert_issue_comment(
        issue_number,
        PLAN_REVIEW_CANONICAL_MARKER,
        render_current_review(raw_review, revision=revision),
    )
    return True


def _no_progress_outcome(
    item: WorkItem,
    ctx: StageContext,
    *,
    reason: str,
    revision: int,
) -> StageOutcome:
    """Persist one no-progress explanation and route solely from its label."""
    assert item.issue is not None  # noqa: S101 - caller narrows the issue
    raw_review = f"Planning is stuck and needs external feedback. {reason}\n\n{STATE_PLAN_BLOCKED}"
    if not _publish_plan_blocked(
        item.issue,
        ctx,
        raw_review=raw_review,
        revision=revision,
    ):
        return StageOutcome(Disposition.RETRY, "blocked label was not confirmed")
    return StageOutcome(
        Disposition.BLOCKED,
        "planning made no progress; external feedback required",
    )


def _publish_candidate_plan(
    item: WorkItem,
    ctx: StageContext,
    *,
    requires_revision: bool,
) -> StageOutcome | None:
    """Publish one plan transaction and confirm its pending-review label state."""
    assert item.issue is not None  # noqa: S101 - caller validates the issue
    live_labels = _require_issue_labels_for_transition(item.issue, ctx)
    if STATE_PLAN_BLOCKED in live_labels:
        return StageOutcome(
            Disposition.BLOCKED,
            "plan was blocked externally while planning was in flight",
        )
    try:
        publication = publish_plan_revision(
            item.issue,
            str(item.payload["plan_text"]),
            ctx.github,
            require_change=requires_revision,
            forced_planning_epoch=bool(item.payload.get("forced_planning_epoch_started")),
        )
    except PlanRevisionOwnershipError:
        note = "plan is being worked by another pipeline item; ejected from queue"
        logger.info("planning:%d: %s", item.issue, note)
        return StageOutcome(Disposition.FINISH_PASS, note)
    item.payload["plan_text"] = publication.plan
    item.payload["plan_revision"] = publication.revision
    if publication.is_stuck:
        return _no_progress_outcome(
            item,
            ctx,
            reason=publication.no_progress_reason,
            revision=publication.revision,
        )
    if not _bind_recovered_successor(
        item,
        ctx,
        plan=publication.plan,
        revision=publication.revision,
    ):
        return StageOutcome(
            Disposition.RETRY,
            "recovered plan successor provenance was not confirmed",
        )
    if not _mark_published_plan_pending_review(
        item.issue,
        ctx,
        was_revision=requires_revision,
    ):
        return StageOutcome(
            Disposition.RETRY,
            "exclusive needs-plan label was not confirmed",
        )
    item.payload.pop("requires_plan_revision", None)
    return None


def _verify_plan(item: WorkItem, ctx: StageContext) -> StageOutcome:
    """Publish or recover the candidate plan, then authorize advancement by label."""
    assert item.issue is not None  # noqa: S101 - stage validates the issue
    lookup = ctx.github.discover_plan(item.issue)
    if lookup.status is PlanDiscoveryStatus.READ_ERROR:
        return StageOutcome(
            Disposition.RETRY,
            f"plan discovery failed: {lookup.error}",
        )

    initial_plan_found = lookup.status is PlanDiscoveryStatus.FOUND
    plan_text = item.payload.get("plan_text")
    awaiting_revision_candidate = bool(item.payload.get("requires_plan_revision")) and (
        plan_text is None
    )
    posted_plan = False
    if plan_text is not None:
        requires_revision = bool(item.payload.get("requires_plan_revision"))
        if requires_revision or lookup.status is PlanDiscoveryStatus.ABSENT:
            logger.info("planning:%d: publishing plan revision", item.issue)
            try:
                publication_outcome = _publish_candidate_plan(
                    item,
                    ctx,
                    requires_revision=requires_revision,
                )
            except CommentJournalReadError as exc:
                return StageOutcome(
                    Disposition.RETRY,
                    f"plan journal read failed: {exc}",
                )
            if publication_outcome is not None:
                return publication_outcome
            posted_plan = True

    # The initial lookup only decides whether publication is necessary.  Do
    # not authorize the handoff from that stale snapshot: another actor can
    # create or delete the canonical comment while VERIFY is running.
    verified_lookup = ctx.github.discover_plan(item.issue)
    if verified_lookup.status is PlanDiscoveryStatus.READ_ERROR:
        return StageOutcome(
            Disposition.RETRY,
            f"plan discovery failed: {verified_lookup.error}",
        )

    if not awaiting_revision_candidate and (
        posted_plan or verified_lookup.status is PlanDiscoveryStatus.FOUND
    ):
        labels = _require_issue_labels_for_transition(item.issue, ctx)
        if STATE_PLAN_BLOCKED in labels:
            return StageOutcome(
                Disposition.BLOCKED,
                "plan was blocked externally before verification",
            )
        if not is_exclusive_plan_state(labels, STATE_NEEDS_PLAN):
            return StageOutcome(
                Disposition.RETRY,
                "exclusive needs-plan label was not confirmed",
            )
        logger.info("planning:%d: plan verified; advancing", item.issue)
        return StageOutcome(Disposition.ADVANCE, "plan generated and verified")

    if posted_plan or initial_plan_found:
        return StageOutcome(Disposition.RETRY, "plan disappeared before verification")

    attempt = item.attempts.get("plan", 0) + 1
    item.attempts["plan"] = attempt
    budget = ctx.budget("plan")
    if attempt < budget:
        logger.warning(
            "planning:%d: plan comment not found; retry %d/%d",
            item.issue,
            attempt,
            budget,
        )
        item.state = "PLAN_WAIT"
        return StageOutcome(Disposition.RETRY, f"plan not found, retry {attempt}/{budget}")
    logger.error("planning:%d: plan not found after %d attempts; exhausted", item.issue, budget)
    return StageOutcome(Disposition.FINISH_FAIL, f"plan not found after {budget} attempts")


class PlanningStage(Stage):
    """Stage for planning an issue: advise -> plan -> verify.

    State machine (doc section "2. planning"):

    - ENTER: route to ADVISE_WAIT (or PLAN_WAIT when advise is disabled).
    - ADVISE_WAIT: submit the advise agent job; findings land in
      ``item.payload["advise_findings"]``.
    - PLAN_WAIT: submit the plan agent job (planner session); plan text
      lands in ``item.payload["plan_text"]``; the plan comment posted by the
      pipeline is the durable artifact.
    - VERIFY: check the plan comment exists -> ADVANCE, else reset to
      ``PLAN_WAIT`` and RETRY within the ``plan`` budget, then FINISH_FAIL.

    on_enter idempotency guards (re-housed from ``Planner._pr_coverage_skip``
    and the planner's tri-state plan discovery, all ordered at-or-past checks):

    - ``state:skip`` -> SKIP (checked BEFORE plan-go; skip wins over
      everything, even a contradictory plan-go, logging a WARNing — #1835)
    - already at-or-past ``state:plan-go`` -> ADVANCE (zero jobs)
    - freshly closed issue + exact merged closing PR -> FINISH_PASS
    - open issue + historic merged closing PR -> continue planning
    - open PR without plan-GO -> continue planning; the PR is not plan approval
    - unlabeled entry -> idempotent bare add of ``state:needs-plan``; entry
      carrying ``state:plan-no-go`` (or a stale ``state:plan-go``) after a
      plan_review fail-back -> ONE atomic ``edit_labels`` swap adding
      ``state:needs-plan`` and removing both siblings, so the labels-first
      plan-discovery gate can pass once a fresh plan comment is posted
      and the mutually-exclusive-label invariant holds (#1857)
    - plan comment already exists (``ctx.github.discover_plan`` returns FOUND) ->
      fast-forward ``item.state`` to VERIFY so a restart mid-stage never
      redoes advise + plan (the base-protocol idempotency promise); the
      ``is_plan_review_go`` label check above stays the primary gate.
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:  # noqa: C901
        """Refresh labels and perform idempotent fast-forward checks.

        Args:
            item: The work item (must have an issue number).
            ctx: Stage context with the GitHub accessor.

        Returns:
            None to proceed with step(), or a StageOutcome to skip/finish.

        """
        if not item.issue:
            logger.warning("planning: work item has no issue number")
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")

        if issue_outcome := _closed_issue_entry_outcome(item, ctx):
            return issue_outcome

        labels = _require_issue_labels(item, ctx)
        force_replan = bool(getattr(ctx.config, "force", False)) and not bool(
            item.payload.get("forced_planning_epoch_started")
        )

        # Operator override: state:skip -> SKIP. Checked BEFORE the
        # plan-go fast-forward — skip wins over everything (#1835), matching
        # seeding.classify_issue's "skip wins over everything" precedent.
        if is_skipped(labels):
            if is_plan_go(labels):
                logger.warning(
                    "planning:%d: state:skip AND state:plan-go both present — "
                    "skip wins; see docs/runbooks/state-skip-revival.md if "
                    "this issue should be revived",
                    item.issue,
                )
            logger.info("planning:%d: state:skip; skipping", item.issue)
            return StageOutcome(Disposition.SKIP, "state:skip")

        # BLOCKED is an operator-owned hold. Automation never clears or
        # replaces it, even when later comments supply the missing decision.
        # An external actor must resolve the dependency and set exactly one
        # next state label before the issue becomes eligible again.
        if STATE_PLAN_BLOCKED in labels:
            ctx.github.ensure_blocked_audit(item.issue)
            return StageOutcome(Disposition.BLOCKED, "plan requires external intervention")

        # Derived automation artifacts in the issue body are not requirements.
        # Semantic tracker/obsolete candidates use the same two-model gate.
        # This check precedes plan-GO so stale approval cannot authorize a body
        # that is itself a copied plan or review.
        try:
            recovery_required = _refresh_requirements_recovery_context(item, ctx)
        except CommentJournalReadError as exc:
            return _retry_incomplete_requirements_snapshot(item, ctx, str(exc))
        except RuntimeError as exc:
            return _retry_incomplete_requirements_snapshot(item, ctx, str(exc))
        if recovery_required:
            logger.info("planning:%d: requirements recovery required", item.issue)
            return None

        # Fast-forward only from the sole confirmed plan state. A stale sibling
        # makes the label set contradictory and must never authorize work.
        if is_exclusive_plan_state(labels, STATE_PLAN_GO) and not force_replan:
            logger.info("planning:%d: already plan-go; advancing", item.issue)
            return StageOutcome(Disposition.ADVANCE, "plan already approved")

        # Entry label normalization. On the plan_review "nogo" fail-back the
        # issue carries state:plan-no-go and NEITHER sibling (apply_plan_verdict
        # ADDS no-go, removing needs-plan/plan-go). A bare add of needs-plan
        # would leave state:plan-no-go in place — violating the
        # mutually-exclusive invariant AND keeping the labels-first
        # plan-discovery gate stuck-False so VERIFY can never ADVANCE
        # (#1857). Swap atomically: add needs-plan, remove both siblings, in
        # ONE gh issue edit. Restores state:plan-no-go ──re-plan──▶ needs-plan.
        try:
            (
                comments,
                snapshot,
                is_replan_entry,
                revision_already_published,
            ) = _load_planning_journal(item, ctx, labels)
        except CommentJournalReadError as exc:
            logger.warning(
                "planning:%d: plan journal reconciliation read failed: %s",
                item.issue,
                exc,
            )
            return _retry_incomplete_requirements_snapshot(item, ctx, str(exc))
        recovered_restart = bool(item.payload.get("requirements_recovered_comment"))
        recovered_successor = recovered_restart and _recovered_successor_is_current(
            comments,
            snapshot,
            source_digest=str(item.payload.get("issue_body_digest") or ""),
        )
        if recovered_restart and not recovered_successor:
            # The source-bound recovery artifact authorizes a new planning
            # epoch until it binds a successor plan to the recovered source.
            item.payload.pop("plan_text", None)
            item.payload.pop("plan_revision", None)
            item.payload.pop("issue_history", None)
            is_replan_entry = True
            revision_already_published = False
        elif recovered_successor:
            # The recovered successor and its pending review are both exact
            # durable artifacts.  A fresh item must resume that review rather
            # than reopening a replan epoch or rewriting state:needs-plan.
            item.payload.pop("requires_plan_revision", None)
        if force_replan:
            if revision_already_published and snapshot.forced_planning_epoch:
                force_replan = False
            else:
                # The journal remains durable history, but none of its current
                # epoch may fast-forward or seed the forced planner invocation.
                item.payload.pop("plan_text", None)
                item.payload.pop("plan_revision", None)
                item.payload.pop("issue_history", None)
                is_replan_entry = bool(snapshot.current_plan)
        if is_replan_entry:
            item.payload["requires_plan_revision"] = True
        if not _write_planning_entry_labels(
            item.issue,
            ctx,
            labels,
            is_replan_entry=is_replan_entry,
            revision_already_published=revision_already_published,
            force_replan=force_replan,
        ):
            return StageOutcome(
                Disposition.RETRY,
                "exclusive planning entry label was not confirmed",
            )
        if force_replan:
            item.payload["forced_planning_epoch_started"] = True
            item.payload.pop("requirements_semantic_clear_digest", None)

        history = _planning_history(comments)
        if history and not force_replan:
            item.payload["issue_history"] = history

        # Restart fast-forward: journal reconciliation already found a current
        # plan, so re-entry must not redo advise + plan.
        # Jump straight to VERIFY; idempotent on repeated on_enter calls.
        if (not recovered_restart or recovered_successor) and _plan_is_ready_for_verify(
            snapshot, is_replan_entry=is_replan_entry
        ):
            logger.info(
                "planning:%d: plan comment already exists; fast-forward to VERIFY", item.issue
            )
            item.state = "VERIFY"

        return None  # proceed to step()

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next planning action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if not item.issue:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")

        if item.state == "ENTER":
            if item.payload.get("requirements_recovery_required"):
                return Continue(next_state="REQUIREMENTS_RECOVERY_WAIT")
            if ctx.config.enable_advise:
                return Continue(next_state="ADVISE_WAIT")
            logger.info("planning:%d: advise disabled; skipping to plan", item.issue)
            return Continue(next_state="PLAN_WAIT")

        recovery_step = _requirements_recovery_step(item, ctx)
        if recovery_step is not None:
            return recovery_step

        if item.state == "ADVISE_WAIT":
            logger.info("planning:%d: requesting advise job", item.issue)
            workspace = source_workspace_binding(item, ctx, SourceLane.IMPLEMENTATION)
            advise_job = AthenaSkillJob(
                request=AthenaSkillRequest(
                    kind="advise",
                    repo=item.repo,
                    issue=item.issue,
                    agent=agent_provider(ctx),
                    model=stage_model(ctx, "advise", advise_model),
                    cwd=workspace.cwd if workspace else ctx.paths.worktree,
                    timeout_s=advise_claude_timeout(),
                    workspace=workspace,
                    payload={
                        "issue_number": item.issue,
                        "issue_title": item.payload.get("issue_title", ""),
                        "issue_body": item.payload.get("issue_body", ""),
                    },
                ),
                descr="advise",
            )
            return JobRequest(advise_job, on_done_state="PLAN_WAIT")

        if item.state == "PLAN_WAIT":
            if item.payload.get("athena_advise_error"):
                return StageOutcome(Disposition.FINISH_FAIL, "athena_advise_failed")
            logger.info("planning:%d: requesting plan job", item.issue)
            workspace = source_workspace_binding(item, ctx, SourceLane.IMPLEMENTATION)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "planner", planner_model),
                prompt_builder=build_plan_prompt,
                cwd=workspace.cwd if workspace else ctx.paths.worktree,
                timeout_s=planner_claude_timeout(),
                workspace=workspace,
                sandbox="read-only",
                allowed_tools="Read,Glob,Grep",
                session_agent=AGENT_PLANNER,
                execution_request=ExecutionRequest(
                    AgentRole.PLANNER, AgentOperation.PLAN, SessionLifecycle.START_NEW
                ),
                # build_plan_prompt composes get_plan_prompt with the issue
                # title/body and advise findings in-worker, mirroring the
                # Preserve the cached advisory context through the plan job.
                # context assembly.
                prompt_kwargs={
                    "issue_number": item.issue,
                    "issue_title": item.payload.get("issue_title", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                    "advise_findings": item.payload.get("advise_findings", ""),
                    "issue_history": item.payload.get("issue_history", ""),
                },
                descr="plan",
            )
            return JobRequest(job, on_done_state="VERIFY")

        if item.state == "VERIFY":
            return _verify_plan(item, ctx)

        logger.warning("planning:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store job results on the item payload (state is still the WAIT state).

        Args:
            item: The work item to update.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.state == "ADVISE_WAIT" and not result.ok:
            item.payload["athena_advise_error"] = result.error or "advise failed"
            logger.warning("planning:%s: advise failed: %s", item.issue, result.error)
            return

        if not result.ok:
            logger.warning("planning:%s: job failed: %s", item.issue, result.error)
            return

        if result.value is not None:
            if item.state == "ADVISE_WAIT":
                if not isinstance(result.value, AthenaSkillResult) or not result.value.ok:
                    item.payload["athena_advise_error"] = "invalid Athena advise result"
                    return
                item.payload["advise_findings"] = result.value.context
                item.payload["athena_advise_receipt"] = result.value.receipt
            elif item.state == "PLAN_WAIT":
                item.payload["plan_text"] = result.value
            elif item.state == "REQUIREMENTS_RECOVERY_WAIT" and isinstance(
                result.value, RecoveredRequirements
            ):
                item.payload["recovered_requirements"] = result.value
            elif item.state == "REQUIREMENTS_RECOVERY_REVIEW_WAIT" and isinstance(
                result.value, RecoveryReview
            ):
                item.payload["requirements_recovery_review"] = result.value
