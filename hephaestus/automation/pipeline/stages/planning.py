"""Planning stage: generate and verify issue plans (issue #1814).

Re-houses the planning control flow from ``planner.py::Planner._plan_issue``
as a pipeline stage (docs/AUTOMATION_LOOP_ARCHITECTURE.md section
"2. planning" is the binding contract):

- States: ENTER -> ADVISE_WAIT -> PLAN_WAIT -> VERIFY.
- Budget: ``plan`` = 2 (max plan attempts per issue); exhaustion ->
  finished(fail).
- Owned label: ``state:needs-plan`` (idempotent, on entry) [durable].
- Prompt functions (imported, never re-authored):
  ``prompts/advise.py get_advise_prompt_builder`` and
  ``prompts/planning.py get_plan_prompt``.
"""

from __future__ import annotations

import logging

from hephaestus.automation.agent_config import (
    advise_claude_timeout,
    advise_model,
    planner_claude_timeout,
    planner_model,
)
from hephaestus.automation.prompts.advise import get_advise_prompt_builder
from hephaestus.automation.prompts.planning import get_plan_prompt
from hephaestus.automation.session_naming import AGENT_ADVISE, AGENT_PLANNER
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    is_plan_go,
    is_skipped,
)

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


def _issue_labels(item: WorkItem, ctx: StageContext) -> list[str]:
    """Refresh the item's labels from GitHub and update ``labels_cache``.

    Reads through ``ctx.github.gh_issue_json`` (mirrors
    ``github_api.issues.gh_issue_json``); on any read failure the cached
    labels are used so a transient API blip cannot mis-route the item.
    """
    if item.issue is None:
        return []
    try:
        data = ctx.github.gh_issue_json(item.issue)
    except Exception as e:  # transient gh failure: fall back to cache
        logger.warning("planning:%d: label refresh failed (using cache): %s", item.issue, e)
        return list(item.labels_cache)
    raw = data.get("labels", []) if isinstance(data, dict) else []
    labels = [entry["name"] if isinstance(entry, dict) else str(entry) for entry in raw]
    item.labels_cache = dict.fromkeys(labels, True)
    return labels


class PlanningStage(Stage):
    """Stage for planning an issue: advise -> plan -> verify.

    State machine (doc section "2. planning"):

    - ENTER: route to ADVISE_WAIT (or PLAN_WAIT when advise is disabled).
    - ADVISE_WAIT: submit the advise agent job; findings land in
      ``item.payload["advise_findings"]``.
    - PLAN_WAIT: submit the plan agent job (planner session); plan text
      lands in ``item.payload["plan_text"]``; the plan comment posted by the
      pipeline is the durable artifact.
    - VERIFY: check the plan comment exists -> ADVANCE, else RETRY within
      the ``plan`` budget, then FINISH_FAIL.

    on_enter idempotency guards (re-housed from ``Planner._pr_coverage_skip``
    and ``Planner._has_existing_plan``, all ordered at-or-past checks):

    - already at-or-past ``state:plan-go`` -> ADVANCE (zero jobs)
    - ``state:skip`` -> SKIP
    - merged closing PR -> close issue as covered, SKIP
    - open PR -> SKIP (PR already covers implementation)
    - unlabeled entry -> durably add ``state:needs-plan`` before proceeding
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

        labels = _issue_labels(item, ctx)

        # Fast-forward (at-or-past, never equality): already plan-go -> ADVANCE
        if is_plan_go(labels):
            logger.info("planning:%d: already plan-go; advancing", item.issue)
            return StageOutcome(Disposition.ADVANCE, "plan already approved")

        # Operator override: state:skip -> SKIP
        if is_skipped(labels):
            logger.info("planning:%d: state:skip; skipping", item.issue)
            return StageOutcome(Disposition.SKIP, "state:skip")

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

        # Owned label: state:needs-plan, idempotent durable write before proceeding
        if STATE_NEEDS_PLAN not in labels:
            logger.info("planning:%d: adding %s label", item.issue, STATE_NEEDS_PLAN)
            ctx.github.add_labels(item.issue, [STATE_NEEDS_PLAN])

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
                agent=AGENT_ADVISE,
                model=advise_model(),
                prompt_builder=get_advise_prompt_builder(ctx.config.agent),
                cwd=ctx.paths.worktree,
                timeout_s=advise_claude_timeout(),
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
                agent=AGENT_PLANNER,
                model=planner_model(),
                prompt_builder=get_plan_prompt,
                cwd=ctx.paths.worktree,
                timeout_s=planner_claude_timeout(),
                # The worker prepends the issue context + advise findings when
                # opening the planner session, exactly as the legacy
                # planner_review_loop.generate_plan composes it (#1817 wires
                # that session setup; the prompt template itself is reused
                # verbatim via get_plan_prompt).
                prompt_kwargs={"issue_number": item.issue},
                descr="plan",
            )
            return JobRequest(job, on_done_state="VERIFY")

        if item.state == "VERIFY":
            # Doc step 4 [M]: verify the plan comment exists (the
            # PlannerStateManager.has_existing_plan read, via ctx.github).
            if ctx.github.has_existing_plan(item.issue):
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
                return StageOutcome(Disposition.RETRY, f"plan not found, retry {attempt}/{budget}")
            logger.error(
                "planning:%d: plan not found after %d attempts; exhausted", item.issue, budget
            )
            return StageOutcome(Disposition.FINISH_FAIL, f"plan not found after {budget} attempts")

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

        if result.value:
            if item.state == "ADVISE_WAIT":
                item.payload["advise_findings"] = result.value
            elif item.state == "PLAN_WAIT":
                item.payload["plan_text"] = result.value
