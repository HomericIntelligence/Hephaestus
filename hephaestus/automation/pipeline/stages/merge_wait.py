"""Merge reviewed PRs with a SHA-conditional ordinary GitHub merge (#2419).

``pr_review`` owns the approval label and records an in-process reviewed-head
proof.  This stage makes no native auto-merge, merge-queue, administrative, or
protection-bypass mutation.  Instead, immediately after re-reading a complete
open, ``main``-targeted, unarmed PR with exclusive implementation-go and the
same reviewed SHA, it makes one ordinary REST squash-merge request conditional
on that SHA.  GitHub's conditional SHA check is the merge linearization point.

Every uncertain outcome is reconciled from fresh lifecycle state before a
bounded timer retry.  An observed auto-merge request is external: it blocks
with no label or auto-merge mutation.
"""

from __future__ import annotations

import logging
from typing import Any

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
MERGE = "MERGE"
LEARN_WAIT = "LEARN_WAIT"
MW_FINISH = "MW_FINISH"
FINISH = MW_FINISH

_RETRYABLE_405_STATES = frozenset({"BEHIND", "BLOCKED", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"})


def build_drive_green_learn_prompt(issue_number: int, pr_number: int) -> str:
    """Compose the post-merge learning prompt in the worker."""
    return build_learn_prompt(
        PromptCatalog.current().render(
            "learn/drive_green_context.j2", issue_number=issue_number, pr_number=pr_number
        )
    )


class MergeWaitStage(Stage):
    """Consume a reviewed-head proof through a conditional normal merge."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Reject an unscoped PR without altering an operator-owned request."""
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
        """Execute the merge-wait mini-state."""
        if item.state == ENTER:
            return Continue(next_state=MERGE)
        if item.state == MERGE:
            return self._merge(item, ctx)
        if item.state == LEARN_WAIT:
            return self._request_learn(item, ctx)
        if item.state == MW_FINISH:
            if item.payload.pop("learn_result_persistence_failed", None):
                return StageOutcome(Disposition.FINISH_FAIL, "learn_result_persistence_failed")
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        logger.warning("merge_wait:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _merge(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Read final admission facts, then make at most one conditional request."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        admission, terminal = self._admit(item, ctx)
        if terminal is not None:
            return terminal
        if admission is None:  # pragma: no cover - _admit returns a terminal instead
            return StageOutcome(Disposition.FINISH_FAIL, "merge_admission_unavailable")
        reviewed_sha = str(item.payload.get("reviewed_pr_head_sha") or "")
        if item.attempts.get("merge", 0) >= ctx.budget("merge"):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
        item.attempts["merge"] = item.attempts.get("merge", 0) + 1
        outcome = ctx.github.merge_pr_if_head(item.pr, reviewed_sha)
        return self._classify_attempt_outcome(item, ctx, outcome)

    def _classify_attempt_outcome(
        self, item: WorkItem, ctx: StageContext, outcome: Any
    ) -> StepResult:
        """Route one adapter result without issuing a duplicate request."""
        if outcome.dry_run:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_dry_run")
        if outcome.malformed:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_response_malformed")
        if outcome.status == 200:
            if outcome.body is not None and outcome.body.get("merged") is True:
                return self._reconcile_merged(item, ctx)
            return StageOutcome(Disposition.FINISH_FAIL, "merge_not_merged")
        if outcome.status == 409:
            return self._reconcile_409(item, ctx)
        if outcome.status == 405:
            return self._reconcile_405(item, ctx)
        if outcome.status in {403, 404, 422}:
            return StageOutcome(Disposition.FINISH_FAIL, f"merge_http_{outcome.status}")
        if outcome.status is None and outcome.transport_error:
            return self._reconcile_transport_ambiguity(item, ctx)
        return StageOutcome(Disposition.FINISH_FAIL, "merge_unknown_status")

    def _admit(
        self,
        item: WorkItem,
        ctx: StageContext,
        pr_state: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, StepResult | None]:
        """Return only final, fully verified normal-merge admission facts."""
        if item.pr is None:
            return None, StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        state = ctx.github.gh_pr_state(item.pr) if pr_state is None else pr_state
        terminal = _terminal_pr_outcome(state, item.pr)
        if terminal is not None:
            result = (
                self._route_merged(item, ctx)
                if terminal.disposition is Disposition.FINISH_PASS
                else terminal
            )
            return None, result
        if state is None:
            return None, StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if state.get("autoMergeRequest") is not None:
            logger.warning("merge_wait: PR #%d already has an external auto-merge request", item.pr)
            return None, StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(state):
            return None, StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        if state.get("baseRefName") != "main":
            return None, StageOutcome(Disposition.FINISH_FAIL, "non_main_base")
        has_go, has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
        if not has_go or has_no_go:
            return None, StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
        head_sha = str(state.get("headRefOid") or "")
        if not head_sha:
            return None, StageOutcome(Disposition.FINISH_FAIL, "missing_pr_head")
        reviewed_sha = str(item.payload.get("reviewed_pr_head_sha") or "")
        if reviewed_sha != head_sha:
            return None, self._revoke_stale_reviewed_head(item, ctx, reviewed_sha)
        return state, None

    def _revoke_stale_reviewed_head(
        self, item: WorkItem, ctx: StageContext, reviewed_sha: str
    ) -> StepResult:
        """Re-read unarmed state before revoking an invalid reviewed-head label."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        current = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(current, item.pr)
        if terminal is not None:
            return (
                self._route_merged(item, ctx)
                if terminal.disposition is Disposition.FINISH_PASS
                else terminal
            )
        if current is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if current.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(current):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        if current.get("baseRefName") != "main":
            return StageOutcome(Disposition.FINISH_FAIL, "non_main_base")
        current_head = str(current.get("headRefOid") or "")
        if not current_head:
            return StageOutcome(Disposition.FINISH_FAIL, "missing_pr_head")
        if reviewed_sha == current_head:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_admission_changed")
        try:
            ctx.github.mark_pr_implementation_no_go(item.pr)
        except Exception as exc:
            logger.warning(
                "merge_wait: failed to revoke stale approval on PR #%d: %s", item.pr, exc
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_label_failed")
        if not reviewed_sha:
            return StageOutcome(Disposition.FAIL_BACK, "reviewed_head_missing")
        return StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")

    def _reconcile_merged(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Confirm a claimed success before post-merge learning, never retry it."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(state, item.pr)
        if terminal is None:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_success_not_observed")
        return (
            self._route_merged(item, ctx)
            if terminal.disposition is Disposition.FINISH_PASS
            else terminal
        )

    def _reconcile_409(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Treat a conditional conflict as drift only after a fresh lifecycle read."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(state, item.pr)
        if terminal is not None:
            return (
                self._route_merged(item, ctx)
                if terminal.disposition is Disposition.FINISH_PASS
                else terminal
            )
        if state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        _admission, admission_terminal = self._admit(item, ctx, state)
        if admission_terminal is not None:
            return admission_terminal
        return StageOutcome(Disposition.FINISH_FAIL, "merge_409_without_head_drift")

    def _reconcile_405(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Classify immediate normal-merge readiness from a fresh operational read."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        readiness = ctx.github.gh_pr_merge_readiness(item.pr)
        if readiness is None:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_unavailable")
        _admission, admission_terminal = self._admit(item, ctx, readiness)
        if admission_terminal is not None:
            return admission_terminal
        state = str(readiness.get("mergeStateStatus") or "").upper()
        mergeable = str(readiness.get("mergeable") or "").upper()
        if state == "DIRTY" or mergeable == "CONFLICTING":
            return StageOutcome(Disposition.FINISH_FAIL, "merge_conflict")
        if state in _RETRYABLE_405_STATES:
            return self._schedule_retry(item, ctx, "merge_not_ready")
        return StageOutcome(Disposition.FINISH_FAIL, "merge_not_ready_unclassified")

    def _reconcile_transport_ambiguity(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Read lifecycle after an uncertain write before any bounded retry."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(state, item.pr)
        if terminal is not None:
            return (
                self._route_merged(item, ctx)
                if terminal.disposition is Disposition.FINISH_PASS
                else terminal
            )
        if state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        _admission, admission_terminal = self._admit(item, ctx, state)
        if admission_terminal is not None:
            return admission_terminal
        return self._schedule_retry(item, ctx, "merge_transport_retry")

    @staticmethod
    def _schedule_retry(item: WorkItem, ctx: StageContext, note: str) -> StageOutcome:
        """Timer-park only a known same-head/unarmed admission within its budget."""
        attempts = item.attempts.get("merge", 0)
        if attempts >= ctx.budget("merge"):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
        item.payload["retry_delay_s"] = min(2 ** max(0, attempts - 1), 60)
        return StageOutcome(Disposition.RETRY, note)

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
