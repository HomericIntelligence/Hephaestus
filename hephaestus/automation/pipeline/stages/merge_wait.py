"""Merge-wait pipeline stage (issue #1816).

Non-blocking re-housing of ci_driver._arm_and_wait_for_merge /
_wait_for_pr_terminal / _resolve_dirty_pr / _resolve_blocked_pr. The single
PR-state read per POLL replaces the _wait_for_pr_terminal sleep loop; pending
-> RETRY(delay). NO time.sleep / import time in this module (AC1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hephaestus.automation.ci_run_coordinator import PrMergeState
from hephaestus.automation.pr_manager import pr_is_genuinely_stuck
from hephaestus.constants import read_timeout_env

from .base import (
    Continue,
    Disposition,
    JobRequest,
    Stage,
    StageContext,
    StageOutcome,
    write_skip_label,
)

if TYPE_CHECKING:
    from hephaestus.automation.pipeline.work_item import WorkItem

_BACKOFF_CAP = 60


class MergeWaitStage(Stage):
    """Pipeline stage for merge-wait after CI green."""

    name = "merge_wait"

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Idempotent entry: initialize state if needed."""
        if not item.state:
            item.state = "ARM"
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest | StageOutcome:
        """Execute the next action for the item's current state."""
        if item.state == "ARM":
            return self._arm(item, ctx)
        if item.state == "POLL":
            return self._poll(item, ctx)
        if item.state == "DIRTY_REBASE_READY":
            return self._request_dirty_resolve(item, ctx)
        if item.state == "BLOCKED_ADDRESS_READY":
            return self._request_blocked_address(item, ctx)
        if item.state == "LEARN_READY":
            return self._request_learn(item, ctx)
        raise AssertionError(f"unreachable merge_wait state {item.state!r}")

    def _arm(self, item: WorkItem, ctx: StageContext) -> Continue | StageOutcome:
        """Step 1 [M]: arm auto-merge if unarmed + write arming record [durable]."""
        if ctx.dry_run:
            item.state = "POLL"
            return Continue("POLL")
        # Placeholder: actual implementation calls
        # ctx.pr_manager.enable_auto_merge_after_implementation_go and
        # writes arming state via ctx.arm_drive_green
        item.state = "POLL"
        return Continue("POLL")

    def _poll(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest | StageOutcome:
        """Step 2 [M]: single PR-state read → classify (non-blocking)."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        # Placeholder: actual implementation calls ctx.gh_pr_state,
        # ctx.failing_required_check_names, etc. For now, assume default
        state = PrMergeState.PENDING

        if state is PrMergeState.MERGED:
            item.state = "LEARN_READY"
            return Continue("LEARN_READY")
        if state is PrMergeState.CLOSED:
            return StageOutcome(Disposition.FINISH_FAIL, "closed")
        if state is PrMergeState.FAILING:
            return StageOutcome(Disposition.FAIL_BACK, "ci_red")
        if state is PrMergeState.DIRTY:
            rebase_budget = ctx.budget("rebase")
            rebase_attempts = item.attempts.get("rebase", 0)
            if rebase_attempts >= rebase_budget:
                return StageOutcome(Disposition.FINISH_FAIL, "timeout")
            item.state = "DIRTY_REBASE_READY"
            return Continue("DIRTY_REBASE_READY")
        if state is PrMergeState.BLOCKED:
            blocked_budget = ctx.budget("blocked_address")
            blocked_attempts = item.attempts.get("blocked_address", 0)
            if blocked_attempts >= blocked_budget:
                if item.issue and pr_is_genuinely_stuck(item.pr):
                    write_skip_label(item.issue, ctx)
                    return StageOutcome(Disposition.SKIP, "blocked_stuck")
                return StageOutcome(Disposition.FAIL_BACK, "blocked_exhausted")
            item.state = "BLOCKED_ADDRESS_READY"
            return Continue("BLOCKED_ADDRESS_READY")
        # PENDING: park with exponential backoff, honor wall-clock deadline.
        max_wait = read_timeout_env("HEPH_PR_MERGE_MAX_WAIT", 1800)
        delay = min(2 ** item.attempts.get("merge_poll", 0), _BACKOFF_CAP)
        elapsed = item.attempts.get("merge_elapsed", 0)
        if elapsed + delay > max_wait:
            return StageOutcome(Disposition.FINISH_FAIL, "timeout")
        item.attempts["merge_poll"] = item.attempts.get("merge_poll", 0) + 1
        item.attempts["merge_elapsed"] = elapsed + delay
        return JobRequest(None, "POLL")  # type: ignore

    def _request_dirty_resolve(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 3 DIRTY [W:G]+[W:A]: re-housed _resolve_dirty_pr."""
        item.attempts["rebase"] = item.attempts.get("rebase", 0) + 1
        return JobRequest(None, "POLL")  # type: ignore

    def _request_blocked_address(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 3 BLOCKED [W:A]: address unresolved threads."""
        item.attempts["blocked_address"] = item.attempts.get("blocked_address", 0) + 1
        return JobRequest(None, "PUSH_BLOCKED_READY")  # type: ignore

    def _request_learn(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 3 MERGED [W:A]: post-merge /learn, deduped."""
        return JobRequest(None, "_FINISH_PASS")  # type: ignore

    def on_job_done(self, item: WorkItem, result: Any, ctx: StageContext) -> None:
        """Route a completed worker job back into the merge-wait state machine."""
        # Placeholder: actual implementation routes based on result.job_kind
        pass
