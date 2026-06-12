"""PR-creation phase (pre-PR test gate + ensure-PR fallback).

Extracted from :class:`ImplementationPhaseRunner` as part of the #712
decomposition. :class:`PRCreatePhase` owns the optional pre-PR test gate and
the idempotent "make sure a PR exists for this branch" fallback, persisting
the resulting PR number on the issue state.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ._stage_context import StageMixin
from .git_utils import issue_ref
from .models import ImplementationPhase, ImplementationState
from .pr_manager import ensure_pr_created

if TYPE_CHECKING:
    from ._stage_context import StageContext

logger = logging.getLogger(__name__)


class PRCreatePhase(StageMixin):
    """Finalize a branch into an open PR (with an optional pre-PR test gate)."""

    def __init__(self, ctx: StageContext) -> None:
        """Store the shared :class:`StageContext`."""
        self.ctx = ctx

    def _finalize_pr(
        self,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
    ) -> int:
        """Ensure commit is pushed and PR is created, then persist the PR number."""
        impl = self.impl
        with self.state_lock:
            state.phase = ImplementationPhase.CREATING_PR
        impl._save_state(state)

        # A2-004: optional pre-PR test gate (opt-in via run_pre_pr_tests=True).
        if self.options.run_pre_pr_tests:
            if slot_id is not None:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: Running pre-PR tests"
                )
            tests_passed = impl._run_tests_in_worktree(worktree_path, issue_number)
            if not tests_passed:
                logger.warning(
                    "#%d: pre-PR tests failed; PR will still be created but "
                    "manual review is required before merging",
                    issue_number,
                )

        pr_number = impl._ensure_pr_created(issue_number, branch_name, worktree_path, slot_id)
        with self.state_lock:
            state.pr_number = pr_number
        impl._save_state(state)
        return pr_number

    def _run_tests_in_worktree(self, worktree_path: Path, issue_number: int) -> bool:
        """Run the unit test suite inside the worktree as a pre-PR gate (A2-004)."""
        try:
            result = subprocess.run(
                ["pixi", "run", "pytest", "tests/unit", "-q", "--tb=short"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                logger.info("#%d: pre-PR tests passed", issue_number)
                return True
            logger.warning(
                "#%d: pre-PR tests FAILED (exit %d):\n%s",
                issue_number,
                result.returncode,
                (result.stdout + result.stderr)[-2000:],
            )
            return False
        except subprocess.TimeoutExpired:
            logger.warning("#%d: pre-PR tests timed out after 600s", issue_number)
            return False
        except Exception as e:
            logger.warning("#%d: pre-PR tests could not run: %s", issue_number, e)
            return False

    def _ensure_pr_created(
        self,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        slot_id: int | None = None,
    ) -> int:
        """Ensure commit is pushed and PR is created (fallback if Claude didn't do it)."""
        return ensure_pr_created(
            issue_number,
            branch_name,
            worktree_path,
            self.options.auto_merge,
            self.status_tracker,
            slot_id,
            self.options.agent,
        )
