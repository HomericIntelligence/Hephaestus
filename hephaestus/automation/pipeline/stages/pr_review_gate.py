# This mixin consumes the stage thread namespace by design.
# ruff: noqa: F403, F405
from .pr_review_threads import *


class PrReviewGate(_PrReviewHost):
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

        if payload.pop("scope_retraction_failure", False):
            logger.warning(
                "pr_review:%d: refusing to publish incomplete scope retraction",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_incomplete")

        if payload.get(_PENDING_IMPLEMENTATION_REPLY_HANDOFF) and not (
            payload.get(_REPLY_HANDOFF_RECEIPT) or payload.get(_REPLY_HANDOFF_RECEIPT_ERROR)
        ):
            return Continue(next_state=RECOVERY_REPLY_WAIT)
        handoff_status = self._consume_reply_handoff_receipt(item)
        if handoff_status == "visibility_wait":
            return StageOutcome(Disposition.RETRY, "implementation_reply_handoff_visibility_wait")
        if handoff_status == "blocked":
            # The handoff may have crossed the mutation boundary without a
            # receipt. Drop all round evidence and refresh from GitHub; the
            # fresh review must reconcile any already-applied replies rather
            # than replaying the armed intent.
            _clear_round_review_state(item)
            payload["review_refresh_required"] = True
            return Continue(next_state=REVIEW_WAIT)
        if handoff_status == "invalid":
            logger.error(
                "pr_review:%d: refusing to replay malformed implementation reply handoff",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        if handoff_status == "retry":
            retries = payload.get(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, 0)
            if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
                return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
            retries += 1
            payload[_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES] = retries
            if retries <= IMPLEMENTATION_REPLY_HANDOFF_RETRY_CAP:
                logger.warning(
                    "pr_review:%d: retrying exact implementation reply handoff %d/%d",
                    item.issue,
                    retries,
                    IMPLEMENTATION_REPLY_HANDOFF_RETRY_CAP,
                )
                return StageOutcome(Disposition.RETRY, "implementation_reply_handoff_retry")
            logger.error(
                "pr_review:%d: implementation reply handoff retry cap reached",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_failed")

        detached_push_failure = payload.pop("detached_push_failure", None)
        if detached_push_failure == "remote_unchanged":
            source_sha = payload.pop("detached_push_head_sha", None)
            if not is_full_commit_sha(source_sha):
                logger.warning(
                    "pr_review:%d: detached push retry receipt lacks an exact local head",
                    item.issue,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "detached_push_retry_receipt_invalid")
            retries = int(payload.get("direct_push_retries", 0))
            if retries < DIRECT_PUSH_RETRY_CAP:
                payload["direct_push_retries"] = retries + 1
                payload["detached_push_retry_head_sha"] = source_sha
                logger.warning(
                    "pr_review:%d: detached push failed with unchanged remote; "
                    "retrying exact commit",
                    item.issue,
                )
                return Continue(next_state=PUSH_WAIT)
            payload["detached_push_failure"] = detached_push_failure
            logger.warning(
                "pr_review:%d: detached push retry cap reached; preserving checkout",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")
        if detached_push_failure == "remote_changed":
            restarts = payload.get("direct_push_remote_changed_restarts", 0)
            if isinstance(restarts, bool) or not isinstance(restarts, int) or restarts < 0:
                return StageOutcome(
                    Disposition.FINISH_FAIL,
                    "detached_push_recovery_receipt_invalid",
                )
            if restarts >= DIRECT_PUSH_REMOTE_CHANGED_RESTART_CAP:
                payload["detached_push_failure"] = detached_push_failure
                logger.warning(
                    "pr_review:%d: detached push remote-change recovery cap reached; "
                    "preserving checkout",
                    item.issue,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")
            payload["direct_push_remote_changed_restarts"] = restarts + 1
            recovery = self._restart_direct_pr_review(item)
            if recovery is not None:
                return recovery
            logger.warning(
                "pr_review:%d: detached push saw changed remote head; "
                "restarting from a fresh checkout",
                item.issue,
            )
            return Continue(next_state=ENTER)
        if detached_push_failure == "remote_changed_unrecorded":
            payload["detached_push_failure"] = detached_push_failure
            logger.warning(
                "pr_review:%d: detached push remote-change receipt could not be recorded; "
                "preserving checkout",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")
        if detached_push_failure in {
            "remote_unconfirmed",
            "retry_checkout_changed",
            "retry_checkout_unconfirmed",
        }:
            payload["detached_push_failure"] = detached_push_failure
            logger.warning(
                "pr_review:%d: detached push recovery state %s; preserving checkout",
                item.issue,
                detached_push_failure,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")

        address_error = self._handle_address_error(item)
        if address_error is not None:
            return address_error

        if payload.pop("review_refresh_required", False):
            # A successful address push changed the reviewed head. Cross the
            # checkout barrier and obtain a new audit before consulting live
            # threads or writing any implementation-state label.
            return self._compact_before_next_review(item, ctx)

        # Real-commit gate (#1575, M4): a no-commit push retries the address
        # once with the directive; the second no-commit turn falls through
        # and is evaluated as an unaddressed round.
        no_commit_retry = self._gate_no_commit(item)
        if no_commit_retry is not None:
            return no_commit_retry

        audit = payload.get("review_audit")
        if payload.pop("review_audit_failure", False) or not isinstance(audit, ReviewAudit):
            return self._handle_error_verdict(item, ReviewAudit(None, "", (), "", valid=False))
        if not audit.valid:
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

        # A valid structural audit is a real review result. Grade, summary,
        # and supplemental feedback never select the implementation state.
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
        """Compact both persisted sessions before continuing the next review round."""
        if item.worktree:
            return Continue(next_state=COMPACT_REVIEWER_WAIT)
        return Continue(next_state=REVIEW_WAIT)

    def _handle_address_error(self, item: WorkItem) -> StageOutcome | None:
        """Fail back hard address/push errors with explicit retry cleanup."""
        payload = item.payload
        if not payload.pop("address_error", None):
            return None

        if payload.get("no_commit_retry_done") or payload.get("unaddressed_findings"):
            payload.pop("push_no_commit", None)
            payload.pop("no_commit_retry_done", None)
            payload.pop("unaddressed_findings", None)
            logger.warning(
                "pr_review:%d: no-commit retry address/push leg failed; "
                "consuming retry directive and failing back agent_error without "
                "burning a review round",
                item.issue,
            )
            return self._fail_back_agent_error(item)

        # The address/push leg hard-failed: the doc's agent_error route —
        # back to implementation for a fresh implement pass (bounded by
        # the implement budget). No labels, no round burned.
        logger.warning("pr_review:%d: address step failed; failing back", item.issue)
        return self._fail_back_agent_error(item)

    @staticmethod
    def _gate_no_commit(item: WorkItem) -> Continue | None:
        """Apply the real-commit gate (#1575): a no-commit push is never "addressed".

        A push that produced NO commit means the address turn self-reported
        a phantom fix. The FIRST such turn retries the address once, carrying
        the still-open threads as ``unaddressed_findings`` (rendered by
        ``build_unaddressed_directive`` inside ``get_address_review_prompt``)
        to re-ground the resumed session. A SECOND consecutive no-commit turn
        returns None so EVAL treats it as an unaddressed round. A real commit
        spends/clears the retry directive (legacy: "a progress round clears
        the retry directive").

        Args:
            item: The work item under evaluation.

        Returns:
            ``Continue(ADDRESS_WAIT)`` for the one retry, else None.

        """
        payload = item.payload
        no_commit = payload.pop("push_no_commit", None)
        if no_commit:
            if not payload.get("no_commit_retry_done"):
                payload["no_commit_retry_done"] = True
                retry_threads = payload.get("remediation_threads") or []
                payload["unaddressed_findings"] = [dict(t) for t in retry_threads]
                logger.warning(
                    "pr_review:%s: address turn produced NO commit; retrying the "
                    "address once with the unaddressed-findings directive (#1575)",
                    item.issue,
                )
                return Continue(next_state=ADDRESS_WAIT)
            logger.warning(
                "pr_review:%s: address retry still produced no commit; "
                "treating this as an unaddressed round",
                item.issue,
            )
        elif no_commit is False:
            payload.pop("no_commit_retry_done", None)
            payload.pop("unaddressed_findings", None)
        return None

    def _handle_error_verdict(self, item: WorkItem, verdict: Any) -> StepResult:
        """Handle a missing/ERROR verdict: bounded RETRY, then fail back.

        Reviewer-infrastructure failure: labels untouched, no round burned,
        RETRY — bounded by the consecutive-failure cap (plan_review
        pattern), then fail back ``agent_error`` (#911/#1554/#1794).

        Args:
            item: The work item under evaluation.
            verdict: The stored verdict (None or an ERROR verdict).

        Returns:
            RETRY below the cap; the flagged agent_error fail-back at it.

        """
        payload = item.payload
        reason = "no review audit found" if verdict is None else "review audit format failure"
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
        """Verify the live unarmed PR is the exact head reviewed this round.

        No pipeline stage owns auto-merge. A non-null or unreadable request is
        consequently an external or ambiguous state, so this method is a
        strict non-mutation boundary. A missing or changed head invalidates
        the in-memory review proof and sends the item back through REVIEW_WAIT.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        pr_state = ctx.github.gh_pr_state(pr_number)
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if pr_state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(pr_state):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        live_head = str(pr_state.get("headRefOid") or "")
        if not reviewed_head or not live_head or reviewed_head != live_head:
            item.payload.pop("reviewed_pr_head_sha", None)
            return Continue(next_state=REVIEW_WAIT)
        return None

    @staticmethod
    def _revalidate_go_write(item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Check the nonconditional GO write against fresh state and labels.

        GitHub exposes no conditional label mutation. A push or external
        label write can therefore race after the pre-write guard. A read after
        our write cannot prove who owns an exclusive GO label, so a changed or
        missing reviewed head only discards this process's proof and restarts
        review. A complete thread read after the label write detects review
        activity in the remaining admission window. This run cannot establish
        ownership of a label after that race, so it preserves the live
        threads for a fresh automation pass and makes no further label
        mutation.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        try:
            state = ctx.github.gh_pr_state(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to revalidate GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        if isinstance(state, dict) and state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        live_head = str(state.get("headRefOid") or "") if isinstance(state, dict) else ""
        if not reviewed_head or not live_head or reviewed_head != live_head:
            item.payload.pop("reviewed_pr_head_sha", None)
            return Continue(next_state=REVIEW_WAIT)
        try:
            live_threads = ctx.github.list_unresolved_review_threads(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to reread review threads after GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "review_threads_unavailable")
        if live_threads:
            return PrReviewStage._handle_late_threads_after_go_write(
                item,
                len(live_threads),
                ctx,
            )
        try:
            has_go, has_no_go = ctx.github.pr_has_implementation_state_label(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to revalidate GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        if _is_confirmed_open_unarmed(state) and has_go and not has_no_go:
            return None
        return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")

    @staticmethod
    def _handle_late_threads_after_go_write(
        item: WorkItem,
        unresolved_threads: int,
        ctx: StageContext,
    ) -> StageOutcome:
        """Stand down after a post-GO thread race without touching state labels.

        The GO write is non-conditional. A concurrent actor may own the current
        implementation state by the time the late thread is observed, so
        clearing or replacing a label would be an unsafe mutation. The next
        loop invocation must start a new review proof before it can validate
        and reconcile those threads.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        logger.warning(
            "pr_review:%d: %d review thread(s) appeared during GO admission on PR #%d; "
            "standing down without label changes",
            item.issue,
            unresolved_threads,
            item.pr,
        )
        return StageOutcome(Disposition.FINISH_FAIL, "review_activity_changed")

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
        arm_outcome = PrReviewStage._require_reviewed_unarmed(item, ctx)
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
        post_write_guard = PrReviewStage._require_reviewed_unarmed(item, ctx)
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
                return Continue(next_state=REVIEW_WAIT)
            if not item.payload.get("pending_implementation_go_label_confirmed"):
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
