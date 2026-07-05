"""Finished stage: record outcomes and clean up worktrees (epic #1809).

Binding contract: docs/AUTOMATION_LOOP_ARCHITECTURE.md section "8. finished".

The universal sink. States: ENTER -> RECORD -> CLEANUP -> DONE.

Steps:

1. [M] RECORD: append the item's :class:`~..work_item.ItemResult` to the run
   ledger (the coordinator injects its ledger list at construction — queue
   and ledger ownership stay with the coordinator).
2. [W:G] CLEANUP: ``GitJob(op="remove_worktree")`` on pass; on fail the
   worktree is PRESERVED for debugging and recorded in the preserved list
   the end-of-run summary prints.

Verdicts: terminal — no outgoing routes (the coordinator drops the item
when the sink emits its final outcome).
"""

from __future__ import annotations

import logging

from hephaestus.automation.pipeline.work_item import ItemResult

from .base import (
    GIT_JOB_TIMEOUT_S,
    Continue,
    Disposition,
    GitJob,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageName,
    StageOutcome,
    StepResult,
    WorkItem,
)

logger = logging.getLogger(__name__)


class FinishedStage(Stage):
    """Sink stage: record :class:`ItemResult` and clean up worktrees.

    Args:
        ledger: The coordinator's run ledger; RECORD appends here.
        preserved: The coordinator's preserved-worktree list
            (``(issue_number, worktree_path)`` tuples) the summary prints.

    """

    kind = StageName.FINISHED

    def __init__(
        self,
        ledger: list[ItemResult],
        preserved: list[tuple[int, str]],
    ) -> None:
        """Bind the coordinator-owned ledger and preserved-worktree list."""
        self._ledger = ledger
        self._preserved = preserved

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Proceed unconditionally (the sink never routes away).

        Args:
            item: The finished work item (``item.result`` set by the router).
            ctx: Stage context.

        Returns:
            None always.

        """
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next sink action for the item's current state.

        Args:
            item: The work item; ``item.result`` was set when it was routed
                here (a missing result is recorded as an internal failure —
                never silently dropped).
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or the terminal StageOutcome.

        """
        if item.state in ("", "ENTER"):
            return Continue(next_state="RECORD")

        if item.state == "RECORD":
            if item.result is None:  # defensive: router always sets it
                item.result = ItemResult(
                    passed=False, reason="internal: no result recorded", final_stage=item.stage
                )
            if not item.payload.get("_recorded", False):
                self._ledger.append(item.result)
                item.payload["_recorded"] = True
            return Continue(next_state="CLEANUP")

        if item.state == "CLEANUP":
            return self._cleanup(item, ctx)

        if item.state == "DONE":
            return StageOutcome(Disposition.FINISH_PASS, note="done")

        return StageOutcome(Disposition.FINISH_FAIL, note=f"unknown state: {item.state}")

    def _cleanup(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Remove the worktree on pass; preserve (and record) it on fail."""
        if not item.worktree:
            return Continue(next_state="DONE")

        passed = bool(item.result and item.result.passed)
        if not passed:
            entry = (item.issue or 0, item.worktree)
            if entry not in self._preserved:
                self._preserved.append(entry)
            logger.info(
                "finished:%s: preserving worktree for debugging: %s",
                item.issue or item.repo,
                item.worktree,
            )
            return Continue(next_state="DONE")

        if ctx.dry_run:
            logger.info("[dry-run] would remove worktree %s", item.worktree)
            return Continue(next_state="DONE")

        job = GitJob(
            repo=item.repo,
            op="remove_worktree",
            timeout_s=GIT_JOB_TIMEOUT_S,
            # worker_pool dispatches to WorktreeManager.remove_worktree(**kwargs).
            kwargs={"issue_number": item.issue or 0, "force": True},
            descr=f"remove worktree {item.worktree}",
        )
        return JobRequest(job=job, on_done_state="DONE")

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Log cleanup failures (never fatal — the result is already recorded).

        Args:
            item: The work item.
            result: The remove_worktree job result.
            ctx: Stage context.

        """
        if not result.ok:
            logger.warning(
                "finished:%s: worktree cleanup failed (non-fatal): %s",
                item.issue or item.repo,
                result.error,
            )
