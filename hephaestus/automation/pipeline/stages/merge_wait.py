"""Merge-wait: reviewed-head interlock and post-merge learning.

``pr_review`` owns the only automated GO/NOGO decision and writes the
loop-owned ``state:implementation-go`` label.  It consumes an in-memory
reviewed-head proof only when it matches the live, confirmed-unarmed PR head.
Until #2419 supplies a separately reviewed conditional normal-merge path, a
matching proof stands down safely: this stage never creates, disables, adopts,
or polls an auto-merge request.

The implemented mini-state graph is:

- Open PR: ENTER -> ARM -> FINISH_FAIL(``merge_wait_standing_by``) after
  reviewed-head and confirmed-unarmed checks.
- Already-merged PR: ENTER -> ARM -> LEARN_WAIT -> MW_FINISH,
  preserving the exactly-once post-merge learning contract through
  ``ctx.github.drive_green_learn_terminal``.

The persistent approval label is deliberately insufficient after a restart or
direct ``--prs`` seed because the reviewed-head proof is process-local.  Those
paths revoke the stale label only after a fresh confirmed-unarmed state read
and return to review; merge-wait-only scope consequently terminates safely.
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
    _is_confirmed_open_unarmed,
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
    """Verify a reviewed approval without creating persistent merge authority."""

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
        """Consume only a matching current-review proof, then stand down."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_state, terminal = self._read_confirmed_open_unarmed(item, ctx)
        if terminal is not None:
            return terminal
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
        if not has_go:
            return StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
        head_sha = str(pr_state.get("headRefOid") or "")
        if not head_sha:
            logger.warning(
                "merge_wait: PR #%d has no readable head; operator action required", item.pr
            )
            return StageOutcome(Disposition.FINISH_FAIL, "missing_pr_head")
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        if reviewed_head != head_sha:
            return self._revoke_stale_reviewed_head(item, ctx, reviewed_head)
        return StageOutcome(Disposition.FINISH_FAIL, "merge_wait_standing_by")

    def _poll(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Stand down when a live unarmed PR reaches the retired poll state."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        _pr_state, terminal = self._read_confirmed_open_unarmed(item, ctx)
        if terminal is not None:
            return terminal
        return StageOutcome(Disposition.FINISH_FAIL, "merge_wait_standing_by")

    def _read_confirmed_open_unarmed(
        self, item: WorkItem, ctx: StageContext
    ) -> tuple[dict[str, object] | None, StepResult | None]:
        """Read one PR state and return it only when it is complete, open, and unarmed."""
        if item.pr is None:
            return None, StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(pr_state, item.pr)
        if terminal is not None:
            result = (
                self._route_merged(item, ctx)
                if terminal.disposition is Disposition.FINISH_PASS
                else terminal
            )
            return None, result
        if pr_state is None:
            logger.warning(
                "merge_wait: PR #%d state unavailable; operator action required", item.pr
            )
            return None, StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if pr_state.get("autoMergeRequest") is not None:
            logger.warning(
                "merge_wait: PR #%d is already armed; leaving it to the operator", item.pr
            )
            return None, StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(pr_state):
            return None, StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        return pr_state, None

    def _revoke_stale_reviewed_head(
        self, item: WorkItem, ctx: StageContext, reviewed_head: str
    ) -> StepResult:
        """Re-read before revoking a stale label and return to review if still stale."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        current_state, terminal = self._read_confirmed_open_unarmed(item, ctx)
        if terminal is not None:
            return terminal
        if current_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        current_head = str(current_state.get("headRefOid") or "")
        if not current_head:
            return StageOutcome(Disposition.FINISH_FAIL, "missing_pr_head")
        if reviewed_head == current_head:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_wait_standing_by")
        try:
            ctx.github.mark_pr_implementation_no_go(item.pr)
        except Exception as exc:
            logger.warning(
                "merge_wait: could not revoke stale approval on PR #%d (%s)", item.pr, exc
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_label_failed")
        if not reviewed_head:
            return StageOutcome(Disposition.FAIL_BACK, "reviewed_head_missing")
        return StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")

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
