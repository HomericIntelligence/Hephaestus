"""Merge-wait stage: loop-owned auto-merge arming and post-merge learning.

Owns the queue pipeline's arming, terminal-state polling, and post-merge
learning handoff (docs/AUTOMATION_LOOP_ARCHITECTURE.md section "7. merge_wait"
is the binding contract).

ARM reads the live PR head, requires the loop-owned ``state:implementation-go``
label, requests squash auto-merge, and confirms that GitHub recorded the arm.
The label is the automation loop's sole merge authorization; no CI status,
external review artifact, or lease is consulted here.

- States: ENTER -> ARM -> POLL -> LEARN_WAIT -> MW_FINISH.
- ARM [M]: persist a prepared record, arm, confirm both GitHub and the
  durable record, then poll.  This is the only automatic call site for
  ``arm_auto_merge``.  Recovery first disarms any persisted remote arm, then
  returns to ARM for a fresh live-head and loop-owned-label read.
- POLL [M]: re-read PR state and the approval label. An open, still-approved
  auto-merge arm timer-parks; a missing label is disarmed and terminalized;
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

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
ARM = "ARM"
POLL = "POLL"
LEARN_WAIT = "LEARN_WAIT"
MW_FINISH = "MW_FINISH"
FINISH = MW_FINISH

# An absent arm is an operational state, not a reason to revoke the
# loop-owned authorization label.  Timer parking prevents a transient GitHub
# response from turning into an in-process busy loop.
_ARM_RETRY_DELAY_S = 30
_ARM_RETRY_LIMIT = 3


def build_drive_green_learn_prompt(issue_number: int, pr_number: int) -> str:
    """Compose the post-merge drive-green /learn prompt (built in-worker).

    Module-level composed builder (NOT a closure): :class:`AgentJob` is frozen
    and prompt builders run in-worker, so the builder must be a top-level
    function receiving everything via ``prompt_kwargs``. Reuses
    ``learn.build_learn_prompt``
    verbatim with the drive-green context string re-housed from
    ``post_merge_processor.run_drive_green_learnings`` so the learnings are
    scoped to the automation-loop review and merge path.

    Args:
        issue_number: GitHub issue number the merged PR closed.
        pr_number: GitHub PR number that merged.

    Returns:
        The full /learn prompt string.

    """
    return build_learn_prompt(
        PromptCatalog.current().render(
            "learn/drive_green_context.j2", issue_number=issue_number, pr_number=pr_number
        )
    )


class MergeWaitStage(Stage):
    """Stage: arm from the loop-owned approval label and contain label loss."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Initialize the mini-state and restore a confirmed arm to POLL.

        A recovery seed carries only a merge-wait record. It first disarms any
        remote arm, then starts from ARM, which reads the live PR head and the
        loop-owned approval label again. A merged PR still reaches the normal
        deduplicated learning path.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
        # A PR number is not requirements context.  In particular, an
        # unlinked direct ``--prs`` seed carrying a stale GO label must be
        # contained here rather than using that label to arm auto-merge.
        # Linked PRs still use the label as the sole durable authorization.
        if item.issue is None:
            outcome = self._disable_and_fail(item, ctx, "merge_wait_orphan")
            if isinstance(outcome, Continue):
                return StageOutcome(Disposition.FINISH_PASS, "merged")
            if isinstance(outcome, JobRequest):
                return StageOutcome(Disposition.FINISH_FAIL, "merge_wait_orphan")
            return outcome
        if item.payload.pop("merge_wait_recovery", False):
            if item.issue is None or item.pr is None:
                return StageOutcome(Disposition.FINISH_FAIL, "invalid_arm_recovery")
            try:
                # This is an ingress boundary, not an authorization check:
                # a persisted old arm can otherwise merge before ARM gets to
                # revalidate the loop-owned approval label.
                ctx.github.defer_auto_merge(item.pr)
            except Exception as exc:
                logger.error(
                    "merge_wait: failed to disarm recovered PR #%d before revalidation: %s",
                    item.pr,
                    exc,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
            item.armed = False
            item.payload.pop("merge_wait_head", None)
            item.state = ARM
            return None
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
            if item.payload.pop("learn_result_persistence_failed", None):
                return StageOutcome(Disposition.FINISH_FAIL, "learn_result_persistence_failed")
            # The PR merged; /learn already ran and its result was durably
            # marked in on_job_done. A failed learn is terminal too, but a
            # failed durable write is not allowed to masquerade as success.
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        logger.warning("merge_wait:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _arm(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901
        """Prepare, arm, and confirm one current-head approval label.

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
        has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
        if not has_go:
            return self._disable_and_fail(item, ctx, "not_implementation_go", recoverable=True)
        if not head_sha:
            return self._disable_and_fail(item, ctx, "missing_pr_head")
        if item.payload.pop("merge_wait_prepared_recovery", False) and bool(
            (pr_state or {}).get("autoMergeRequest")
        ):
            # The previous process may have crashed after GitHub accepted the
            # remote arm but before it durably recorded confirmation.  We now
            # hold a current approval label and saw the live arm, so promote
            # the prepared record and resume POLL without a second enable.
            if not self._confirm_arm(item, ctx, head_sha):
                return self._disable_and_fail(item, ctx, "arm_confirmation_record_failed")
            item.armed = True
            item.payload["merge_wait_head"] = head_sha
            return Continue(next_state=POLL)
        # Persist the recovery handoff before the remote arm. The arm RPC can
        # succeed and the process can die before it returns or confirms; a
        # later pipeline run needs this record to recover the merged PR's
        # deduplicated learn path.
        if not self._record_arm(item, ctx, head_sha):
            return StageOutcome(Disposition.FINISH_FAIL, "arm_record_failed")
        try:
            ctx.github.arm_auto_merge(item.pr, head_sha)
        except Exception as exc:
            logger.warning("merge_wait: arm response failed for PR #%d: %s", item.pr, exc)
            # A transport failure is ambiguous: read back first.  A confirmed
            # live arm remains valid under the loop-owned label; an absent arm
            # retries with a timer rather than blindly issuing a second arm.
            confirmed = ctx.github.gh_pr_state(item.pr)
            confirmed_terminal = _terminal_pr_outcome(confirmed, item.pr)
            if confirmed_terminal is not None:
                if confirmed_terminal.disposition is Disposition.FINISH_PASS:
                    return self._route_merged(item, ctx)
                return confirmed_terminal
            return self._handle_arm_readback(item, ctx, head_sha, confirmed)
        confirmed = ctx.github.gh_pr_state(item.pr)
        confirmed_terminal = _terminal_pr_outcome(confirmed, item.pr)
        if confirmed_terminal is not None:
            if confirmed_terminal.disposition is Disposition.FINISH_PASS:
                return self._route_merged(item, ctx)
            return confirmed_terminal
        return self._handle_arm_readback(item, ctx, head_sha, confirmed)

    def _handle_arm_readback(
        self,
        item: WorkItem,
        ctx: StageContext,
        requested_head: str,
        confirmed: dict[str, object] | None,
    ) -> StepResult:
        """Accept a live labelled arm or schedule a bounded operational retry."""
        confirmed_head = str((confirmed or {}).get("headRefOid") or "")
        confirmed_has_go, _confirmed_has_no_go = ctx.github.pr_has_implementation_state_label(
            item.pr or 0
        )
        if not confirmed_has_go:
            return self._disable_and_fail(item, ctx, "not_implementation_go", recoverable=True)
        if not confirmed_head or not (confirmed or {}).get("autoMergeRequest"):
            return self._retry_arm(item, "auto_merge_not_armed")
        # The label, not this transient head, is the authorization.  Store
        # the confirmed head only as arm-recovery metadata so a restart can
        # resume the post-merge learning handoff consistently.
        if confirmed_head != requested_head and not self._record_arm(item, ctx, confirmed_head):
            return self._disable_and_fail(item, ctx, "arm_confirmation_record_failed")
        if not self._confirm_arm(item, ctx, confirmed_head):
            # GitHub is armed but recovery cannot prove that fact durably.
            # Contain the remote arm before terminalizing rather than letting
            # a future restart re-enable an already-valid request.
            return self._disable_and_fail(item, ctx, "arm_confirmation_record_failed")
        item.armed = True
        item.payload["merge_wait_head"] = confirmed_head
        item.payload.pop("merge_wait_arm_retries", None)
        return Continue(next_state=POLL)

    @staticmethod
    def _retry_arm(item: WorkItem, note: str) -> StageOutcome:
        """Timer-park a label-preserving ARM retry without busy-spinning."""
        retries = int(item.payload.get("merge_wait_arm_retries", 0)) + 1
        item.payload["merge_wait_arm_retries"] = retries
        item.armed = False
        item.state = ARM
        if retries > _ARM_RETRY_LIMIT:
            logger.error("merge_wait:%s: auto-merge arm retry budget exhausted", item.issue)
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_arm_retry_exhausted")
        item.payload["retry_delay_s"] = _ARM_RETRY_DELAY_S
        return StageOutcome(Disposition.RETRY, note)

    @staticmethod
    def _record_arm(item: WorkItem, ctx: StageContext, head_sha: str) -> bool:
        """Persist a linked issue's arm record; orphan PRs have no learn scope."""
        if item.issue is None or item.pr is None:
            return True
        try:
            ctx.github.arm_drive_green(item.issue, item.pr, head_sha)
        except Exception as exc:
            logger.error(
                "merge_wait: failed to persist arm record for PR #%d: %s",
                item.pr,
                exc,
            )
            return False
        return True

    @staticmethod
    def _confirm_arm(item: WorkItem, ctx: StageContext, head_sha: str) -> bool:
        """Durably promote a prepared arm after GitHub's exact-head read-back."""
        if item.issue is None or item.pr is None:
            return True
        confirmer = getattr(ctx.github, "confirm_drive_green_arm", None)
        if not callable(confirmer):
            logger.error(
                "merge_wait:%d: adapter cannot persist durable arm confirmation",
                item.issue,
            )
            return False
        try:
            confirmer(item.issue, item.pr, head_sha)
        except Exception as exc:
            logger.error(
                "merge_wait: failed to persist arm confirmation for PR #%d: %s",
                item.pr,
                exc,
            )
            return False
        return True

    def _disable_and_fail(
        self,
        item: WorkItem,
        ctx: StageContext,
        note: str,
        *,
        recoverable: bool = False,
    ) -> StepResult:
        """Contain a failed gate condition before routing its outcome.

        Approval-label loss is recoverable only after containment. Failures to
        disable auto-merge or to persist an arm record stay terminal because
        their remote state is ambiguous.
        """
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
        disabled = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(disabled, item.pr)
        if terminal is not None:
            if terminal.disposition is Disposition.FINISH_PASS:
                return self._route_merged(item, ctx)
            return terminal
        if disabled is None or bool(disabled.get("autoMergeRequest")):
            logger.error(
                "merge_wait: could not verify auto-merge disabled for PR #%d",
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        item.armed = False
        if recoverable:
            # A failback starts a new strict-review cycle even when the PR
            # head is unchanged.  In particular, restart recovery arrives
            # here with the prior GO pass's one-shot attempt already spent;
            # retaining it would make REVIEW_WAIT emit an immediate NOGO
            # instead of invoking Athena again.
            for key in (
                "strict_review_attempt",
                "strict_review_head",
                "strict_review_verdict",
                "strict_review_text",
                "strict_review_failed",
                "strict_review_worktree",
                "strict_review_worktree_head",
                "strict_review_worktree_failed",
                "strict_review_worktree_pending",
            ):
                item.payload.pop(key, None)
        disposition = Disposition.FAIL_BACK if recoverable else Disposition.FINISH_FAIL
        return StageOutcome(disposition, note)

    def _poll(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """POLL only the post-merge state needed for the deduped learn path."""
        if item.pr is None:  # guarded by ARM; kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        gh_state = ctx.github.gh_pr_state(item.pr)
        pr_state_str = ((gh_state or {}).get("state") or "").upper()
        if pr_state_str not in {"MERGED", "CLOSED"}:
            has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
            if not has_go:
                return self._disable_and_fail(item, ctx, "not_implementation_go", recoverable=True)
            if item.armed and bool((gh_state or {}).get("autoMergeRequest")) and has_go:
                started = item.payload.get("merge_wait_started_at")
                if started is None:
                    return StageOutcome(Disposition.FINISH_FAIL, "missing_merge_wait_started_at")
                # Auto-merge is owned by GitHub after the confirmed arm.  The
                # coordinator only parks and re-reads; it never sleeps or
                # performs another merge mutation from this state.
                item.payload["retry_delay_s"] = 30
                return StageOutcome(Disposition.RETRY, "merge_pending")
            return self._retry_arm(item, "auto_merge_not_armed")

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
        return StageOutcome(Disposition.FINISH_FAIL, "not_implementation_go")

    def _route_merged(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dedupe the MERGED PR's post-merge /learn via the arming record.

        The /learn leg fires at most once per merged PR (#848): a terminal
        learn record (``ctx.github.drive_green_learn_terminal``) — or learn
        disabled by config — finishes PASS immediately without dispatching
        the session.
        """
        if item.issue is None or not getattr(ctx.config, "enable_learn", True):
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if item.issue is not None and ctx.github.drive_green_learn_terminal(item.issue):
            logger.info(
                "merge_wait:%d: /learn already terminal for PR #%s; deduped",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if item.issue is not None and ctx.github.drive_green_learn_inflight(item.issue):
            logger.error(
                "merge_wait:%d: /learn outcome is unknown after a durable in-flight claim; "
                "refusing to replay it",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "learn_outcome_unknown")
        return Continue(next_state=LEARN_WAIT)

    def _request_learn(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """LEARN_WAIT [W:A]: dispatch the deduped post-merge /learn session.

        Prompt composed in-worker by :func:`build_drive_green_learn_prompt`.
        The dedupe already held at ``_route_merged`` (a terminal record never
        reaches here). A durable claim is written before dispatch; an
        unpersisted outcome is therefore an explicit unknown rather than a
        replayable job after restart.
        """
        if item.issue is None or item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "missing_learn_scope")
        try:
            claimed = ctx.github.claim_drive_green_learn(item.issue, item.pr)
        except Exception as exc:
            logger.error(
                "merge_wait:%d: failed to durably claim /learn dispatch: %s",
                item.issue,
                exc,
            )
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
            prompt_kwargs={
                "issue_number": item.issue,
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
                logger.error(
                    "merge_wait:%d: failed to durably mark /learn result: %s",
                    item.issue,
                    exc,
                )
                item.payload["learn_result_persistence_failed"] = True
