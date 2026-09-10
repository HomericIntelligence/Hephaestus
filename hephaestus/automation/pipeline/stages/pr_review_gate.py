# This mixin consumes the stage thread namespace by design.
# ruff: noqa: F403, F405
from hephaestus.automation.review_audit import is_clean_go_review

from .pr_review_repository import _require_reviewed_unarmed_state
from .pr_review_scope_expansion import PrReviewScopeExpansionMixin
from .pr_review_threads import *


class PrReviewGate(PrReviewScopeExpansionMixin, _PrReviewHost):
    """Own bounded review iteration, label proofs, and GO/NO-GO routing."""

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901 - state-machine gate
        """EVAL [M]: apply the structural-audit gate and review budget.

        Every durable write below happens BEFORE the outcome that causes a
        queue push. The round counters (lifetime ``attempts`` audit trail
        and cycle-relative ``payload`` gate) advance here, and only for real
        audits — never for malformed or missing audits (#911/#1554/#1794).
        """
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        payload = item.payload

        if scope_failure := self._scope_retraction_failure(item):
            return scope_failure

        audit = payload.get("review_audit")
        if payload.pop("review_audit_failure", False) or not isinstance(audit, ReviewAudit):
            return self._handle_error_verdict(item, ReviewAudit(None, "", (), "", valid=False))
        if not audit.valid:
            return self._handle_error_verdict(item, audit)
        if audit.verdict not in {"GO", "NOGO", "BLOCKED"}:
            # The parser rejects this state, but the gate also validates an
            # in-memory audit. A malformed payload is an agent failure, not
            # a NOGO result that can burn the review budget or write a label.
            return self._handle_error_verdict(item, audit)
        if not item.payload.get("reviewed_pr_head_sha"):
            # Addressing a finding or pushing a new commit clears the prior
            # head proof. A fresh negative transition may still be based on
            # durable blocking-thread facts, but a clean result must return
            # through REVIEW_WAIT so its positive label is bound to a fresh
            # checkout/review.
            try:
                live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            except Exception:
                return self._compact_before_next_review(item, ctx)
            unresolved_count = len(live_threads)
            if not unresolved_count:
                return self._compact_before_next_review(item, ctx)
            bind_outcome = self._bind_current_head_for_negative(item, ctx)
            if bind_outcome is not None:
                return bind_outcome
            payload["review_error_retries"] = 0
            round_done = payload.get("pr_review_round", 0) + 1
            payload["pr_review_round"] = round_done
            item.attempts["pr_review_iter"] = item.attempts.get("pr_review_iter", 0) + 1
            return self._handle_non_go(
                item,
                ctx,
                audit,
                unresolved_count,
                unresolved_count,
                round_done,
                ctx.budget("pr_review_iter"),
                ctx.budget("pr_review_hard"),
            )

        if scope_outcome := self._handle_scope_expansions(item, ctx, audit):
            return scope_outcome

        # A fresh total open-thread count after the address/push leg is the
        # only thread fact that can downgrade a GO decision.
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: fresh review-thread read failed (%s)",
                item.issue,
                type(error).__name__,
            )
            return self._handle_error_verdict(item, None)
        item.payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        open_thread_count = len(live_threads)

        if (
            audit.verdict == "BLOCKED"
            and not audit.findings
            and not audit.scope_expansions
            and not open_thread_count
        ):
            guard_outcome = self._require_reviewed_unarmed(item, ctx)
            if guard_outcome is not None:
                return guard_outcome
            prefix = f"review_evidence_blocked {item.payload['reviewed_pr_head_sha']}"
            summary = audit.summary
            available = 320 - len(prefix) - 1
            if len(summary) > available:
                summary = f"{summary[: available - 3].rstrip()}..."
            note = f"{prefix} {summary}" if summary else prefix
            return StageOutcome(Disposition.BLOCKED, note)

        # A clean implementation-state transition requires the reviewer's
        # explicit GO verdict. The grade is audit metadata only.
        payload["review_error_retries"] = 0
        round_done = payload.get("pr_review_round", 0) + 1
        payload["pr_review_round"] = round_done
        item.attempts["pr_review_iter"] = item.attempts.get("pr_review_iter", 0) + 1
        soft_cap = ctx.budget("pr_review_iter")
        hard_cap = ctx.budget("pr_review_hard")
        if round_done > soft_cap:
            # Audit trail of progress-earned extension rounds (4..hard_cap).
            item.attempts["pr_review_hard"] = item.attempts.get("pr_review_hard", 0) + 1

        if not open_thread_count:
            # A clean thread read cannot infer GO; the shared contract requires GO and no findings.
            if not is_clean_go_review(audit):
                return self._handle_non_go(
                    item,
                    ctx,
                    audit,
                    open_thread_count,
                    open_thread_count,
                    round_done,
                    soft_cap,
                    hard_cap,
                )
            return self._handle_clean_go(item, ctx)  # type: ignore[attr-defined,no-any-return]

        return self._handle_non_go(
            item,
            ctx,
            audit,
            open_thread_count,
            open_thread_count,
            round_done,
            soft_cap,
            hard_cap,
        )

    def _handle_non_go(
        self,
        item: WorkItem,
        ctx: StageContext,
        verdict: Any,
        open_thread_count: int,
        unresolved_count: int,
        round_done: int,
        soft_cap: int,
        hard_cap: int,
    ) -> StepResult:
        """Persist a non-GO round and choose its bounded retry or terminal route."""
        if item.pr is None or item.issue is None:  # guarded by _eval; type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        guard_outcome = self._write_no_go(item, ctx)
        if guard_outcome is not None:
            return guard_outcome
        # #1863: prev_unresolved is THIS round's pre-address snapshot
        # (POST's unresolved_threads_before_address) so the extension gate compares
        # pre-address vs post-address WITHIN the round being evaluated —
        # progress landing on the soft-cap round is no longer invisible
        # to a stale cross-round comparison.
        prev_unresolved = item.payload.get("unresolved_threads_before_address")
        if round_done < soft_cap:
            logger.info(
                "pr_review:%d: %s (round %d/%d, %d unresolved); re-reviewing",
                item.issue,
                "structured audit",
                round_done,
                soft_cap,
                unresolved_count,
            )
            return self._compact_before_next_review(item, ctx)
        made_progress = prev_unresolved is not None and open_thread_count < prev_unresolved
        if round_done < hard_cap and made_progress:
            # #1554 progress-aware extension: rounds soft_cap+1..hard_cap are
            # admitted only while the total open-thread count strictly decreases.
            logger.info(
                "pr_review:%d: extension round %d/%d earned (%s -> %d open threads)",
                item.issue,
                round_done + 1,
                hard_cap,
                prev_unresolved,
                open_thread_count,
            )
            return self._compact_before_next_review(item, ctx)

        logger.warning(
            "pr_review:%d: exhausted at round %d (open threads %s -> %d); applying %s",
            item.issue,
            round_done,
            prev_unresolved,
            open_thread_count,
            STATE_SKIP,
        )
        # Reuse the exact-head guard after the durable NO-GO write: a push in
        # that window invalidates this exhaustion decision and must re-review
        # the newer head instead of applying state:skip to it.
        arm_outcome = self._require_reviewed_unarmed(item, ctx)
        if arm_outcome is not None:
            return arm_outcome
        write_skip_label(
            item.issue,
            ctx,
            f"PR review rounds exhausted at round {round_done} with the "
            f"open-thread count stuck "
            f"({prev_unresolved} -> {open_thread_count}); further re-review "
            f"cannot make progress. Push new commits addressing the review "
            f"feedback, then remove this label to re-enter the loop.",
        )
        return StageOutcome(Disposition.SKIP, "exhaustion")

    @staticmethod
    def _compact_before_next_review(item: WorkItem, ctx: StageContext) -> Continue:
        """Compact the reviewer before the next review round."""
        if item.worktree:
            return Continue(next_state=COMPACT_REVIEWER_WAIT)
        return Continue(next_state=REVIEW_WAIT)

    def _handle_error_verdict(
        self, item: WorkItem, verdict: Any, *, reason: str | None = None
    ) -> StepResult:
        """Retry a missing or invalid audit within the consecutive-failure cap.

        Do not change labels or consume a review round. At the cap, return
        the item to implementation with the agent_error failure class.
        """
        payload = item.payload
        reason = reason or (
            "no review audit found" if verdict is None else "review audit format failure"
        )
        retries = payload.get("review_error_retries", 0) + 1
        payload["review_error_retries"] = retries
        if retries > REVIEW_ERROR_RETRY_CAP:
            logger.error(
                "pr_review:%s: %s; %d consecutive reviewer failures (cap %d)"
                " — failing back to implementation",
                item.issue,
                reason,
                retries,
                REVIEW_ERROR_RETRY_CAP,
            )
            return self._fail_back_agent_error(item)
        logger.warning(
            "pr_review:%s: %s; retry %d/%d (no round burned)",
            item.issue,
            reason,
            retries,
            REVIEW_ERROR_RETRY_CAP,
        )
        return self._cleanup_review_worktree_then(
            item,
            StageOutcome(Disposition.RETRY, reason),
        )

    @staticmethod
    def _fail_back_agent_error(item: WorkItem) -> StageOutcome:
        """FAIL_BACK ``agent_error``, flagging the re-entry for the M1 bound.

        Every agent_error fail-back marks
        ``payload["agent_error_failback"]`` so the implementation GATE's
        existing-PR adoption consumes the ``implement`` budget — without a
        moving counter the fail-back -> adopt -> ADVANCE cycle would
        ping-pong forever.

        Args:
            item: The work item failing back.

        Returns:
            The FAIL_BACK(``agent_error``) outcome.

        """
        item.payload["agent_error_failback"] = True
        return StageOutcome(Disposition.FAIL_BACK, "agent_error")

    @staticmethod
    def _require_reviewed_unarmed(item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Verify that the reviewed PR is open, unarmed, and at the reviewed head."""
        return _require_reviewed_unarmed_state(item, ctx, review_wait=REVIEW_WAIT)

    @staticmethod
    def _bind_current_head_for_negative(item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Bind the current open head for a negative-only transition."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        state = ctx.github.gh_pr_state(item.pr)
        if state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(state):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        head = str(state.get("headRefOid") or "")
        if not head:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_head_unavailable")
        item.payload["reviewed_pr_head_sha"] = head
        return None

    @staticmethod
    def _require_confirmed_unarmed(pr_number: int, ctx: StageContext) -> StageOutcome | None:
        """Verify a live PR is open and unarmed before an unrelated mutation."""
        pr_state = ctx.github.gh_pr_state(pr_number)
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if pr_state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(pr_state):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        return None

    @staticmethod
    def _write_no_go(item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Durably mark NO-GO after fresh exact-head and label checks.

        Label writes have no compare-and-set operation.  Re-read the live PR
        state and exclusive implementation labels after the write, and never
        attempt a compensating mutation if that proof is lost: a concurrent
        actor may own the current state by then.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        arm_outcome = PrReviewGate._require_reviewed_unarmed(item, ctx)
        if arm_outcome is not None:
            return arm_outcome
        try:
            ctx.github.mark_pr_implementation_no_go(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to mark PR #%d implementation-no-go: %s",
                pr_number,
                error,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_label_failed")
        post_write_guard = PrReviewGate._require_reviewed_unarmed(item, ctx)
        if post_write_guard is not None:
            return post_write_guard
        try:
            has_go, has_no_go = ctx.github.pr_has_implementation_state_label(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to verify PR #%d implementation-no-go: %s",
                pr_number,
                error,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_readback_failed")
        if has_go or not has_no_go:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_readback_failed")
        return None

    def _write_go(self, item: WorkItem, ctx: StageContext) -> StepResult:
        return self.write_go(item, ctx.github)

    def write_go(self, item: WorkItem, github: Any) -> StepResult:  # noqa: C901 - proof gate
        """Atomically perform the reviewed-head GO proof and readback.

        This is the sole pipeline call site for the mechanical GO mutation.
        The reads intentionally surround the write so a concurrent head,
        thread, or label change fails closed without compensating mutations.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        try:
            if github.list_unresolved_review_threads(pr_number):
                return Continue(next_state=REVIEW_WAIT)
            state = github.gh_pr_state(pr_number)
            if state is None:
                return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
            if state.get("autoMergeRequest") is not None:
                return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
            if not _is_confirmed_open_unarmed(state):
                return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
            reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
            live_head = str(state.get("headRefOid") or "")
            if not reviewed_head or reviewed_head != live_head:
                item.payload.pop("reviewed_pr_head_sha", None)
                item.payload.pop("reviewed_pr_node_id", None)
                return Continue(next_state=REVIEW_WAIT)
            github.mark_pr_implementation_go(pr_number)
            state = github.gh_pr_state(pr_number)
            if state is None:
                return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
            if state.get("autoMergeRequest") is not None:
                return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
            if (
                not _is_confirmed_open_unarmed(state)
                or str(state.get("headRefOid") or "") != reviewed_head
            ):
                item.payload.pop("reviewed_pr_head_sha", None)
                item.payload.pop("reviewed_pr_node_id", None)
                return Continue(next_state=REVIEW_WAIT)
            if github.list_unresolved_review_threads(pr_number):
                return StageOutcome(Disposition.FINISH_FAIL, "review_activity_changed")
            has_go, has_no_go = github.pr_has_implementation_state_label(pr_number)
            if not has_go or has_no_go:
                return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        except Exception as error:
            logger.warning(
                "pr_review: GO admission failed on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        return StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
