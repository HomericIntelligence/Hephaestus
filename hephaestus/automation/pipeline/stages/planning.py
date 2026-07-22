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
from collections.abc import Sequence

from hephaestus.automation.agent_config import (
    advise_claude_timeout,
    advise_model,
    planner_claude_timeout,
    planner_model,
)
from hephaestus.automation.prompts._shared import fence_content
from hephaestus.automation.prompts.advise import get_advise_prompt_builder
from hephaestus.automation.prompts.planning import get_plan_prompt
from hephaestus.automation.protocol import PLAN_REVIEW_CANONICAL_MARKER, PLAN_REVIEW_PREFIX
from hephaestus.automation.review_journal import (
    IssueComment,
    JournalSnapshot,
    history_projection,
    is_pending_review,
    journal_snapshot,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.session_naming import AGENT_ADVISE, AGENT_PLANNER
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    enter_planning_transition,
    is_exclusive_plan_state,
    is_plan_go,
    is_skipped,
)
from hephaestus.prompts import PromptCatalog

from ..plan_journal import publish_plan_revision, reconcile_plan_journal
from .base import (
    AgentJob,
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
    stage_model,
)

logger = logging.getLogger(__name__)


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
    """Return every ordered plan/review journal iteration."""
    return history_projection(comments)


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


def _plan_is_ready_for_verify(
    issue_number: int,
    ctx: StageContext,
    *,
    is_replan_entry: bool,
) -> bool:
    """Return whether restart may verify the canonical plan without another agent."""
    if is_replan_entry:
        return False
    return ctx.github.has_existing_plan(issue_number)


def _write_planning_entry_labels(
    issue_number: int,
    ctx: StageContext,
    labels: Sequence[str],
    *,
    is_replan_entry: bool,
    revision_already_published: bool,
) -> bool:
    """Durably establish the mutually-exclusive planning entry label."""
    if STATE_PLAN_NO_GO in labels and revision_already_published:
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
    elif STATE_NEEDS_PLAN not in labels:
        logger.info("planning:%d: adding %s label", issue_number, STATE_NEEDS_PLAN)
        ctx.github.add_labels(issue_number, [STATE_NEEDS_PLAN])
    return is_exclusive_plan_state(
        _require_issue_labels_for_transition(issue_number, ctx),
        STATE_NEEDS_PLAN,
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
        legacy_marker=PLAN_REVIEW_PREFIX,
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
    publication = publish_plan_revision(
        item.issue,
        str(item.payload["plan_text"]),
        ctx.github,
        require_change=requires_revision,
    )
    item.payload["plan_text"] = publication.plan
    item.payload["plan_revision"] = publication.revision
    if publication.is_stuck:
        return _no_progress_outcome(
            item,
            ctx,
            reason=publication.no_progress_reason,
            revision=publication.revision,
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
    plan_text = item.payload.get("plan_text")
    posted_plan = False
    if plan_text is not None:
        requires_revision = bool(item.payload.get("requires_plan_revision"))
        has_existing_plan = ctx.github.has_existing_plan(item.issue)
        if requires_revision or not has_existing_plan:
            logger.info("planning:%d: publishing plan revision", item.issue)
            publication_outcome = _publish_candidate_plan(
                item,
                ctx,
                requires_revision=requires_revision,
            )
            if publication_outcome is not None:
                return publication_outcome
            posted_plan = True

    if posted_plan or ctx.github.has_existing_plan(item.issue):
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
    and ``Planner._has_existing_plan``, all ordered at-or-past checks):

    - ``state:skip`` -> SKIP (checked BEFORE plan-go; skip wins over
      everything, even a contradictory plan-go, logging a WARNing — #1835)
    - already at-or-past ``state:plan-go`` -> ADVANCE (zero jobs)
    - merged closing PR -> close issue as covered, SKIP
    - open PR -> SKIP (PR already covers implementation)
    - unlabeled entry -> idempotent bare add of ``state:needs-plan``; entry
      carrying ``state:plan-no-go`` (or a stale ``state:plan-go``) after a
      plan_review fail-back -> ONE atomic ``edit_labels`` swap adding
      ``state:needs-plan`` and removing both siblings, so the labels-first
      ``has_existing_plan`` gate can pass once a fresh plan comment is posted
      and the mutually-exclusive-label invariant holds (#1857)
    - plan comment already exists (``ctx.github.has_existing_plan``) ->
      fast-forward ``item.state`` to VERIFY so a restart mid-stage never
      redoes advise + plan (the base-protocol idempotency promise); the
      ``is_plan_review_go`` label check above stays the primary gate.
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
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

        labels = _require_issue_labels(item, ctx)

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

        # Fast-forward only from the sole confirmed plan state. A stale sibling
        # makes the label set contradictory and must never authorize work.
        if is_exclusive_plan_state(labels, STATE_PLAN_GO):
            logger.info("planning:%d: already plan-go; advancing", item.issue)
            return StageOutcome(Disposition.ADVANCE, "plan already approved")

        # Re-housed _pr_coverage_skip gate A: merged closing PR covers the issue
        merged_pr = ctx.github.find_merged_closing_pr(item.issue)
        if merged_pr:
            logger.info("planning:%d: merged PR #%d covers issue; closing", item.issue, merged_pr)
            ctx.github.close_issue_as_covered(item.issue, merged_pr)
            return StageOutcome(Disposition.SKIP, f"covered by merged PR #{merged_pr}")

        # Re-housed _pr_coverage_skip gate B: open PR already in flight
        open_pr = ctx.github.find_pr_for_issue(item.issue)
        if open_pr:
            logger.info("planning:%d: open PR #%d exists; skipping", item.issue, open_pr)
            return StageOutcome(Disposition.SKIP, f"open PR #{open_pr} exists")

        # Entry label normalization. On the plan_review "nogo" fail-back the
        # issue carries state:plan-no-go and NEITHER sibling (apply_plan_verdict
        # ADDS no-go, removing needs-plan/plan-go). A bare add of needs-plan
        # would leave state:plan-no-go in place — violating the
        # mutually-exclusive invariant AND keeping the labels-first
        # has_existing_plan gate stuck-False so VERIFY can never ADVANCE
        # (#1857). Swap atomically: add needs-plan, remove both siblings, in
        # ONE gh issue edit. Restores state:plan-no-go ──re-plan──▶ needs-plan.
        (
            comments,
            _snapshot,
            is_replan_entry,
            revision_already_published,
        ) = _load_planning_journal(item, ctx, labels)
        if is_replan_entry:
            item.payload["requires_plan_revision"] = True
        if not _write_planning_entry_labels(
            item.issue,
            ctx,
            labels,
            is_replan_entry=is_replan_entry,
            revision_already_published=revision_already_published,
        ):
            return StageOutcome(
                Disposition.RETRY,
                "exclusive planning entry label was not confirmed",
            )

        history = _planning_history(comments)
        if history:
            item.payload["issue_history"] = history

        # Restart fast-forward: a plan comment already exists (real has-plan
        # semantics via ctx.github), so re-entry must not redo advise + plan.
        # Jump straight to VERIFY; idempotent on repeated on_enter calls.
        if _plan_is_ready_for_verify(
            item.issue,
            ctx,
            is_replan_entry=is_replan_entry,
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
            if ctx.config.enable_advise:
                return Continue(next_state="ADVISE_WAIT")
            logger.info("planning:%d: advise disabled; skipping to plan", item.issue)
            return Continue(next_state="PLAN_WAIT")

        if item.state == "ADVISE_WAIT":
            logger.info("planning:%d: requesting advise job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "advise", advise_model),
                prompt_builder=get_advise_prompt_builder(ctx.config.agent),
                cwd=ctx.paths.worktree,
                timeout_s=advise_claude_timeout(),
                session_agent=AGENT_ADVISE,
                # Issue title/body and the Mnemosyne marketplace path are
                # seeded into item.payload by the coordinator (#1817), which
                # owns issue fetching and advise_runner setup.
                prompt_kwargs={
                    "issue_number": item.issue,
                    "issue_title": item.payload.get("issue_title", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                    "marketplace_path": item.payload.get("marketplace_path", ""),
                },
                descr="advise",
            )
            return JobRequest(job, on_done_state="PLAN_WAIT")

        if item.state == "PLAN_WAIT":
            logger.info("planning:%d: requesting plan job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "planner", planner_model),
                prompt_builder=build_plan_prompt,
                cwd=ctx.paths.worktree,
                timeout_s=planner_claude_timeout(),
                session_agent=AGENT_PLANNER,
                # build_plan_prompt composes get_plan_prompt with the issue
                # title/body and advise findings in-worker, mirroring the
                # legacy planner_review_loop.generate_plan(cached_advise=...)
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
        if not result.ok:
            logger.warning("planning:%s: job failed: %s", item.issue, result.error)
            return

        if result.value is not None:
            if item.state == "ADVISE_WAIT":
                item.payload["advise_findings"] = result.value
            elif item.state == "PLAN_WAIT":
                item.payload["plan_text"] = result.value
