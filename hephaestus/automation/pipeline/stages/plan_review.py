"""Plan-review stage: review, amend, and approve issue plans (issue #1814).

Re-houses the plan-review control flow from
``planner_review_loop.py::PlanReviewLoop.run`` as a pipeline stage
(docs/AUTOMATION_LOOP_ARCHITECTURE.md section "3. plan_review" is the
binding contract):

- States: ENTER -> REVIEW_WAIT -> EVAL -> AMEND_WAIT -> (loop) -> LEARN_WAIT.
- Budgets: ``plan_review_iter`` = 3 (max review iterations),
  ``plan_cycles`` = 2 (max plan->review->amend cycles before giving up).
- Owned labels: ``state:plan-go`` (GO) [durable], ``state:plan-no-go``
  (exhausted) [durable] — both computed by the shared pure
  ``state_labels.apply_plan_verdict`` so this stage and the legacy loop
  apply identical transitions.
- Verdict parsed IN-WORKER by ``claude_invoke.parse_review_verdict``
  (carried as the review job's ``parse`` callable).
- ERROR verdicts leave labels untouched and RETRY (reviewer-infrastructure
  failure must not stamp a go/no-go label, #911).
- Prompt functions (imported, never re-authored):
  ``prompts/planning.py get_plan_loop_review_prompt``,
  ``prompts/planning.py get_plan_prompt`` (amend), and
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
from hephaestus.automation.prompts.planning import (
    get_plan_loop_review_prompt,
    get_plan_prompt,
)
from hephaestus.automation.session_naming import AGENT_PLAN_REVIEWER, AGENT_PLANNER
from hephaestus.automation.state_labels import apply_plan_verdict

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
)

logger = logging.getLogger(__name__)


class PlanReviewStage(Stage):
    """Stage for reviewing and iterating on a plan.

    State machine (doc section "3. plan_review"):

    - ENTER: route to REVIEW_WAIT (no [M] entry step in the doc contract).
    - REVIEW_WAIT: submit the reviewer job; the verdict is parsed in-worker
      by ``parse_review_verdict`` and lands in
      ``item.payload["review_verdict"]``.
    - EVAL [M]: GO -> durably apply ``state:plan-go`` (write BEFORE the
      advancing outcome) then learn step or ADVANCE; NOGO within the
      iteration budget -> AMEND_WAIT; NOGO/AMBIGUOUS at the cap -> durably
      apply ``state:plan-no-go``, then FAIL_BACK("nogo") while plan_cycles
      remain or FAIL_BACK("plan_cycles_exhausted") once exhausted; ERROR ->
      labels untouched, RETRY.
    - AMEND_WAIT: submit the planner amend job (planner session resumed with
      the reviewer feedback block by the worker, #1817), loop to REVIEW_WAIT.
    - LEARN_WAIT (GO only, gated by ``enable_learn``): submit the learn job,
      then ADVANCE.

    Fail routes (ROUTES): "nogo" -> planning, "plan_cycles_exhausted" ->
    finished(fail).
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """No durable entry work: the doc defines no [M] entry step here.

        Idempotent by construction (nothing is written). Label fast-forward
        for items already at-or-past ``state:plan-go`` happens in the
        planning stage's on_enter before the item is ever routed here.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
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
            iteration = item.attempts.get("plan_review_iter", 0) + 1
            item.attempts["plan_review_iter"] = iteration
            logger.info(
                "plan_review:%d: requesting review job (iteration %d)", item.issue, iteration
            )
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=AGENT_PLAN_REVIEWER,
                model=reviewer_model(),
                prompt_builder=get_plan_loop_review_prompt,
                cwd=ctx.paths.worktree,
                timeout_s=plan_reviewer_claude_timeout(),
                # get_plan_loop_review_prompt takes a 0-based iteration index
                # (full-sweep suffix on the final iteration). Issue title/body
                # are seeded into item.payload by the coordinator (#1817).
                prompt_kwargs={
                    "issue_number": item.issue,
                    "issue_title": item.payload.get("issue_title", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                    "plan_text": item.payload.get("plan_text", ""),
                    "learnings": item.payload.get("learnings", ""),
                    "iteration": iteration - 1,
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
                agent=AGENT_PLANNER,
                model=planner_model(),
                prompt_builder=get_plan_prompt,
                cwd=ctx.paths.worktree,
                timeout_s=planner_claude_timeout(),
                # The worker resumes the planner session and prepends the
                # reviewer feedback block from item.payload["prior_review"]
                # (doc: "resume planner session with feedback block"; mirrors
                # planner_review_loop.generate_plan(prior_review=...); #1817
                # wires that session setup).
                prompt_kwargs={"issue_number": item.issue},
                descr="amend",
            )
            return JobRequest(job, on_done_state="REVIEW_WAIT")

        if item.state == "LEARN_WAIT":
            logger.info("plan_review:%d: requesting learn job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=AGENT_PLANNER,  # resume the planner's own session (legacy parity)
                model=planner_model(),
                prompt_builder=build_learn_prompt,
                cwd=ctx.paths.worktree,
                timeout_s=learn_claude_timeout(),
                prompt_kwargs={"context": item.payload.get("plan_text", "")},
                descr="learn",
            )
            return JobRequest(job, on_done_state="FINISH")

        if item.state == "FINISH":
            logger.info("plan_review:%d: learn completed; advancing", item.issue)
            return StageOutcome(Disposition.ADVANCE, "plan approved and learned")

        logger.warning("plan_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """EVAL [M]: decide the next action from the parsed reviewer verdict.

        Every durable label write below happens BEFORE the outcome that
        causes a queue push (the load-bearing pipeline invariant).
        """
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        verdict = item.payload.get("review_verdict")
        if verdict is None:
            logger.warning("plan_review:%d: no verdict found; retry", item.issue)
            return StageOutcome(Disposition.RETRY, "no verdict found")

        # ERROR = reviewer-infrastructure failure: labels untouched, RETRY
        # (must not stamp a go/no-go label, #911).
        if verdict.is_error:
            logger.warning("plan_review:%d: reviewer error; retry", item.issue)
            return StageOutcome(Disposition.RETRY, "reviewer error")

        if verdict.is_go:
            logger.info("plan_review:%d: GO verdict; applying label and advancing", item.issue)
            label_to_add, labels_to_remove = apply_plan_verdict(is_go=True)
            ctx.github.add_labels(item.issue, [label_to_add])
            ctx.github.remove_labels(item.issue, labels_to_remove)
            if ctx.config.enable_learn:
                return Continue(next_state="LEARN_WAIT")
            return StageOutcome(Disposition.ADVANCE, "plan approved (learn disabled)")

        # NOGO (or AMBIGUOUS, treated as not-GO): amend within budget
        iteration = item.attempts.get("plan_review_iter", 0)
        budget_iter = ctx.budget("plan_review_iter")
        if iteration < budget_iter:
            logger.info(
                "plan_review:%d: %s verdict (iteration %d/%d); amending plan",
                item.issue,
                verdict.verdict,
                iteration,
                budget_iter,
            )
            return Continue(next_state="AMEND_WAIT")

        # Iteration cap: durably apply state:plan-no-go, then fail back —
        # "nogo" while plan_cycles remain, "plan_cycles_exhausted" once the
        # cycle budget is consumed (routes to finished(fail) via ROUTES).
        logger.warning(
            "plan_review:%d: %s exhausted (iteration %d/%d); applying no-go label",
            item.issue,
            verdict.verdict,
            iteration,
            budget_iter,
        )
        label_to_add, labels_to_remove = apply_plan_verdict(is_go=False)
        ctx.github.add_labels(item.issue, [label_to_add])
        ctx.github.remove_labels(item.issue, labels_to_remove)

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
                item.payload["review_verdict"] = result.value
                # Feed the raw review text to the next amend/review round,
                # mirroring the legacy loop's prior_review threading.
                item.payload["prior_review"] = getattr(result.value, "raw", str(result.value))
            elif item.state == "AMEND_WAIT":
                item.payload["plan_text"] = result.value
