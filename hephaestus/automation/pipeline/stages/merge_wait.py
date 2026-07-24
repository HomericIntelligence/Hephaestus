"""Merge-wait: one in-process auto-merge request and post-merge learning.

``pr_review`` owns the only automated GO/NOGO decision and writes the
loop-owned ``state:implementation-go`` label.  This stage consumes that
label once, asks GitHub to arm auto-merge for the live head, and then polls
only the arm it created.  It deliberately does not recover, confirm, retry,
or take over an arm created by another process or run: those operational
states are blocked and left for an operator.

The implemented mini-state graph is:

- Open PR: ENTER -> ARM -> FINISH_FAIL(``strict_gate_unavailable``) after
  auto-merge disablement is verified.
- Already-merged PR: ENTER -> ARM -> POLL -> LEARN_WAIT -> MW_FINISH,
  preserving the exactly-once post-merge learning contract through
  ``ctx.github.drive_green_learn_terminal``.

Dirty/blocked recovery and automatic arming are not dormant branches in this
module. Issue #2055 must introduce those transitions explicitly behind a
head-bound strict-review proof rather than reviving undocumented legacy state.
"""

from __future__ import annotations

import logging

from hephaestus.automation.agent_config import implementer_model, learn_claude_timeout
from hephaestus.automation.learn import build_learn_prompt
from hephaestus.automation.session_naming import AGENT_LEARNINGS
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
    _terminal_pr_outcome,
    _worktree_path,
    agent_provider,
    stage_model,
)

logger = logging.getLogger(__name__)

ENTER = "ENTER"
ARM = "ARM"
POLL = "POLL"
LEARN_WAIT = "LEARN_WAIT"
MW_FINISH = "MW_FINISH"
FINISH = MW_FINISH


def build_drive_green_learn_prompt(issue_number: int, pr_number: int) -> str:
    """Compose the post-merge learning prompt in the worker."""
    return build_learn_prompt(
        PromptCatalog.current().render(
            "learn/drive_green_context.j2", issue_number=issue_number, pr_number=pr_number
        )
    )


class MergeWaitStage(Stage):
    """Arm once from the loop-owned approval label, then observe that arm."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Reject an unscoped PR without altering any operator-owned arm."""
        del ctx
        if item.issue is None:
            logger.warning(
                "merge_wait: PR #%s has no requirements context; operator action required", item.pr
            )
            return StageOutcome(Disposition.FINISH_FAIL, "merge_wait_orphan")
        if not item.state:
            item.state = ENTER
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the current merge-wait mini-state."""
        if item.state == ENTER:
            return Continue(next_state=ARM)
        if item.state == ARM:
            return self._arm(item, ctx)
        if item.state == POLL:
            return self._poll(item, ctx)
        if item.state == LEARN_WAIT:
            return self._request_learn(item, ctx)
        if item.state == MW_FINISH:
            if item.payload.pop("learn_result_persistence_failed", None):
                return StageOutcome(Disposition.FINISH_FAIL, "learn_result_persistence_failed")
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        logger.warning("merge_wait:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _arm(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Issue exactly one auto-merge request for a current-run approval."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(pr_state, item.pr)
        if terminal is not None:
            return (
                self._route_merged(item, ctx)
                if terminal.disposition is Disposition.FINISH_PASS
                else terminal
            )
        if pr_state is None:
            logger.warning(
                "merge_wait: PR #%d state unavailable; operator action required", item.pr
            )
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if pr_state.get("autoMergeRequest"):
            logger.warning(
                "merge_wait: PR #%d is already armed; leaving it to the operator", item.pr
            )
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
        if not has_go:
            return StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
        head_sha = str(pr_state.get("headRefOid") or "")
        if not head_sha:
            logger.warning(
                "merge_wait: PR #%d has no readable head; operator action required", item.pr
            )
            return StageOutcome(Disposition.FINISH_FAIL, "missing_pr_head")
        try:
            ctx.github.arm_auto_merge(item.pr, head_sha)
        except Exception as exc:
            logger.warning(
                "merge_wait: auto-merge request for PR #%d failed (%s); operator action required",
                item.pr,
                exc,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_arm_failed")
        item.armed = True
        return Continue(next_state=POLL)

    def _poll(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Poll only the arm created by this run; never reconcile external state."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(pr_state, item.pr)
        if terminal is not None:
            return (
                self._route_merged(item, ctx)
                if terminal.disposition is Disposition.FINISH_PASS
                else terminal
            )
        if pr_state is None:
            logger.warning(
                "merge_wait: PR #%d state unavailable; operator action required", item.pr
            )
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
        if not has_go:
            logger.warning(
                "merge_wait: PR #%d approval disappeared; operator action required", item.pr
            )
            return StageOutcome(Disposition.FINISH_FAIL, "not_implementation_go")
        if not pr_state.get("autoMergeRequest"):
            logger.warning(
                "merge_wait: PR #%d is no longer armed; operator action required", item.pr
            )
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_no_longer_armed")
        if not item.armed:
            logger.warning(
                "merge_wait: PR #%d was armed outside this run; leaving it to the operator", item.pr
            )
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        attempt = item.attempts.get("merge", 0) + 1
        item.attempts["merge"] = attempt
        budget = ctx.budget("merge")
        if attempt >= budget:
            logger.warning(
                "merge_wait:%s: PR #%d remains pending after %d/%d own-arm polls; stopping",
                item.issue,
                item.pr,
                attempt,
                budget,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "merge_wait_exhausted")
        item.payload["retry_delay_s"] = 30
        return StageOutcome(Disposition.RETRY, "merge_pending")

    def _route_merged(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dispatch the existing deduplicated post-merge learning step."""
        if item.issue is None or not getattr(ctx.config, "enable_learn", True):
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if ctx.github.drive_green_learn_terminal(item.issue):
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if ctx.github.drive_green_learn_inflight(item.issue):
            logger.error("merge_wait:%d: post-merge learning outcome is unknown", item.issue)
            return StageOutcome(Disposition.FINISH_FAIL, "learn_outcome_unknown")
        return Continue(next_state=LEARN_WAIT)

    def _request_learn(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dispatch the existing post-merge learning job exactly once."""
        if item.issue is None or item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "missing_learn_scope")
        try:
            claimed = ctx.github.claim_drive_green_learn(item.issue, item.pr)
        except Exception as exc:
            logger.error("merge_wait:%d: failed to claim /learn dispatch: %s", item.issue, exc)
            return StageOutcome(Disposition.FINISH_FAIL, "learn_claim_failed")
        if not claimed:
            return StageOutcome(Disposition.FINISH_FAIL, "learn_outcome_unknown")
        job = AgentJob(
            repo=item.repo,
            issue=item.issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=build_drive_green_learn_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=learn_claude_timeout(),
            session_agent=AGENT_LEARNINGS,
            prompt_kwargs={"issue_number": item.issue, "pr_number": item.pr},
            descr="drive_green_learn",
        )
        return JobRequest(job, on_done_state=MW_FINISH)

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Persist the post-merge learning result without changing merge outcome."""
        if item.state != LEARN_WAIT or item.issue is None:
            return
        try:
            ctx.github.mark_drive_green_learn_result(item.issue, succeeded=bool(result.ok))
        except Exception as exc:
            logger.error("merge_wait:%d: failed to persist /learn result: %s", item.issue, exc)
            item.payload["learn_result_persistence_failed"] = True
