"""Plan-review stage: review, amend, and approve issue plans (issue #1814).

The single plan-review control flow as a pipeline stage
(docs/architecture.md §5.3 "plan_review" is the
binding contract). It fully owns the review/amend/learn loop that the legacy
planner review loop used to run before ``hephaestus-plan-issues`` was
re-pointed at the pipeline (#1820):

- States: ENTER -> REVIEW_WAIT -> EVAL -> AMEND_WAIT -> (loop) -> LEARN_WAIT
  -> PLAN_FINISH.
- Budgets: ``plan_review_iter`` = 3 (max review iterations per cycle),
  ``plan_cycles`` = 2 (max plan->review->amend cycles before giving up).
- Iteration accounting: ``item.attempts["plan_review_iter"]`` is the
  PER-LIFETIME audit trail (routing.py contract: attempts are never reset),
  so EVAL gates on the CYCLE-RELATIVE counter
  ``item.payload["review_round"]``, reset by ``on_enter`` whenever the item
  enters a new plan cycle (keyed on ``attempts["plan_cycles"]``). Cycle 2
  therefore gets its full ``plan_review_iter`` reviews/amends and
  ``get_plan_loop_review_prompt`` always sees a 0-based iteration in
  ``0..plan_review_iter-1``; ``plan_cycles`` bounds the number of cycles.
- Both counters advance in EVAL and ONLY for real verdicts
  (GO/NOGO/BLOCKED). ERROR, unsupported, and missing verdicts never burn an iteration
  (claude_invoke ``ReviewVerdict`` doctrine: "the reviewer never ran" must
  not be mistaken for a judgement, #911/#1554/#1794); they RETRY with
  labels untouched, bounded in-stage by
  ``item.payload["review_error_retries"]`` (cap
  ``REVIEW_ERROR_RETRY_CAP`` consecutive failures, reset on any real
  verdict or on entry into a new plan cycle, #1869). At the cap the item
  FINISH_FAILs — a reviewer-infrastructure
  failure is not fixed by replanning, so failing back to planning would
  only spend ``plan_cycles`` on more doomed reviews; labels stay untouched
  on the whole ERROR path.
- Owned labels: ``state:plan-go`` (GO), ``state:plan-no-go`` (NOGO),
  and ``state:plan-blocked`` (external input required) [durable]. GO/NOGO are
  computed by the shared pure ``state_labels.apply_plan_verdict`` and applied
  through one fail-closed, mutually-exclusive label transition.
- Verdict parsed IN-WORKER by :func:`parse_plan_review_verdict` from one exact
  final state-label token
  (carried as the review job's ``parse`` callable). REVIEW_WAIT clears any
  stale ``payload["review_verdict"]`` at submission so a failed later round
  can never replay an earlier round's verdict.
- Prompt functions (imported, never re-authored):
  ``prompts/planning.py get_plan_loop_review_prompt``,
  ``prompts/planning.py get_plan_prompt`` (composed with the reviewer
  feedback block by :func:`build_amend_prompt` for amends, then
  upserted as the durable plan comment before the next review), and
  ``learn.py build_learn_prompt`` (GO only).
- Current plan and review comments are actor-owned canonical records. Replaced
  revisions are append-once GitHub comments, and their plan record carries the
  next-plan recovery payload so restart can finish an interrupted journal
  transition before another agent runs.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from hephaestus.automation.agent_config import (
    learn_claude_timeout,
    plan_reviewer_claude_timeout,
    planner_claude_timeout,
    planner_model,
    reviewer_model,
)
from hephaestus.automation.learn import build_learn_prompt
from hephaestus.automation.prompts._shared import fence_content
from hephaestus.automation.prompts.planning import (
    get_plan_loop_review_prompt,
)
from hephaestus.automation.protocol import (
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_PREFIX,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    history_projection,
    journal_snapshot,
    parse_plan_review_state,
    render_current_review,
)
from hephaestus.automation.review_types import ReviewVerdict
from hephaestus.automation.session_naming import AGENT_PLAN_REVIEWER, AGENT_PLANNER
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    apply_plan_verdict,
    enter_planning_transition,
    is_exclusive_plan_state,
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
from .planning import _publish_plan_blocked, build_plan_prompt

logger = logging.getLogger(__name__)

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
REVIEW_WAIT = "REVIEW_WAIT"
EVAL = "EVAL"
AMEND_WAIT = "AMEND_WAIT"
LEARN_WAIT = "LEARN_WAIT"
PLAN_FINISH = "PLAN_FINISH"
FINISH = PLAN_FINISH

#: Max CONSECUTIVE reviewer-infrastructure failures (ERROR verdicts or
#: failed/valueless review jobs) tolerated before the item FINISH_FAILs.
#: Bounds the in-stage ERROR retry loop without burning ``plan_review_iter``
#: or stamping labels (#911). Reset whenever a real verdict arrives.
REVIEW_ERROR_RETRY_CAP = 2

_PLAN_REVIEW_LABELS = {
    STATE_PLAN_GO: "GO",
    STATE_PLAN_NO_GO: "NOGO",
    STATE_PLAN_BLOCKED: "BLOCKED",
}


def parse_plan_review_verdict(text: str) -> ReviewVerdict:
    """Parse the one exact state label that terminates a plan review."""
    state = parse_plan_review_state(text)
    if state is None:
        return ReviewVerdict(grade=None, verdict="ERROR", raw=text)
    return ReviewVerdict(grade=None, verdict=_PLAN_REVIEW_LABELS[state], raw=text)


def _normalize_review_comment(review: str, *, revision: int | None = None) -> str:
    """Normalize reviewer output for the canonical issue comment."""
    return render_current_review(review, revision=revision or 1)


def _current_revision(comments: Sequence[IssueComment | str]) -> int:
    """Recover the current plan revision from durable issue comments."""
    return journal_snapshot(comments).revision


def _plan_history(comments: Sequence[IssueComment | str]) -> str:
    """Render tracked comments in logical revision order for the next agent."""
    return history_projection(comments)


def _confirm_pending_amendment_transition(
    item: WorkItem,
    ctx: StageContext,
) -> StageOutcome | None:
    """Confirm a revised plan's exclusive NEEDS_PLAN state before review."""
    if not item.payload.get("needs_plan_transition_pending"):
        return None
    assert item.issue is not None  # noqa: S101 - stage validates the issue
    labels = _require_issue_labels(item, ctx)
    if STATE_PLAN_BLOCKED in labels:
        return StageOutcome(
            Disposition.BLOCKED,
            "plan was blocked externally while amendment was in flight",
        )
    if not is_exclusive_plan_state(labels, STATE_NEEDS_PLAN):
        add, remove = enter_planning_transition()
        ctx.github.edit_labels(item.issue, add=add, remove=remove)
        labels = _require_issue_labels(item, ctx)
        if STATE_PLAN_BLOCKED in labels:
            return StageOutcome(
                Disposition.BLOCKED,
                "plan was blocked externally while amendment was in flight",
            )
        if not is_exclusive_plan_state(labels, STATE_NEEDS_PLAN):
            return StageOutcome(
                Disposition.RETRY,
                "exclusive needs-plan label was not confirmed",
            )
    item.payload.pop("needs_plan_transition_pending", None)
    return None


def _operator_blocked_outcome(item: WorkItem, ctx: StageContext) -> StageOutcome | None:
    """Stop non-EVAL work when the operator latch appears between steps."""
    if item.state in {"ENTER", "EVAL"} or item.issue is None:
        return None
    live_labels = _require_issue_labels(item, ctx)
    if STATE_PLAN_BLOCKED not in live_labels:
        return None
    return StageOutcome(
        Disposition.BLOCKED,
        "plan is blocked pending external intervention",
    )


def build_amend_prompt(
    issue_number: int,
    prior_review: str,
    issue_title: str = "",
    issue_body: str = "",
    advise_findings: str = "",
    plan_history: str = "",
) -> str:
    """Compose the amend prompt: task-aware plan prompt + reviewer feedback block.

    Module-level composed builder (NOT a closure): :class:`AgentJob` is
    frozen and prompt builders run in-worker, so the builder must be a
    top-level function receiving everything via ``prompt_kwargs``. The
    feedback block appends the prior reviewer critique to the same
    task-aware plan prompt used by initial planning (doc section 3 step 3:
    "resume planner session with feedback block").

    Args:
        issue_number: GitHub issue number being re-planned.
        prior_review: The previous review round's NOGO critique text.
        issue_title: Source issue title.
        issue_body: Source issue body.
        advise_findings: Advise-step findings; empty string means no block.

    Returns:
        The full amend prompt with the feedback block appended.

    """
    prompt = build_plan_prompt(issue_number, issue_title, issue_body, advise_findings)
    fenced = fence_content()
    feedback = PromptCatalog.current().render(
        "planning/amend_feedback.j2",
        untrusted_notice=fenced.untrusted_notice,
        prior_review_block=fenced.fence("PRIOR_REVIEW", prior_review),
        plan_history_block=(
            fenced.fence("PLAN_HISTORY", plan_history) if plan_history else "_(first revision)_"
        ),
    )
    return prompt + feedback


class PlanReviewStage(Stage):
    """Stage for reviewing and iterating on a plan.

    State machine (doc section "3. plan_review"):

    - ENTER: fast-forward at-or-past ``state:plan-go`` -> ADVANCE (mirrors
      planning's on_enter guard, defends against a restart re-seeding the
      item directly into plan_review); otherwise reset the cycle-relative
      review counter when a new plan cycle begins, then route to
      REVIEW_WAIT.
    - REVIEW_WAIT: clear any stale verdict, then submit the reviewer job;
      the verdict is parsed in-worker by ``parse_plan_review_verdict`` and lands
      in ``item.payload["review_verdict"]``.
    - EVAL [M]: real verdicts (GO/NOGO/BLOCKED) advance both iteration
      counters; ERROR/missing verdicts never do. GO -> durably apply
      ``state:plan-go`` (write BEFORE the advancing outcome) then learn
      step or ADVANCE; NOGO within the cycle-relative iteration budget ->
      durably apply ``state:plan-no-go`` and enter AMEND_WAIT within budget,
      or FAIL_BACK("nogo") while plan cycles remain;
      BLOCKED applies and confirms ``state:plan-blocked`` first, publishes its
      explanation as audit data, and stops
      or FAIL_BACK("plan_cycles_exhausted") once exhausted; ERROR/missing
      verdict -> labels untouched, RETRY (bounded by
      ``REVIEW_ERROR_RETRY_CAP`` consecutive failures, then FINISH_FAIL).
    - AMEND_WAIT: submit the planner amend job (:func:`build_amend_prompt`
      carries the reviewer feedback block), loop to REVIEW_WAIT.
    - LEARN_WAIT (GO only, gated by ``enable_learn``): submit the learn job,
      then ADVANCE.

    Fail routes (ROUTES): "nogo" -> planning, "plan_cycles_exhausted" ->
    finished(fail).
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Fast-forward at-or-past state:plan-go, else reset the cycle-relative review counter.

        A restart can re-seed an item directly into plan_review carrying
        state:plan-go (product_to_work_item classifies issues into arbitrary
        entry stages), bypassing planning's on_enter guard entirely. Without
        this check the item would run a full reviewer job again instead of
        fast-forwarding, wasting an invocation and risking a spurious
        re-verdict (#1870).

        ``attempts["plan_review_iter"]`` is per-lifetime (routing.py:
        attempts are never reset), so the per-cycle review budget is
        tracked in ``payload["review_round"]``. The reset keys on
        ``attempts["plan_cycles"]`` (recorded in ``payload["review_cycle"]``)
        so it fires exactly once per fail-back cycle: a same-cycle re-entry
        (e.g. the ERROR-path RETRY) matches the recorded cycle and keeps its
        round count. A fresh cycle also restarts the consecutive
        reviewer-failure streak (``payload["review_error_retries"]``,
        #1869) — otherwise a leftover count from a prior transient failure
        could let the first ERROR of the new cycle immediately exceed
        ``REVIEW_ERROR_RETRY_CAP``. Idempotent — a literal double on_enter
        is a no-op.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            A StageOutcome to fast-forward past review, or None to proceed
            to step().

        """
        if item.issue is not None:
            labels = _require_issue_labels(item, ctx)
            if STATE_PLAN_BLOCKED in labels:
                logger.info("plan_review:%d: already plan-blocked; stopping", item.issue)
                ctx.github.ensure_blocked_audit(item.issue)
                return StageOutcome(Disposition.BLOCKED, "plan requires external intervention")
            if is_exclusive_plan_state(labels, STATE_PLAN_GO):
                logger.info("plan_review:%d: already plan-go; advancing", item.issue)
                return StageOutcome(Disposition.ADVANCE, "plan already approved")

            comments = reconcile_plan_journal(item.issue, ctx.github)
            snapshot = journal_snapshot(comments)
            if snapshot.current_plan:
                item.payload["plan_text"] = snapshot.current_plan
                item.payload["plan_revision"] = snapshot.revision
            if snapshot.current_review and snapshot.current_review_revision == snapshot.revision:
                item.payload["prior_review"] = snapshot.current_review

        cycle = item.attempts.get("plan_cycles", 0)
        if item.payload.get("review_cycle") != cycle:
            item.payload["review_cycle"] = cycle
            item.payload["review_round"] = 0
            # Fresh plan cycle: the consecutive reviewer-failure streak
            # restarts too (#1869) — a leftover count from a prior
            # transient reviewer failure must not let the first ERROR of
            # a new cycle immediately exceed REVIEW_ERROR_RETRY_CAP.
            item.payload.pop("review_error_retries", None)
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next plan-review action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if not item.issue:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")

        # on_enter is not a durable lock. Re-check the operator latch before
        # every agent request and before the delayed post-learn transition.
        # EVAL performs its own read immediately before any verdict write.
        blocked_outcome = _operator_blocked_outcome(item, ctx)
        if blocked_outcome is not None:
            return blocked_outcome

        if item.state == "ENTER":
            return Continue(next_state="REVIEW_WAIT")

        if item.state == "REVIEW_WAIT":
            transition_outcome = _confirm_pending_amendment_transition(item, ctx)
            if transition_outcome is not None:
                return transition_outcome

            no_progress_reason = str(item.payload.get("no_progress_reason") or "")
            if no_progress_reason:
                raw_review = (
                    "Planning is stuck and needs external feedback. "
                    f"{no_progress_reason}\n\n{STATE_PLAN_BLOCKED}"
                )
                revision = int(item.payload.get("plan_revision") or 1)
                confirmed = _publish_plan_blocked(
                    item.issue,
                    ctx,
                    raw_review=raw_review,
                    revision=revision,
                )
                if not confirmed:
                    return StageOutcome(Disposition.RETRY, "blocked label was not confirmed")
                return StageOutcome(
                    Disposition.BLOCKED,
                    "planning made no progress; external feedback required",
                )

            # Counters advance in EVAL, and only for real verdicts — a
            # submission is not an iteration (#1554/#1794). The 0-based
            # prompt iteration is the cycle-relative round count.
            round_index = item.payload.get("review_round", 0)
            # Clear any stale verdict at submission so a failed later round
            # can never replay an earlier round's verdict in EVAL.
            item.payload.pop("review_verdict", None)
            item.payload.pop("review_comment_published", None)
            logger.info("plan_review:%d: requesting review job (round %d)", item.issue, round_index)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "reviewer", reviewer_model),
                prompt_builder=get_plan_loop_review_prompt,
                cwd=ctx.paths.worktree,
                timeout_s=plan_reviewer_claude_timeout(),
                session_agent=AGENT_PLAN_REVIEWER,
                # get_plan_loop_review_prompt takes a 0-based iteration index
                # (full-sweep suffix on the final iteration). Issue title/body
                # are seeded into item.payload by the coordinator (#1817).
                prompt_kwargs={
                    "issue_number": item.issue,
                    "issue_title": item.payload.get("issue_title", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                    "plan_text": item.payload.get("plan_text", ""),
                    # Populated from the payload when present; wiring the
                    # legacy capture_planner_learnings step into the pipeline
                    # is deferred to the coordinator slice (#1817).
                    "learnings": item.payload.get("learnings", ""),
                    "iteration": round_index,
                    "prior_review": item.payload.get("prior_review") or None,
                    "advise_findings": item.payload.get("advise_findings", ""),
                    "plan_history": _plan_history(ctx.github.issue_comments(item.issue)),
                },
                parse=parse_plan_review_verdict,  # verdict parsed in-worker
                descr="review",
            )
            return JobRequest(job, on_done_state="EVAL")

        if item.state == "EVAL":
            return self._eval(item, ctx)

        if item.state == "AMEND_WAIT":
            logger.info("plan_review:%d: requesting amend job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "planner", planner_model),
                prompt_builder=build_amend_prompt,
                cwd=ctx.paths.worktree,
                timeout_s=planner_claude_timeout(),
                session_agent=AGENT_PLANNER,
                # build_amend_prompt composes get_plan_prompt with the
                # reviewer feedback block in-worker (doc: "resume planner
                # session with feedback block"). The worker resumes the planner
                # session (#1817 wires that session setup).
                prompt_kwargs={
                    "issue_number": item.issue,
                    "issue_title": item.payload.get("issue_title", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                    "advise_findings": item.payload.get("advise_findings", ""),
                    "prior_review": item.payload.get("prior_review", ""),
                    "plan_history": _plan_history(ctx.github.issue_comments(item.issue)),
                },
                descr="amend",
            )
            return JobRequest(job, on_done_state="REVIEW_WAIT")

        if item.state == "LEARN_WAIT":
            logger.info("plan_review:%d: requesting learn job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "planner", planner_model),
                prompt_builder=build_learn_prompt,
                cwd=ctx.paths.worktree,
                timeout_s=learn_claude_timeout(),
                session_agent=AGENT_PLANNER,  # resume planner session (legacy parity)
                prompt_kwargs={"context": item.payload.get("plan_text", "")},
                descr="learn",
            )
            return JobRequest(job, on_done_state=PLAN_FINISH)

        if item.state == PLAN_FINISH:
            return self._finish_after_learning(item, ctx)

        logger.warning("plan_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    @staticmethod
    def _finish_after_learning(item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Advance only while the approved label remains live and exclusive."""
        live_labels = _require_issue_labels(item, ctx)
        if not is_exclusive_plan_state(live_labels, STATE_PLAN_GO):
            return StageOutcome(
                Disposition.RETRY,
                "exclusive plan-go label was not confirmed after learning",
            )
        logger.info("plan_review:%d: learn completed; advancing", item.issue)
        return StageOutcome(Disposition.ADVANCE, "plan approved and learned")

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """EVAL [M]: decide the next action from the parsed reviewer verdict.

        Every durable label write below happens BEFORE the outcome that
        causes a queue push (the load-bearing pipeline invariant). Both
        iteration counters (lifetime ``attempts["plan_review_iter"]`` audit
        trail and cycle-relative ``payload["review_round"]`` gate) advance
        here, and only for real verdicts — never for ERROR or missing
        verdicts (#911/#1554/#1794).
        """
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        issue_number = item.issue
        verdict = item.payload.get("review_verdict")

        # ERROR verdict or missing verdict (the review job failed, so
        # on_job_done stored nothing) = reviewer-infrastructure failure:
        # labels untouched, no iteration burned, RETRY — bounded by the
        # consecutive-failure cap so the retry loop cannot spin forever.
        if verdict is None or verdict.verdict not in {"GO", "NOGO", "BLOCKED"}:
            reason = "no verdict found" if verdict is None else "reviewer error"
            retries = item.payload.get("review_error_retries", 0) + 1
            item.payload["review_error_retries"] = retries
            if retries > REVIEW_ERROR_RETRY_CAP:
                logger.error(
                    "plan_review:%d: %s; %d consecutive reviewer failures "
                    "(cap %d) — failing without labels",
                    item.issue,
                    reason,
                    retries,
                    REVIEW_ERROR_RETRY_CAP,
                )
                return StageOutcome(
                    Disposition.FINISH_FAIL,
                    f"reviewer error retries exhausted ({reason})",
                )
            logger.warning(
                "plan_review:%d: %s; retry %d/%d (no iteration burned)",
                item.issue,
                reason,
                retries,
                REVIEW_ERROR_RETRY_CAP,
            )
            return StageOutcome(Disposition.RETRY, reason)

        # An operator may apply BLOCKED while the reviewer agent is running.
        # Re-read immediately before any audit or label write; automation must
        # neither overwrite the blocked explanation nor clear the latch.
        live_labels = _require_issue_labels(item, ctx)
        if STATE_PLAN_BLOCKED in live_labels and verdict.verdict != "BLOCKED":
            return StageOutcome(
                Disposition.BLOCKED,
                "plan was blocked externally while review was in flight",
            )

        # Real verdict: this review round counts. Advance the cycle-relative
        # gate and the lifetime audit trail; reset the consecutive-failure cap.
        item.payload["review_error_retries"] = 0
        round_done = item.payload.get("review_round", 0) + 1
        item.payload["review_round"] = round_done
        item.attempts["plan_review_iter"] = item.attempts.get("plan_review_iter", 0) + 1

        def publish_review_comment() -> None:
            """Idempotently update the explanatory journal after safe state ordering."""
            if item.payload.get("review_comment_published"):
                return
            revision = int(
                item.payload.get("plan_revision")
                or _current_revision(ctx.github.issue_comments(issue_number))
            )
            ctx.github.upsert_issue_comment(
                issue_number,
                PLAN_REVIEW_CANONICAL_MARKER,
                _normalize_review_comment(verdict.raw, revision=revision),
                legacy_marker=PLAN_REVIEW_PREFIX,
            )
            item.payload["review_comment_published"] = True

        if verdict.verdict == "BLOCKED":
            # BLOCKED is the safety latch. Make it durable first so an audit
            # write failure cannot resume autonomous work; the retry still
            # attempts to persist the required explanation idempotently.
            return self._complete_blocked_with_audit(item, ctx, verdict)

        # GO/NOGO audit text is durable before its proposed label. Regardless
        # of prose, only the confirmed exclusive label below can route.
        publish_review_comment()

        if verdict.is_go:
            return self._complete_go(item, ctx)

        # Every NOGO is durable control state, including rounds that can still
        # amend. The replacement plan publication transitions back to
        # state:needs-plan only after both canonical comments are updated.
        self._write_verdict_labels(item.issue, ctx, is_go=False)
        if not is_exclusive_plan_state(
            _require_issue_labels(item, ctx),
            STATE_PLAN_NO_GO,
        ):
            return StageOutcome(Disposition.RETRY, "plan-no-go label was not confirmed")

        # NOGO: amend within the cycle-relative budget.
        budget_iter = ctx.budget("plan_review_iter")
        if round_done < budget_iter:
            logger.info(
                "plan_review:%d: %s verdict (round %d/%d); amending plan",
                item.issue,
                verdict.verdict,
                round_done,
                budget_iter,
            )
            return Continue(next_state="AMEND_WAIT")

        # Iteration cap: fail back with state:plan-no-go already durable —
        # "nogo" while plan_cycles remain, "plan_cycles_exhausted" once the
        # cycle budget is consumed (routes to finished(fail) via ROUTES).
        logger.warning(
            "plan_review:%d: %s exhausted (round %d/%d); applying no-go label",
            item.issue,
            verdict.verdict,
            round_done,
            budget_iter,
        )
        cycles = item.attempts.get("plan_cycles", 0) + 1
        item.attempts["plan_cycles"] = cycles
        if cycles >= ctx.budget("plan_cycles"):
            logger.error(
                "plan_review:%d: plan_cycles exhausted (%d/%d)",
                item.issue,
                cycles,
                ctx.budget("plan_cycles"),
            )
            return StageOutcome(Disposition.FAIL_BACK, "plan_cycles_exhausted")
        return StageOutcome(Disposition.FAIL_BACK, "nogo")

    def _complete_blocked(self, item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Apply and confirm BLOCKED before returning its routing outcome."""
        assert item.issue is not None  # noqa: S101 - _eval narrows the issue
        ctx.github.edit_labels(
            item.issue,
            add=[STATE_PLAN_BLOCKED],
            remove=[STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO],
        )
        if not is_exclusive_plan_state(
            _require_issue_labels(item, ctx),
            STATE_PLAN_BLOCKED,
        ):
            return StageOutcome(Disposition.RETRY, "blocked label was not confirmed")
        return StageOutcome(Disposition.BLOCKED, "plan requires external intervention")

    def _complete_blocked_with_audit(
        self,
        item: WorkItem,
        ctx: StageContext,
        verdict: ReviewVerdict,
    ) -> StageOutcome:
        """Latch BLOCKED, confirm it, then persist the required explanation."""
        outcome = self._complete_blocked(item, ctx)
        if outcome.disposition == Disposition.RETRY:
            return outcome
        assert item.issue is not None  # noqa: S101 - _eval narrows the issue
        revision = int(
            item.payload.get("plan_revision")
            or _current_revision(ctx.github.issue_comments(item.issue))
        )
        ctx.github.upsert_issue_comment(
            item.issue,
            PLAN_REVIEW_CANONICAL_MARKER,
            _normalize_review_comment(verdict.raw, revision=revision),
            legacy_marker=PLAN_REVIEW_PREFIX,
        )
        item.payload["review_comment_published"] = True
        return outcome

    def _complete_go(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Apply and confirm GO before learn or implementation routing."""
        assert item.issue is not None  # noqa: S101 - _eval narrows the issue
        logger.info("plan_review:%d: GO verdict; applying label and advancing", item.issue)
        self._write_verdict_labels(item.issue, ctx, is_go=True)
        if not is_exclusive_plan_state(
            _require_issue_labels(item, ctx),
            STATE_PLAN_GO,
        ):
            return StageOutcome(Disposition.RETRY, "plan-go label was not confirmed")
        if ctx.config.enable_learn:
            return Continue(next_state="LEARN_WAIT")
        return StageOutcome(Disposition.ADVANCE, "plan approved (learn disabled)")

    @staticmethod
    def _write_verdict_labels(issue_number: int, ctx: StageContext, *, is_go: bool) -> None:
        """Write a verdict label without ever clearing an operator BLOCKED latch.

        The caller must re-read and confirm an exclusive state before using
        this write to route the item. A concurrent BLOCKED application wins.

        Args:
            issue_number: GitHub issue number.
            ctx: Stage context carrying the GitHub accessor.
            is_go: True for a GO verdict, False for NOGO-exhausted.

        """
        label_to_add, labels_to_remove = apply_plan_verdict(is_go=is_go)
        ctx.github.edit_labels(
            issue_number,
            add=[label_to_add],
            remove=labels_to_remove,
        )

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store job results on the item payload (state is still the WAIT state).

        Args:
            item: The work item to update.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if not result.ok:
            logger.warning("plan_review:%s: job failed: %s", item.issue, result.error)
            return

        if result.value is not None:
            if item.state == "REVIEW_WAIT":
                verdict = result.value
                item.payload["review_verdict"] = verdict
                if getattr(verdict, "verdict", None) == "NOGO":
                    raw_review = getattr(verdict, "raw", None)
                    assert isinstance(raw_review, str), (  # noqa: S101 - explicit worker contract
                        "plan_review REVIEW_WAIT NOGO verdict must expose raw review text"
                    )
                    item.payload["prior_review"] = raw_review
            elif item.state == "AMEND_WAIT":
                plan_text = result.value
                if item.issue is not None and isinstance(plan_text, str):
                    publication = publish_plan_revision(
                        item.issue,
                        plan_text,
                        ctx.github,
                        require_change=True,
                    )
                    item.payload["plan_text"] = publication.plan
                    item.payload["plan_revision"] = publication.revision
                    if publication.is_stuck:
                        item.payload["no_progress_reason"] = publication.no_progress_reason
                        raw_review = (
                            "Planning is stuck and needs external feedback. "
                            f"{publication.no_progress_reason}\n\n{STATE_PLAN_BLOCKED}"
                        )
                        _publish_plan_blocked(
                            item.issue,
                            ctx,
                            raw_review=raw_review,
                            revision=publication.revision,
                        )
                        return
                    add, remove = enter_planning_transition()
                    ctx.github.edit_labels(item.issue, add=add, remove=remove)
                    item.payload["needs_plan_transition_pending"] = True
                    item.payload.pop("no_progress_reason", None)
            # LEARN_WAIT intentionally has no branch: the learn job's output
            # is a side effect for the Mnemosyne skill store, not a payload
            # value any later state consumes.
