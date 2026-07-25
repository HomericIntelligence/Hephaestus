"""Merge-wait: final head-bound admission and one conditional normal merge.

``pr_review`` owns the loop's source-review decision. This stage may perform
one ordinary REST squash merge only after it observes the exact active-run
reviewed head, an exclusive implementation-GO label, an open ``main`` PR, and
an explicitly absent auto-merge request. It never enables, disables, adopts,
or polls native auto-merge.
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

_RETRYABLE_READINESS = frozenset({"BEHIND", "BLOCKED", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"})
_CONFLICTING_READINESS = frozenset({"CONFLICTING", "DIRTY"})


def build_drive_green_learn_prompt(issue_number: int, pr_number: int) -> str:
    """Compose the post-merge learning prompt in the worker."""
    return build_learn_prompt(
        PromptCatalog.current().render(
            "learn/drive_green_context.j2", issue_number=issue_number, pr_number=pr_number
        )
    )


class MergeWaitStage(Stage):
    """Attempt a bounded SHA-conditional merge after final live admission."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Reject an unscoped PR without touching any GitHub state."""
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
        """Execute the merge-wait state machine."""
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
        admitted = self._admit(item, ctx)
        if not isinstance(admitted, tuple):
            return admitted
        pr_state, reviewed_head = admitted
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        base_branch = pr_state.get("baseRefName")
        if not isinstance(base_branch, str) or not base_branch:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        thread_admission = self._admit_no_unresolved_threads(item.pr, ctx)
        if thread_admission is not None:
            return thread_admission
        if item.attempts["merge"] >= ctx.budget("merge"):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
        protection_admission = self._admit_conversation_resolution(
            item.pr,
            base_branch,
            ctx,
        )
        if protection_admission is not None:
            return protection_admission
        item.attempts["merge"] += 1
        result = ctx.github.merge_pr_if_head(item.pr, reviewed_head)
        return self._reconcile_merge_request(item, ctx, result)

    def _reconcile_merge_request(
        self, item: WorkItem, ctx: StageContext, result: Any
    ) -> StepResult:
        """Interpret the one permitted conditional merge response."""
        if result.dry_run:
            return StageOutcome(Disposition.FINISH_FAIL, "conditional_merge_dry_run")
        if result.malformed:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_result_malformed")
        if result.transport_error or result.status is None:
            return self._reconcile_transport_ambiguity(item, ctx)
        if result.status == 200:
            if result.body is not None and result.body.get("merged") is True:
                return self._reconcile_successful_merge(item, ctx)
            return StageOutcome(Disposition.FINISH_FAIL, "merge_not_merged")
        if result.status == 409:
            return self._reconcile_head_conflict(item, ctx)
        if result.status == 405:
            return self._reconcile_not_ready(item, ctx)
        if result.status in {403, 404, 422}:
            return StageOutcome(Disposition.FINISH_FAIL, f"merge_http_{result.status}")
        return StageOutcome(Disposition.FINISH_FAIL, f"merge_http_{result.status}")

    @staticmethod
    def _admit_no_unresolved_threads(pr_number: int, ctx: StageContext) -> StageOutcome | None:
        """Use a final empty local thread read as a non-atomic defense in depth.

        The server-enforced branch-protection admission immediately following
        this helper is the merge safety gate: a client-side thread list can
        change before the SHA-conditional PUT reaches GitHub.
        """
        try:
            live_threads = ctx.github.list_unresolved_review_threads(pr_number)
        except Exception as error:
            logger.warning(
                "merge_wait:%d: final review-thread admission read failed (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "review_threads_unavailable")
        if live_threads:
            logger.info(
                "merge_wait:%d: refusing conditional merge with %d unresolved review thread(s)",
                pr_number,
                len(live_threads),
            )
            return StageOutcome(Disposition.FINISH_FAIL, "unresolved_review_threads")
        return None

    @staticmethod
    def _admit_conversation_resolution(
        pr_number: int, base_branch: str, ctx: StageContext
    ) -> StageOutcome | None:
        """Require the base branch's server-enforced conversation-resolution gate."""
        try:
            enabled = ctx.github.base_branch_requires_conversation_resolution(
                pr_number,
                base_branch,
            )
        except Exception as error:
            logger.warning(
                "merge_wait:%d: conversation-resolution protection read failed (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "conversation_resolution_unavailable")
        if not enabled:
            logger.warning(
                "merge_wait:%d: base branch %r lacks required conversation resolution",
                pr_number,
                base_branch,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "conversation_resolution_required")
        return None

    def _admit(self, item: WorkItem, ctx: StageContext) -> tuple[dict[str, Any], str] | StepResult:
        """Return the complete final-admission facts or a safe terminal route."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(pr_state, item.pr)
        if terminal is not None:
            if terminal.disposition is Disposition.FINISH_PASS:
                return self._route_merged(item, ctx)
            return terminal
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if pr_state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(pr_state):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        if pr_state.get("baseRefName") != "main":
            return StageOutcome(Disposition.FINISH_FAIL, "non_main_base")
        has_go, has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
        if not has_go or has_no_go:
            return StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
        head = str(pr_state.get("headRefOid") or "")
        if not head:
            return StageOutcome(Disposition.FINISH_FAIL, "missing_pr_head")
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        if reviewed_head != head:
            return self._revoke_stale_reviewed_head(item, ctx, reviewed_head)
        return pr_state, reviewed_head

    def _revoke_stale_reviewed_head(
        self, item: WorkItem, ctx: StageContext, reviewed_head: str
    ) -> StageOutcome:
        """Discard stale process-local proof without relabelling a live PR.

        A final read cannot prove this process owns a future label: an external
        arm or newer GO can arrive immediately afterward. Fresh review owns any
        subsequent state decision, so this method performs zero mutations.
        """
        del item, ctx
        if not reviewed_head:
            return StageOutcome(Disposition.FAIL_BACK, "reviewed_head_missing")
        return StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")

    def _reconcile_successful_merge(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Require a fresh terminal lifecycle observation after HTTP 200."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(state, item.pr)
        if terminal is None:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_not_merged")
        if terminal.disposition is Disposition.FINISH_PASS:
            return self._route_merged(item, ctx)
        return terminal

    def _reconcile_head_conflict(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Classify a conditional-SHA conflict using fresh live lifecycle state."""
        admitted = self._admit(item, ctx)
        if not isinstance(admitted, tuple):
            return admitted
        return StageOutcome(Disposition.FINISH_FAIL, "merge_409_without_head_drift")

    def _reconcile_not_ready(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Use operational readiness to distinguish retryable 405 from conflict."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        readiness = ctx.github.gh_pr_merge_readiness(item.pr)
        terminal = _terminal_pr_outcome(readiness, item.pr)
        if terminal is not None:
            if terminal.disposition is Disposition.FINISH_PASS:
                return self._route_merged(item, ctx)
            return terminal
        if readiness is None:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_unavailable")
        if readiness.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        admitted = self._admit(item, ctx)
        if not isinstance(admitted, tuple):
            return admitted
        status = str(readiness.get("mergeStateStatus") or "").upper()
        if status in _CONFLICTING_READINESS:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_conflicting")
        if status not in _RETRYABLE_READINESS:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_unknown")
        return self._retry(item, ctx)

    def _reconcile_transport_ambiguity(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Re-read lifecycle before deciding whether an unknown request may retry."""
        admitted = self._admit(item, ctx)
        if not isinstance(admitted, tuple):
            return admitted
        return self._retry(item, ctx)

    @staticmethod
    def _retry(item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Timer-park a bounded retry without issuing another merge in this step."""
        if item.attempts["merge"] >= ctx.budget("merge"):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
        item.payload["retry_delay_s"] = float(min(2 ** max(0, item.attempts["merge"] - 1), 60))
        return StageOutcome(Disposition.RETRY, "merge_not_ready")

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
