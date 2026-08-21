"""Finished stage: record outcomes and clean up worktrees (epic #1809).

Binding contract: docs/architecture.md §5.8 "finished".

The universal sink. States: ENTER -> RECORD -> CLEANUP -> DONE.

Steps:

1. [M] RECORD: append the item's :class:`~..work_item.ItemResult` to the run
   ledger (the coordinator injects its ledger list at construction — queue
   and ledger ownership stay with the coordinator).
2. [W:G] CLEANUP: after all durable learning records are terminal, remove a clean
   implementation worktree on pass. On fail, or while learning is pending,
   preserve that writer worktree and record it in the preserved list that the
   end-of-run summary prints. Cleanup never forces removal. A direct-scope no-op
   also removes its local branch only when its ownership receipt still matches.

Verdicts: terminal — no outgoing routes (the coordinator drops the item
when the sink emits its final outcome).
"""

from __future__ import annotations

import logging
from pathlib import Path

from hephaestus.automation.direct_review_recovery import (
    is_inspection_only_detached_push_failure,
    list_direct_review_recovery_paths,
)
from hephaestus.automation.issue_waves import (
    WAVE_LEASE_PAYLOAD,
    IssueWaveError,
    IssueWaveStore,
    WaveLease,
)
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
    stage_timeout,
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
        recovery_preserved: The coordinator's direct-review recovery list.
            These checkouts remain reported even if a later fresh review
            succeeds, unlike ordinary failed-item debugging worktrees.

    """

    kind = StageName.FINISHED

    def __init__(
        self,
        ledger: list[ItemResult],
        preserved: list[PreservedWorktree],
        recovery_preserved: list[PreservedWorktree],
    ) -> None:
        """Bind the coordinator-owned ledger and preserved-worktree list."""
        self._ledger = ledger
        self._preserved = preserved
        self._recovery_preserved = recovery_preserved

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
            lease = item.payload.get(WAVE_LEASE_PAYLOAD)
            if (
                isinstance(lease, WaveLease)
                and item.issue is not None
                and not item.payload.get("_wave_outcome_recorded", False)
            ):
                try:
                    IssueWaveStore(
                        Path(str(ctx.paths.repo_root)), ctx.org, item.repo
                    ).record_terminal_outcome(
                        lease,
                        issue_number=item.issue,
                        passed=item.result.passed,
                        reason=item.result.reason,
                        pr_number=item.pr,
                    )
                except IssueWaveError as exc:
                    # The checkpoint is authoritative for wave advancement.
                    # Convert a successful-looking item to a failure and make
                    # one best-effort failed write before preserving its tree.
                    item.result = ItemResult(
                        passed=False,
                        reason=f"wave checkpoint write failed: {exc}",
                        final_stage=item.stage,
                    )
                    try:
                        IssueWaveStore(
                            Path(str(ctx.paths.repo_root)), ctx.org, item.repo
                        ).record_terminal_outcome(
                            lease,
                            issue_number=item.issue,
                            passed=False,
                            reason=item.result.reason,
                            pr_number=item.pr,
                        )
                    except IssueWaveError:
                        logger.exception("finished:%s: failed to persist wave failure", item.issue)
                item.payload["_wave_outcome_recorded"] = True
            if not item.payload.get("_recorded", False):
                self._ledger.append(item.result)
                item.payload["_recorded"] = True
            return Continue(next_state="CLEANUP")

        if item.state == "CLEANUP":
            return self._cleanup(item, ctx)

        if item.state == "DONE":
            self._record_cleanup_terminal(item, ctx)
            return StageOutcome(Disposition.FINISH_PASS, note="done")

        return StageOutcome(Disposition.FINISH_FAIL, note=f"unknown state: {item.state}")

    @staticmethod
    def _record_cleanup_terminal(item: WorkItem, ctx: StageContext) -> None:
        """Close durable cleanup obligations after the sink reaches DONE."""
        if item.post_processing is None:
            return
        succeeded = bool(item.payload.pop("_learning_cleanup_succeeded", True))
        error = str(item.payload.pop("_learning_cleanup_error", ""))
        for key in item.post_processing.intent_keys:
            try:
                ctx.learning_journal.finish_cleanup(
                    key,
                    succeeded=succeeded,
                    error=error,
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning(
                    "finished:%s: could not record cleanup completion for %s: %s",
                    item.issue or item.repo,
                    key,
                    exc,
                )

    def _cleanup(  # noqa: C901 - cleanup validates independent durable receipts
        self, item: WorkItem, ctx: StageContext
    ) -> StepResult:
        """Clean or preserve the writer worktree."""
        recovery_worktrees = self._record_recovery_worktrees(item, ctx)
        if item.worktree and not self._learning_is_terminal(item, ctx):
            return self._preserve_pending_learning_worktree(item)
        reservation = item.payload.get(DIRECT_SCOPE_RESERVATION_KEY)
        if not item.payload.get("_direct_scope_reservation_release_attempted", False):
            if isinstance(reservation, dict):
                branch_name = reservation.get("branch")
                base_sha = reservation.get("base_sha")
                owns_branch = isinstance(branch_name, str) and branch_name == item.branch
                if owns_branch and is_full_commit_sha(base_sha):
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
                                timeout_s=stage_timeout(ctx, "metadata", GIT_JOB_TIMEOUT_S),
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
                        "finished:%s: invalid or stale direct-scope reservation receipt; "
                        "not releasing branch",
                        item.issue or item.repo,
                    )
                    item.payload["_learning_cleanup_succeeded"] = False
                    item.payload["_learning_cleanup_error"] = "cleanup ownership changed"
            item.payload["_direct_scope_reservation_release_attempted"] = True

        if not item.worktree:
            return Continue(next_state="DONE")

        passed = bool(item.result and item.result.passed)
        local_cleanup = item.payload.get(DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY)
        is_direct_noop = (
            isinstance(local_cleanup, dict)
            and isinstance(local_cleanup.get("branch"), str)
            and bool(local_cleanup["branch"])
            and is_full_commit_sha(local_cleanup.get("base_sha"))
        )
        inspection_only = (
            is_inspection_only_detached_push_failure(item.payload.get("detached_push_failure"))
            or item.worktree in recovery_worktrees
        )
        if not passed and inspection_only:
            entry = (item.repo, item.issue or item.pr or 0, item.worktree)
            if entry not in self._recovery_preserved:
                self._recovery_preserved.append(entry)
            logger.info(
                "finished:%s: retaining detached-review checkout for inspection: %s",
                item.issue or item.repo,
                item.worktree,
            )
            return Continue(next_state="DONE")

        if not passed and not is_direct_noop:
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
            "issue_number": item.issue or item.pr or 0,
            # A no-op direct scope is known-clean at commit/push time, but a
            # human may edit it before this terminal cleanup runs. Refuse to
            # discard that late edit; on failure the worktree is preserved.
            # Learning has finished, but a user can still edit the checkout.
            # Cleanup must preserve those edits instead of forcing removal.
            "force": False,
            **({"expected_branch": item.branch} if item.branch else {}),
        }
        expected_head = item.payload.get("_worktree_cleanup_head_sha")
        if is_full_commit_sha(expected_head):
            kwargs["expected_head"] = expected_head
        if is_direct_noop:
            kwargs["local_branch_cleanup"] = local_cleanup
            item.payload["_direct_scope_noop_cleanup_inflight"] = True
        job = GitJob(
            repo=item.repo,
            op="remove_worktree",
            timeout_s=stage_timeout(ctx, "metadata", GIT_JOB_TIMEOUT_S),
            # Use the concrete worktree path: the cleanup worker constructs a
            # fresh WorktreeManager, so its in-memory issue map is empty.
            kwargs=kwargs,
            descr=f"remove worktree {item.worktree}",
        )
        return JobRequest(job=job, on_done_state="DONE")

    def _preserve_pending_learning_worktree(self, item: WorkItem) -> StageOutcome:
        """Eject a writer checkout until all learning work is terminal."""
        entry = (item.repo, item.issue or item.pr or 0, item.worktree)
        if entry not in self._preserved:
            self._preserved.append(entry)
        logger.warning(
            "finished:%s: preserving worktree until learning is terminal: %s",
            item.issue or item.repo,
            item.worktree,
        )
        return StageOutcome(Disposition.EJECT, "learning_cleanup_pending")

    @staticmethod
    def _learning_is_terminal(item: WorkItem, ctx: StageContext) -> bool:
        """Return whether all cleanup-bound learning records are terminal."""
        if item.post_processing is None:
            return True
        journal = ctx.learning_journal
        if journal is None:
            return False
        for key in item.post_processing.intent_keys:
            try:
                record = journal.load(key)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                return False
            if not isinstance(record, dict) or record.get("status") not in {
                "succeeded",
                "failed",
                "disabled",
            }:
                return False
        return True

    def _record_recovery_worktrees(self, item: WorkItem, ctx: StageContext) -> set[str]:
        """Record receipt-backed recoveries and return their concrete paths."""
        if item.issue is None or item.pr is None:
            return set()
        try:
            recovery_worktrees = list_direct_review_recovery_paths(
                repo_root=ctx.paths.repo_root,
                issue=item.issue,
                pr=item.pr,
            )
        except (OSError, RuntimeError) as error:
            logger.warning(
                "finished:%s: could not read detached-review recovery receipts: %s",
                item.issue or item.repo,
                error,
            )
            return set()
        paths = {str(worktree) for worktree in recovery_worktrees}
        for worktree in recovery_worktrees:
            entry = (item.repo, item.issue, str(worktree))
            if entry not in self._recovery_preserved:
                self._recovery_preserved.append(entry)
        return paths

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
                item.payload["_learning_cleanup_succeeded"] = False
                item.payload["_learning_cleanup_error"] = result.error
                logger.warning(
                    "finished:%s: direct-scope reservation release failed after retries: %s",
                    item.issue or item.repo,
                    result.error,
                )
            return
        if not result.ok:
            item.payload["_learning_cleanup_succeeded"] = False
            item.payload["_learning_cleanup_error"] = result.error
            if item.payload.pop("_direct_scope_noop_cleanup_inflight", False):
                entry = (item.repo, item.issue or item.pr or 0, item.worktree)
                if entry not in self._preserved:
                    self._preserved.append(entry)
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
