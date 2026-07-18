"""Merge-wait stage: strictly-gated auto-merge arming and post-merge learning.

Re-houses ``ci_driver._arm_and_wait_for_merge`` (:584) /
``_wait_for_pr_terminal`` (:1492) / ``_resolve_dirty_pr`` (:923) /
``_resolve_blocked_pr`` (:986) as a pipeline stage
(docs/AUTOMATION_LOOP_ARCHITECTURE.md section "7. merge_wait" is the
binding contract).

ARM reads the live PR head, requires the loop-owned ``state:implementation-go``
label, requests squash auto-merge, and confirms that GitHub recorded the arm.
The label is the automation loop's sole merge authorization; no CI status,
external review artifact, or lease is consulted here.

- States: ENTER -> ARM -> POLL -> LEARN_WAIT -> MW_FINISH.
- ARM [M]: persist a prepared record, arm, confirm both GitHub and the
  durable record, then poll.  This is the only automatic call site for
  ``arm_auto_merge``.  Confirmed recovery resumes POLL without re-arming.
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
from hephaestus.automation.state_labels import STATE_IMPLEMENTATION_GO
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
    """Stage: arm only a current loop-owned approval label and contain drift."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Initialize the mini-state and restore a confirmed arm to POLL.

        A recovery seed carries only a merge-wait record, not the ephemeral
        current-head handoff from strict_review.  It first disarms any remote
        arm, then starts from ARM.  ARM routes an open PR back to
        strict_review unless that direct current-head handoff is present; a
        merged PR still reaches the
        normal deduplicated learning path.  This prevents an old label from
        authorizing a pushed head after a restart.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
        if item.payload.pop("merge_wait_recovery", False):
            if item.issue is None or item.pr is None:
                return StageOutcome(Disposition.FINISH_FAIL, "invalid_arm_recovery")
            try:
                # This is an ingress boundary, not an authorization check:
                # a persisted old arm can otherwise merge before ARM gets to
                # revalidate the missing in-memory review handoff.
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
        reviewed_head = str(item.payload.get("pr_review_skill_head") or "")
        if not reviewed_head or not head_sha or reviewed_head != head_sha:
            # Only the direct strict_review handoff carries the reviewed
            # current head.  A restart/reseed cannot reconstruct it from the
            # label, so it must repeat strict_review rather than arm a
            # possibly pushed head.  Deferral must precede label removal: a
            # label does not cancel a stale GitHub auto-merge arm.
            return self._disable_and_fail(
                item, ctx, "review_stale", recoverable=True, revoke_go_label=True
            )
        has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
        if not has_go:
            return self._disable_and_fail(item, ctx, "not_implementation_go", recoverable=True)
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
            raced = ctx.github.gh_pr_state(item.pr)
            raced_terminal = _terminal_pr_outcome(raced, item.pr)
            if raced_terminal is not None and raced_terminal.disposition is Disposition.FINISH_PASS:
                logger.info("merge_wait: PR #%d merged while arming: %s", item.pr, exc)
                return self._route_merged(item, ctx)
            logger.warning("merge_wait: failed to arm auto-merge for PR #%d: %s", item.pr, exc)
            # A transport error is ambiguous: GitHub may have accepted the
            # arm before the client observed the failure.  Contain that
            # possible remote arm before terminalizing so a later push cannot
            # merge without its loop-owned approval label.
            return self._disable_and_fail(item, ctx, "arm_failed")
        confirmed = ctx.github.gh_pr_state(item.pr)
        confirmed_terminal = _terminal_pr_outcome(confirmed, item.pr)
        if confirmed_terminal is not None:
            if confirmed_terminal.disposition is Disposition.FINISH_PASS:
                return self._route_merged(item, ctx)
            return confirmed_terminal
        confirmed_head = str((confirmed or {}).get("headRefOid") or "")
        confirmed_has_go, _confirmed_has_no_go = ctx.github.pr_has_implementation_state_label(
            item.pr
        )
        if (
            confirmed_head != head_sha
            or not (confirmed or {}).get("autoMergeRequest")
            or not confirmed_has_go
        ):
            return self._disable_and_fail(item, ctx, "arm_confirm_failed", recoverable=True)
        if not self._confirm_arm(item, ctx, head_sha):
            # GitHub is armed but recovery cannot prove that fact durably.
            # Contain the remote arm before terminalizing rather than letting
            # a future restart re-enable an already-valid request.
            return self._disable_and_fail(item, ctx, "arm_confirmation_record_failed")
        item.armed = True
        item.payload["merge_wait_head"] = head_sha
        return Continue(next_state=POLL)

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

    def _disable_and_fail(  # noqa: C901 - containment preserves each remote failure distinction.
        self,
        item: WorkItem,
        ctx: StageContext,
        note: str,
        *,
        recoverable: bool = False,
        revoke_go_label: bool = False,
    ) -> StepResult:
        """Contain a failed gate condition before routing its outcome.

        Label loss and head drift are recoverable only after containment and a
        fresh `$athena:pr-review` pass. Failures to disable auto-merge or to persist
        an arm record stay terminal because their remote state is ambiguous.
        When a stale approval label must be revoked, the read-back-confirmed
        deferral is deliberately performed first because label removal alone
        does not stop an existing GitHub arm.
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
        if revoke_go_label:
            label_revoked = True
            try:
                ctx.github.remove_labels(item.pr, [STATE_IMPLEMENTATION_GO])
            except Exception as exc:
                logger.error(
                    "merge_wait: failed to revoke stale implementation-go on PR #%d: %s",
                    item.pr,
                    exc,
                )
                # The outcome is ambiguous: GitHub may have applied the
                # label change despite the failed response.  Re-contain the
                # remote arm before reporting the label failure.
                label_revoked = False
            # The label mutation is not a disarm.  Repeat containment after
            # it so a same-head re-arm racing that RPC cannot survive the
            # failback to strict review.
            try:
                ctx.github.defer_auto_merge(item.pr)
            except Exception as exc:
                logger.error(
                    "merge_wait: failed to re-disable auto-merge after label revocation "
                    "for PR #%d: %s",
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
                    "merge_wait: could not verify auto-merge disabled after label revocation "
                    "for PR #%d",
                    item.pr,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
            if not label_revoked:
                return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_revoke_failed")
        item.armed = False
        disposition = Disposition.FAIL_BACK if recoverable else Disposition.FINISH_FAIL
        return StageOutcome(disposition, note)

    def _poll(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """POLL only the post-merge state needed for the deduped learn path."""
        if item.pr is None:  # guarded by ARM; kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        gh_state = ctx.github.gh_pr_state(item.pr)
        pr_state_str = ((gh_state or {}).get("state") or "").upper()
        if pr_state_str not in {"MERGED", "CLOSED"}:
            armed_head = str(item.payload.get("merge_wait_head") or "")
            current_head = str((gh_state or {}).get("headRefOid") or "")
            has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
            if (
                item.armed
                and armed_head
                and current_head == armed_head
                and bool((gh_state or {}).get("autoMergeRequest"))
                and has_go
            ):
                started = item.payload.get("merge_wait_started_at")
                if started is None:
                    return StageOutcome(Disposition.FINISH_FAIL, "missing_merge_wait_started_at")
                # Auto-merge is owned by GitHub after the confirmed arm.  The
                # coordinator only parks and re-reads; it never sleeps or
                # performs another merge mutation from this state.
                item.payload["retry_delay_s"] = 30
                return StageOutcome(Disposition.RETRY, "merge_pending")
            return self._disable_and_fail(item, ctx, "not_implementation_go", recoverable=True)

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
