"""Merge-wait stage: strictly-gated auto-merge arming and post-merge learning.

Re-houses ``ci_driver._arm_and_wait_for_merge`` (:584) /
``_wait_for_pr_terminal`` (:1492) / ``_resolve_dirty_pr`` (:923) /
``_resolve_blocked_pr`` (:986) as a pipeline stage
(docs/AUTOMATION_LOOP_ARCHITECTURE.md section "7. merge_wait" is the
binding contract).

ARM reads the live PR head, requires an authenticated strict-GO artifact for
that exact head, requests squash auto-merge, and confirms that GitHub recorded
the arm.  Any failed or ambiguous proof condition disables auto-merge and
finishes failed; a merge observed in an arm race uses the normal deduplicated
post-merge learn path.  POLL revalidates the proof before parking, so a head
change revokes eligibility rather than waiting on a stale arm.

- States: ENTER -> ARM -> POLL -> LEARN_WAIT -> MW_FINISH.
- ARM [M]: prepare proof from the live head, arm, confirm, then record the
  arming state before polling.  This is the only automatic call site for
  ``arm_auto_merge``.
- POLL [M]: re-read PR state and the exact-head proof.  An open, still-proven
  auto-merge arm timer-parks; a missing proof is disarmed and terminalized;
  merged and closed PRs finish normally.
- LEARN_WAIT [W:A]: records the post-merge learning outcome before terminal
  finish, so a restart cannot replay ``/learn`` for the same merge.
- Zero ``time.sleep`` / ``import time`` in this module (AC1) — the coordinator
  owns timer parking.
"""

from __future__ import annotations

import logging

from hephaestus.automation.agent_config import implementer_model, learn_claude_timeout
from hephaestus.automation.learn import build_learn_prompt
from hephaestus.automation.session_naming import AGENT_CI_DRIVER

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

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
ARM = "ARM"
POLL = "POLL"
LEARN_WAIT = "LEARN_WAIT"
MW_FINISH = "MW_FINISH"
FINISH = MW_FINISH


def build_drive_green_learn_prompt(issue_number: int, pr_number: int) -> str:
    """Compose the post-merge drive-green /learn prompt (built in-worker).

    Module-level composed builder (NOT a closure): :class:`AgentJob` is frozen
    and prompt builders run in-worker, so the builder must be a top-level
    function receiving everything via ``prompt_kwargs`` (mirrors
    :func:`..ci.build_ci_fix_prompt`). Reuses ``learn.build_learn_prompt``
    verbatim with the drive-green context string re-housed from
    ``post_merge_processor.run_drive_green_learnings`` so the learnings are
    scoped to what made CI fail and how it was fixed.

    Args:
        issue_number: GitHub issue number the merged PR closed.
        pr_number: GitHub PR number that reached green and merged.

    Returns:
        The full /learn prompt string.

    """
    return build_learn_prompt(
        f"You just drove PR #{pr_number} (issue #{issue_number}) "
        "to green CI. Capture concise learnings about what made CI fail and how "
        "you fixed it, scoped to this issue/PR."
    )


class MergeWaitStage(Stage):
    """Stage: contain auto-merge until the strict-review gate is available."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Initialize the mini-state; the durable arming lives in ARM.

        ARM is an [M] step of this stage so a restart re-runs it
        idempotently via step() (an armed item skips re-arming). Nothing
        durable is written here.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
        if not item.state:
            item.state = ENTER
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next merge-wait action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if item.state == ENTER:
            return Continue(next_state=ARM)
        if item.state == ARM:
            return self._arm(item, ctx)
        if item.state == POLL:
            return self._poll(item, ctx)
        if item.state == LEARN_WAIT:
            return self._request_learn(item, ctx)
        if item.state == MW_FINISH:
            # The PR merged; /learn already ran (best-effort) and its result
            # was durably marked in on_job_done. A failed learn never flips
            # a merged PR to failure.
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        logger.warning("merge_wait:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _arm(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Prepare, arm, and confirm exactly one current-head strict GO proof.

        Every read can race with a manual merge.  A merge observed before the
        attempt, after a failed arm, or during confirmation takes the normal
        deduplicated post-merge route; all other uncertainty fails closed.
        """
        if item.pr is None:
            logger.warning("merge_wait:%s: no PR on item; finishing failed", item.issue)
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        item.payload.setdefault("merge_wait_started_at", ctx.now())
        pr_state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(pr_state, item.pr)
        if terminal is not None:
            if terminal.disposition is Disposition.FINISH_PASS:
                return self._route_merged(item, ctx)
            return terminal
        head_sha = str((pr_state or {}).get("headRefOid") or "")
        artifact_reader = getattr(ctx.github, "strict_review_artifact", None)
        artifact = artifact_reader(item.pr, head_sha) if callable(artifact_reader) else None
        if artifact is None or not bool(getattr(artifact, "is_go", False)):
            return self._disable_and_fail(item, ctx, "strict_gate_unavailable")
        try:
            ctx.github.arm_auto_merge(item.pr)
        except Exception as exc:
            raced = ctx.github.gh_pr_state(item.pr)
            raced_terminal = _terminal_pr_outcome(raced, item.pr)
            if raced_terminal is not None and raced_terminal.disposition is Disposition.FINISH_PASS:
                logger.info("merge_wait: PR #%d merged while arming: %s", item.pr, exc)
                return self._route_merged(item, ctx)
            logger.warning("merge_wait: failed to arm auto-merge for PR #%d: %s", item.pr, exc)
            return StageOutcome(Disposition.FINISH_FAIL, "arm_failed")
        confirmed = ctx.github.gh_pr_state(item.pr)
        confirmed_terminal = _terminal_pr_outcome(confirmed, item.pr)
        if confirmed_terminal is not None:
            if confirmed_terminal.disposition is Disposition.FINISH_PASS:
                return self._route_merged(item, ctx)
            return confirmed_terminal
        if not (confirmed or {}).get("autoMergeRequest"):
            return self._disable_and_fail(item, ctx, "arm_confirm_failed")
        if item.issue is not None:
            try:
                ctx.github.arm_drive_green(item.issue, item.pr, head_sha)
            except Exception as exc:
                logger.error(
                    "merge_wait: failed to persist arm record for PR #%d: %s",
                    item.pr,
                    exc,
                )
                return self._disable_and_fail(item, ctx, "arm_record_failed")
        item.armed = True
        return Continue(next_state=POLL)

    @staticmethod
    def _disable_and_fail(item: WorkItem, ctx: StageContext, note: str) -> StageOutcome:
        """Contain a failed gate condition by disabling auto-merge first."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        try:
            ctx.github.defer_auto_merge(item.pr)
        except Exception as exc:
            logger.error(
                "merge_wait: failed to disable auto-merge for PR #%d: %s",
                item.pr,
                exc,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        item.armed = False
        return StageOutcome(Disposition.FINISH_FAIL, note)

    def _poll(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """POLL only the post-merge state needed for the deduped learn path."""
        if item.pr is None:  # guarded by ARM; kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        gh_state = ctx.github.gh_pr_state(item.pr)
        pr_state_str = ((gh_state or {}).get("state") or "").upper()
        if pr_state_str not in {"MERGED", "CLOSED"}:
            head_sha = str((gh_state or {}).get("headRefOid") or "")
            artifact_reader = getattr(ctx.github, "strict_review_artifact", None)
            artifact = artifact_reader(item.pr, head_sha) if callable(artifact_reader) else None
            if item.armed and artifact is not None and bool(getattr(artifact, "is_go", False)):
                started = item.payload.get("merge_wait_started_at")
                if started is None:
                    return StageOutcome(Disposition.FINISH_FAIL, "missing_merge_wait_started_at")
                # Auto-merge is owned by GitHub after the confirmed arm.  The
                # coordinator only parks and re-reads; it never sleeps or
                # performs another merge mutation from this state.
                item.payload["retry_delay_s"] = 30
                return StageOutcome(Disposition.RETRY, "merge_pending")
            try:
                ctx.github.defer_auto_merge(item.pr)
            except Exception as exc:
                logger.error(
                    "merge_wait:%s: failed to verify auto-merge disabled for PR #%d: %s",
                    item.issue,
                    item.pr,
                    exc,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
            item.armed = False
            return StageOutcome(Disposition.FINISH_FAIL, "strict_gate_unavailable")

        started = item.payload.get("merge_wait_started_at")
        if started is None:
            logger.error(
                "merge_wait:%s: PR #%d reached POLL without merge_wait_started_at",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "missing_merge_wait_started_at")

        if pr_state_str == "MERGED":
            return self._route_merged(item, ctx)
        if pr_state_str == "CLOSED":
            logger.info("merge_wait:%s: PR #%d closed without merging", item.issue, item.pr)
            return StageOutcome(Disposition.FINISH_FAIL, "closed")
        return StageOutcome(Disposition.FINISH_FAIL, "strict_gate_unavailable")

    def _route_merged(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dedupe the MERGED PR's post-merge /learn via the arming record.

        The /learn leg fires at most once per merged PR (#848): a terminal
        learn record (``ctx.github.drive_green_learn_terminal``) — or learn
        disabled by config — finishes PASS immediately without dispatching
        the session.
        """
        if not getattr(ctx.config, "enable_learn", True):
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if item.issue is not None and ctx.github.drive_green_learn_terminal(item.issue):
            logger.info(
                "merge_wait:%d: /learn already terminal for PR #%s; deduped",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        return Continue(next_state=LEARN_WAIT)

    def _request_learn(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """LEARN_WAIT [W:A]: dispatch the deduped post-merge /learn session.

        Prompt composed in-worker by :func:`build_drive_green_learn_prompt`.
        The dedupe already held at ``_route_merged`` (a terminal record never
        reaches here); ``on_job_done`` durably marks the outcome on the
        arming record BEFORE MW_FINISH, closing the exactly-once loop.
        """
        job = AgentJob(
            repo=item.repo,
            issue=item.issue if item.issue is not None else 0,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=build_drive_green_learn_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=learn_claude_timeout(),
            session_agent=AGENT_CI_DRIVER,
            prompt_kwargs={
                "issue_number": item.issue if item.issue is not None else 0,
                "pr_number": item.pr,
            },
            descr="drive_green_learn",
        )
        return JobRequest(job, on_done_state=MW_FINISH)

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Record the post-merge learn result without changing a merged PR's outcome."""
        if item.state != LEARN_WAIT:
            return
        if not result.ok:
            logger.warning(
                "merge_wait:%s: post-merge /learn failed (non-fatal): %s",
                item.issue,
                result.error,
            )
        if item.issue is not None:
            try:
                ctx.github.mark_drive_green_learn_result(item.issue, succeeded=bool(result.ok))
            except Exception as exc:
                logger.warning(
                    "merge_wait:%d: failed to mark /learn result (non-fatal): %s",
                    item.issue,
                    exc,
                )
