"""Finished stage: record outcomes and clean up worktrees (epic #1809).

Binding contract: docs/AUTOMATION_LOOP_ARCHITECTURE.md section "finished".

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

from hephaestus.automation.pipeline.work_item import ItemResult, PreservedWorktree

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
            (``(repo, item_number, worktree_path)`` tuples) the summary prints.
            Failed issue items use the issue number; PR-only items use the PR
            number; unknown items fall back to 0.

    """

    kind = StageName.FINISHED

    def __init__(
        self,
        ledger: list[ItemResult],
        preserved: list[PreservedWorktree],
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
        """Remove all owned worktrees on pass; preserve them on fail."""
        worktrees = self._cleanup_worktrees(item)
        if not worktrees:
            return Continue(next_state="DONE")

        passed = bool(item.result and item.result.passed)
        if not passed:
            for worktree in worktrees:
                entry = (item.repo, item.issue or item.pr or 0, worktree)
                if entry not in self._preserved:
                    self._preserved.append(entry)
                logger.info(
                    "finished:%s: preserving worktree for debugging: %s",
                    item.issue or item.repo,
                    worktree,
                )
            return Continue(next_state="DONE")

        if ctx.dry_run:
            for worktree in worktrees:
                logger.info("[dry-run] would remove worktree %s", worktree)
            return Continue(next_state="DONE")

        cleanup_index = item.payload.get("_finished_cleanup_index", 0)
        if not isinstance(cleanup_index, int) or cleanup_index < 0:
            cleanup_index = 0
        if cleanup_index >= len(worktrees):
            item.payload.pop("_finished_cleanup_index", None)
            return Continue(next_state="DONE")
        worktree = worktrees[cleanup_index]

        job = GitJob(
            repo=item.repo,
            op="remove_worktree",
            timeout_s=GIT_JOB_TIMEOUT_S,
            # Use the concrete worktree path: the cleanup worker constructs a
            # fresh WorktreeManager, so its in-memory issue map is empty.
            kwargs={
                "worktree_path": worktree,
                "repo_root": str(ctx.paths.repo_root),
                "force": True,
            },
            descr=f"remove worktree {worktree}",
        )
        return JobRequest(job=job, on_done_state="CLEANUP")

    @staticmethod
    def _cleanup_worktrees(item: WorkItem) -> list[str]:
        """Return the primary and strict auxiliary worktrees once each."""
        candidates: list[object] = [item.worktree, item.payload.get("strict_review_worktree")]
        auxiliary = item.payload.get("strict_review_worktrees")
        if isinstance(auxiliary, list):
            candidates.extend(auxiliary)

        worktrees: list[str] = []
        for candidate in candidates:
            if isinstance(candidate, str) and candidate and candidate not in worktrees:
                worktrees.append(candidate)
        return worktrees

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Log cleanup failures (never fatal — the result is already recorded).

        Args:
            item: The work item.
            result: The remove_worktree job result.
            ctx: Stage context.

        """
        cleanup_index = item.payload.get("_finished_cleanup_index", 0)
        if not isinstance(cleanup_index, int) or cleanup_index < 0:
            cleanup_index = 0
        item.payload["_finished_cleanup_index"] = cleanup_index + 1
        if not result.ok:
            logger.warning(
                "finished:%s: worktree cleanup failed (non-fatal): %s",
                item.issue or item.repo,
                result.error,
            )
