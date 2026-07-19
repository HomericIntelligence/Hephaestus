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
  (GO/NOGO/AMBIGUOUS). ERROR and missing verdicts never burn an iteration
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
- Owned labels: ``state:plan-go`` (GO) [durable], ``state:plan-no-go``
  (exhausted) [durable] — both computed by the shared pure
  ``state_labels.apply_plan_verdict`` so this stage and the legacy loop
  apply identical transitions; the paired add/remove writes are non-fatal
  (independent try/except-warn per write).
- Verdict parsed IN-WORKER by ``claude_invoke.parse_review_verdict``
  (carried as the review job's ``parse`` callable). REVIEW_WAIT clears any
  stale ``payload["review_verdict"]`` at submission so a failed later round
  can never replay an earlier round's verdict.
- Prompt functions (imported, never re-authored):
  ``prompts/planning.py get_plan_loop_review_prompt``,
  ``prompts/planning.py get_plan_prompt`` (composed with the reviewer
  feedback block by :func:`build_amend_prompt` for amends, then
  upserted as the durable plan comment before the next review), and
  ``learn.py build_learn_prompt`` (GO only).
"""

from __future__ import annotations

import logging

from hephaestus.automation.agent_config import (
    learn_claude_timeout,
    plan_reviewer_claude_timeout,
    planner_claude_timeout,
    planner_model,
    reviewer_model,
)
from hephaestus.automation.claude_invoke import parse_review_verdict
from hephaestus.automation.learn import build_learn_prompt
from hephaestus.automation.prompts._shared import fence_content
from hephaestus.automation.prompts.planning import (
    get_plan_loop_review_prompt,
)
from hephaestus.automation.session_naming import AGENT_PLAN_REVIEWER, AGENT_PLANNER
from hephaestus.automation.state_labels import apply_plan_verdict, is_plan_go
from hephaestus.prompts import PromptCatalog

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
    _issue_labels,
    agent_provider,
    stage_model,
)
from .planning import _normalize_plan_comment, build_plan_prompt

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


def build_amend_prompt(
    issue_number: int,
    prior_review: str,
    issue_title: str = "",
    issue_body: str = "",
    advise_findings: str = "",
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
      the verdict is parsed in-worker by ``parse_review_verdict`` and lands
      in ``item.payload["review_verdict"]``.
    - EVAL [M]: real verdicts (GO/NOGO/AMBIGUOUS) advance both iteration
      counters; ERROR/missing verdicts never do. GO -> durably apply
      ``state:plan-go`` (write BEFORE the advancing outcome) then learn
      step or ADVANCE; NOGO within the cycle-relative iteration budget ->
      AMEND_WAIT; NOGO/AMBIGUOUS at the cap -> durably apply
      ``state:plan-no-go``, then FAIL_BACK("nogo") while plan_cycles remain
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
            labels = _issue_labels(item, ctx)
            if is_plan_go(labels):
                logger.info("plan_review:%d: already plan-go; advancing", item.issue)
                return StageOutcome(Disposition.ADVANCE, "plan already approved")

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

        if item.state == "ENTER":
            return Continue(next_state="REVIEW_WAIT")

        if item.state == "REVIEW_WAIT":
            # Counters advance in EVAL, and only for real verdicts — a
            # submission is not an iteration (#1554/#1794). The 0-based
            # prompt iteration is the cycle-relative round count.
            round_index = item.payload.get("review_round", 0)
            # Clear any stale verdict at submission so a failed later round
            # can never replay an earlier round's verdict in EVAL.
            item.payload.pop("review_verdict", None)
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
                },
                parse=parse_review_verdict,  # verdict parsed in-worker
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
            logger.info("plan_review:%d: learn completed; advancing", item.issue)
            return StageOutcome(Disposition.ADVANCE, "plan approved and learned")

        logger.warning("plan_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

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
        verdict = item.payload.get("review_verdict")

        # ERROR verdict or missing verdict (the review job failed, so
        # on_job_done stored nothing) = reviewer-infrastructure failure:
        # labels untouched, no iteration burned, RETRY — bounded by the
        # consecutive-failure cap so the retry loop cannot spin forever.
        if verdict is None or verdict.is_error:
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

        # Real verdict: this review round counts. Advance the cycle-relative
        # gate and the lifetime audit trail; reset the consecutive-failure cap.
        item.payload["review_error_retries"] = 0
        round_done = item.payload.get("review_round", 0) + 1
        item.payload["review_round"] = round_done
        item.attempts["plan_review_iter"] = item.attempts.get("plan_review_iter", 0) + 1

        if verdict.is_go:
            logger.info("plan_review:%d: GO verdict; applying label and advancing", item.issue)
            self._write_verdict_labels(item.issue, ctx, is_go=True)
            if ctx.config.enable_learn:
                return Continue(next_state="LEARN_WAIT")
            return StageOutcome(Disposition.ADVANCE, "plan approved (learn disabled)")

        # NOGO (or AMBIGUOUS, treated as not-GO): amend within the
        # cycle-relative budget.
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

        # Iteration cap: durably apply state:plan-no-go, then fail back —
        # "nogo" while plan_cycles remain, "plan_cycles_exhausted" once the
        # cycle budget is consumed (routes to finished(fail) via ROUTES).
        logger.warning(
            "plan_review:%d: %s exhausted (round %d/%d); applying no-go label",
            item.issue,
            verdict.verdict,
            round_done,
            budget_iter,
        )
        self._write_verdict_labels(item.issue, ctx, is_go=False)

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

    @staticmethod
    def _write_verdict_labels(issue_number: int, ctx: StageContext, *, is_go: bool) -> None:
        """Apply the verdict's label pair, each write non-fatal.

        The add and the remove are wrapped independently so an exception
        between the pair never propagates half-applied — the reviewer's
        verdict comment remains the ultimate fallback for the backfill path.

        Args:
            issue_number: GitHub issue number.
            ctx: Stage context carrying the GitHub accessor.
            is_go: True for a GO verdict, False for NOGO-exhausted.

        """
        label_to_add, labels_to_remove = apply_plan_verdict(is_go=is_go)
        try:
            ctx.github.add_labels(issue_number, [label_to_add])
        except Exception as e:
            logger.warning(
                "plan_review:%d: failed to add label %r (non-fatal): %s",
                issue_number,
                label_to_add,
                e,
            )
        try:
            ctx.github.remove_labels(issue_number, labels_to_remove)
        except Exception as e:
            logger.warning(
                "plan_review:%d: failed to remove labels %s (non-fatal): %s",
                issue_number,
                labels_to_remove,
                e,
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

        if result.value:
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
                item.payload["plan_text"] = plan_text
                if item.issue is not None and isinstance(plan_text, str):
                    ctx.github.upsert_plan_comment(
                        item.issue,
                        _normalize_plan_comment(plan_text),
                    )
            # LEARN_WAIT intentionally has no branch: the learn job's output
            # is a side effect for the Mnemosyne skill store, not a payload
            # value any later state consumes.
