"""Finished stage: record outcomes and clean up worktrees (epic #1809).

Binding contract: docs/architecture.md §5.7 "finished".

The universal sink. States: ENTER -> RECORD -> CLEANUP -> DONE.

Steps:

1. [M] RECORD: append the item's :class:`~..work_item.ItemResult` to the run
   ledger (the coordinator injects its ledger list at construction — queue
   and ledger ownership stay with the coordinator).
2. [W:G] CLEANUP: remove the implementation worktree on pass; on fail, preserve that writer
   worktree for debugging and record it in the preserved list the end-of-run
   summary prints.

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
from .repo import (
    DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY,
    DIRECT_SCOPE_RESERVATION_KEY,
    is_full_commit_sha,
)

logger = logging.getLogger(__name__)

_RESERVATION_RELEASE_RETRY_CAP = 2


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
        """Clean or preserve the writer worktree."""
        reservation = item.payload.get(DIRECT_SCOPE_RESERVATION_KEY)
        if not item.payload.get("_direct_scope_reservation_release_attempted", False):
            if isinstance(reservation, dict):
                branch_name = reservation.get("branch")
                base_sha = reservation.get("base_sha")
                if isinstance(branch_name, str) and is_full_commit_sha(base_sha):
                    # The branch contains no coordinator-published commit.
                    # Release only if its remote ref is still the exact base
                    # we reserved, so a later human/concurrent writer is
                    # never deleted.  This also runs for failed items whose
                    # writer worktree is deliberately preserved.
                    attempts = int(
                        item.payload.get("_direct_scope_reservation_release_attempts", 0)
                    )
                    if attempts < _RESERVATION_RELEASE_RETRY_CAP:
                        item.payload["_direct_scope_reservation_release_attempts"] = attempts + 1
                        item.payload["_direct_scope_reservation_release_inflight"] = True
                        return JobRequest(
                            job=GitJob(
                                repo=item.repo,
                                op="release_branch_reservation",
                                timeout_s=GIT_JOB_TIMEOUT_S,
                                kwargs={
                                    "branch": branch_name,
                                    "base_sha": base_sha,
                                    "repo_root": str(ctx.paths.repo_root),
                                },
                                descr=f"release unused direct-scope branch {branch_name}",
                            ),
                            on_done_state="CLEANUP",
                        )
                    logger.warning(
                        "finished:%s: could not release direct-scope reservation after %d attempts",
                        item.issue or item.repo,
                        attempts,
                    )
                else:
                    logger.warning(
                        "finished:%s: invalid direct-scope reservation receipt; "
                        "not releasing branch",
                        item.issue or item.repo,
                    )
            item.payload["_direct_scope_reservation_release_attempted"] = True

        if not item.worktree:
            return Continue(next_state="DONE")

        passed = bool(item.result and item.result.passed)
        if not passed:
            entry = (item.repo, item.issue or item.pr or 0, item.worktree)
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

        kwargs: dict[str, object] = {
            "worktree_path": item.worktree,
            "repo_root": str(ctx.paths.repo_root),
            "force": True,
        }
        local_cleanup = item.payload.get(DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY)
        if isinstance(local_cleanup, dict):
            kwargs["local_branch_cleanup"] = local_cleanup
        job = GitJob(
            repo=item.repo,
            op="remove_worktree",
            timeout_s=GIT_JOB_TIMEOUT_S,
            # Use the concrete worktree path: the cleanup worker constructs a
            # fresh WorktreeManager, so its in-memory issue map is empty.
            kwargs=kwargs,
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
        if item.payload.pop("_direct_scope_reservation_release_inflight", False):
            if result.ok:
                item.payload["_direct_scope_reservation_release_attempted"] = True
                item.payload.pop(DIRECT_SCOPE_RESERVATION_KEY, None)
                if result.value is False:
                    logger.warning(
                        "finished:%s: direct-scope reservation changed and was not released",
                        item.issue or item.repo,
                    )
            elif (
                item.payload.get("_direct_scope_reservation_release_attempts", 0)
                >= _RESERVATION_RELEASE_RETRY_CAP
            ):
                item.payload["_direct_scope_reservation_release_attempted"] = True
                logger.warning(
                    "finished:%s: direct-scope reservation release failed after retries: %s",
                    item.issue or item.repo,
                    result.error,
                )
            return
        if not result.ok:
            logger.warning(
                "finished:%s: worktree cleanup failed (non-fatal): %s",
                item.issue or item.repo,
                result.error,
            )
        elif isinstance(result.value, dict) and result.value.get("local_branch_deleted") is False:
            logger.warning(
                "finished:%s: local direct-scope branch changed and was not deleted",
                item.issue or item.repo,
            )
