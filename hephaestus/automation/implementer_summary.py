"""End-of-run summary printer for :mod:`hephaestus.automation.implementer`.

Extracted from :class:`IssueImplementer` as part of the #597 decomposition.
The printer owns the "render final results to ``logger.info``" concern and
nothing else — it does not consult the state manager, mutate any state, or
touch the filesystem.

The printer reads the list of *preserved* worktrees off the
``WorktreeManager`` it is constructed with, which mirrors the legacy
inline behavior exactly.
"""

from __future__ import annotations

import logging
import sys

from .models import WorkerResult
from .pipeline.summary import format_preserved_worktrees
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


class ImplementationSummaryPrinter:
    """Render the end-of-run summary to ``logger.info``.

    Attributes:
        worktree_manager: Source of preserved-worktree entries. Held by
            reference so the summary sees the same list the orchestrator
            populated during the run.

    """

    def __init__(self, worktree_manager: WorktreeManager) -> None:
        """Initialize the printer.

        Args:
            worktree_manager: Worktree manager whose ``preserved`` list will
                be appended to the summary footer.

        """
        self.worktree_manager = worktree_manager

    @staticmethod
    def _tally(results: dict[int, WorkerResult]) -> tuple[int, int, int, int, int]:
        """Return ``(total, successful, deferred, skipped_has_pr, failed)``.

        Outcome classes are mutually exclusive: a result counts as deferred,
        skipped-because-PR, or successful — never more than one — so the four
        sub-counts plus failed sum to total.
        """
        total = len(results)
        deferred = sum(1 for r in results.values() if r.plan_review_not_go)
        skipped_has_pr = sum(1 for r in results.values() if r.already_has_pr)
        successful = sum(
            1
            for r in results.values()
            if r.success and not r.plan_review_not_go and not r.already_has_pr
        )
        failed = total - successful - deferred - skipped_has_pr
        return total, successful, deferred, skipped_has_pr, failed

    def print(self, results: dict[int, WorkerResult]) -> None:
        """Print the implementation summary for *results*."""
        total, successful, deferred, skipped_has_pr, failed = self._tally(results)

        logger.info("=" * 60)
        logger.info("Implementation Summary")
        logger.info("=" * 60)
        logger.info("Total issues: %s", total)
        logger.info("Successful: %s", successful)
        logger.info("Deferred (awaiting GO plan-review): %s", deferred)
        logger.info("Skipped (open PR already exists): %s", skipped_has_pr)
        logger.info("Failed: %s", failed)

        if successful > 0:
            logger.info("\nSuccessful PRs:")
            for issue_num, result in results.items():
                if result.success and result.pr_number and not result.already_has_pr:
                    logger.info("  #%s: PR #%s", issue_num, result.pr_number)

        if skipped_has_pr > 0:
            logger.info("\nSkipped (open PR already exists):")
            for issue_num, result in results.items():
                if result.already_has_pr:
                    logger.info("  #%s: PR #%s", issue_num, result.pr_number)

        if failed > 0:
            logger.info("\nFailed issues:")
            for issue_num, result in results.items():
                if not result.success:
                    logger.info("  #%s: %s", issue_num, result.error)

        self._print_preserved_worktrees()

    def _print_preserved_worktrees(self) -> None:
        """Log the preserved-worktree footer (issues with uncommitted changes).

        The line sequence lives in
        :func:`~hephaestus.automation.pipeline.summary.format_preserved_worktrees`
        (re-housed by #1817) so the pipeline summary and this legacy printer
        emit identical guidance.
        """
        for line in format_preserved_worktrees(self.worktree_manager.preserved, sys.argv[0]):
            logger.info("%s", line)
