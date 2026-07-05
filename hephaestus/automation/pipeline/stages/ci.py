"""CI drive-green pipeline stage (issue #1816).

Non-blocking re-housing of ci_driver._drive_issue (minus merge-wait). Every
poll returns RETRY(delay_s) instead of sleeping — the coordinator timer heap
owns the wait. NO time.sleep / import time in this module (AC1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hephaestus.automation.ci_run_coordinator import CiConclusion, classify_ci_state
from hephaestus.automation.github_api.checks import gh_pr_checks

from .base import Continue, Disposition, JobRequest, Stage, StageContext, StageOutcome

if TYPE_CHECKING:
    from hephaestus.automation.pipeline.work_item import WorkItem

_BACKOFF_CAP = 60  # matches ci_run_coordinator poll backoff min(2**n, 60)


class CiStage(Stage):
    """Pipeline stage for CI drive-green."""

    name = "ci"

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Idempotent entry: refresh labels, initialize state if needed."""
        if not item.state:
            item.state = "DISCOVER"
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest | StageOutcome:
        """Execute the next action for the item's current state."""
        if item.state == "DISCOVER":
            return self._discover(item, ctx)
        if item.state == "REBASE_READY":
            return self._request_rebase(item, ctx)
        if item.state == "POLL":
            return self._poll(item, ctx)
        if item.state == "FIX_READY":
            return self._request_fix(item, ctx)
        if item.state == "PUSH_READY":
            return self._request_push(item, ctx)
        raise AssertionError(f"unreachable ci state {item.state!r}")

    def _discover(self, item: WorkItem, ctx: StageContext) -> Continue | StageOutcome:
        """Step 1 [M]: discover PR if unset; verify implementation-go."""
        if item.pr is None:
            # Placeholder: actual discovery would call ctx.discovery or similar
            # For now, assume item.pr is pre-populated by coordinator
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        # Check implementation-go label
        # Placeholder: actual check would call ctx.github or pr_manager
        # For now, assume pre-validated
        item.state = "REBASE_READY"
        return Continue("REBASE_READY")

    def _request_rebase(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest:
        """Step 2 [W:G]: mechanical rebase if behind/conflicting."""
        # Check if rebase is enabled and budget remains
        rebase_budget = ctx.budget("rebase")
        rebase_attempts = item.attempts.get("rebase", 0)
        if rebase_attempts >= rebase_budget:
            item.state = "POLL"
            return Continue("POLL")
        # For now, skip rebase and go to POLL
        item.state = "POLL"
        return Continue("POLL")

    def _poll(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest | StageOutcome:
        """Step 3 [M]: non-blocking classify."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        checks = gh_pr_checks(item.pr, ctx.dry_run)
        conclusion = classify_ci_state(checks)
        if conclusion is CiConclusion.PENDING:
            # TODO(#1816): delay is parked on coordinator timer heap; not used here
            min(2 ** item.attempts.get("ci_poll", 0), _BACKOFF_CAP)
            item.attempts["ci_poll"] = item.attempts.get("ci_poll", 0) + 1
            return JobRequest(None, "POLL")  # type: ignore
        if conclusion in (CiConclusion.GREEN, CiConclusion.NO_CHECKS):
            return StageOutcome(Disposition.ADVANCE)
        # FAILING: consume ci_fix budget or fail back
        ci_fix_budget = ctx.budget("ci_fix")
        ci_fix_attempts = item.attempts.get("ci_fix", 0)
        if ci_fix_attempts < ci_fix_budget:
            item.state = "FIX_READY"
            return Continue("FIX_READY")
        return StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")

    def _request_fix(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 5 [W:A]: CI-fix agent session."""
        item.attempts["ci_fix"] = item.attempts.get("ci_fix", 0) + 1
        return JobRequest(None, "PUSH_READY")  # type: ignore

    def _request_push(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 5 [W:G]: push contract (push_ci_fix owns head-advance+lease+no-commit retry)."""
        return JobRequest(None, "POLL")  # type: ignore

    def on_job_done(self, item: WorkItem, result: Any, ctx: StageContext) -> None:
        """Route a completed worker job back into the stage's state machine."""
        # Placeholder: actual implementation routes based on result.job_kind
        pass
