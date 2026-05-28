"""Bulk issue planning using Claude Code.

Provides:
- Parallel issue planning
- Duplicate plan detection
- Rate limit handling
- Plan posting to GitHub issues
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess

# fcntl is POSIX-only; CPython does not bundle it on Windows. Import lazily so
# this module stays importable on Windows for tests that only need its
# pure-Python helpers. The cross-process file locking that uses fcntl is only
# reached on the live planner path.
try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None  # type: ignore[assignment]
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import add_agent_argument
from hephaestus.cli.utils import add_json_arg, emit_json_status

from .claude_models import advise_model
from .git_utils import get_repo_root, issue_ref
from .github_api import (
    GitHubRateLimitError,
    gh_issue_comment,
    gh_list_open_issues,
)
from .models import PlannerOptions, PlanResult
from .planner_claude import PlannerClaudeRunner
from .planner_review_loop import MAX_REVIEW_ITERATIONS, PlanReviewLoop
from .planner_state import PlannerStateManager
from .prompts import (
    get_advise_prompt,
)
from .session_naming import AGENT_ADVISE
from .status_tracker import StatusTracker
from .work_report import write_work_report

__all__ = ["MAX_REVIEW_ITERATIONS", "Planner", "main"]

logger = logging.getLogger(__name__)


class Planner:
    """Plans GitHub issues using Claude Code.

    Supports parallel planning with rate limit handling and
    duplicate detection.
    """

    _mnemosyne_lock: threading.Lock = threading.Lock()

    def __init__(self, options: PlannerOptions):
        """Initialize planner.

        Args:
            options: Planner configuration options

        """
        self.options = options
        self.status_tracker = StatusTracker(options.parallel)
        self.results: dict[int, PlanResult] = {}
        self.lock = threading.Lock()
        self.state_mgr = PlannerStateManager(options)
        self.claude_runner = PlannerClaudeRunner(options)
        self.review_loop = PlanReviewLoop(self)

    def run(self) -> dict[int, PlanResult]:
        """Run the planner on all issues.

        Returns:
            Dictionary mapping issue number to PlanResult

        """
        logger.info(
            "Planning %s issues with %s parallel workers",
            len(self.options.issues),
            self.options.parallel,
        )

        # Filter closed issues if requested
        issues_to_plan = self._filter_issues()

        if not issues_to_plan:
            logger.warning("No issues to plan")
            return {}

        # Plan issues in parallel
        with ThreadPoolExecutor(max_workers=self.options.parallel) as executor:
            futures: dict[Future[Any], int] = {}

            for issue_num in issues_to_plan:
                future = executor.submit(self._plan_issue, issue_num)
                futures[future] = issue_num

            # Collect results
            for future in as_completed(futures):
                issue_num = futures[future]
                try:
                    result = future.result()
                    with self.lock:
                        self.results[issue_num] = result
                except Exception as e:
                    logger.error("Failed to plan %s: %s", issue_ref(issue_num), e)
                    with self.lock:
                        self.results[issue_num] = PlanResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

        self._print_summary()
        return self.results

    def _filter_issues(self) -> list[int]:
        """Filter issues based on options (delegates to state manager)."""
        return self.state_mgr.filter()

    def _has_existing_plan(self, issue_number: int) -> bool:
        """Check if an issue already has a plan (delegates to state manager)."""
        return self.state_mgr.has_existing_plan(issue_number)

    def _plan_issue(self, issue_number: int) -> PlanResult:
        """Plan a single issue.

        Args:
            issue_number: Issue number to plan

        Returns:
            PlanResult

        """
        slot_id = self.status_tracker.acquire_slot()
        if slot_id is None:
            return PlanResult(
                issue_number=issue_number,
                success=False,
                error="Failed to acquire worker slot",
            )

        try:
            # Skip-if-already-planned moved here from _filter_issues (#548) so
            # the check runs inside the thread pool (parallel, overlapped with
            # actual planning work) instead of as a serial pre-pass that
            # blocked all workers behind N ``gh issue view --comments`` calls.
            if not self.options.force:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: checking existing plan"
                )
                if self._has_existing_plan(issue_number):
                    logger.info("Issue #%s already has a plan, skipping", issue_number)
                    self.status_tracker.update_slot(
                        slot_id, f"{issue_ref(issue_number)}: plan exists, skipped"
                    )
                    return PlanResult(
                        issue_number=issue_number,
                        success=True,
                        plan_already_exists=True,
                    )

            self.status_tracker.update_slot(slot_id, f"Planning {issue_ref(issue_number)}")

            if self.options.dry_run:
                logger.info("[DRY RUN] Would plan %s", issue_ref(issue_number))
                return PlanResult(issue_number=issue_number, success=True)

            # Run the strict review loop: advise → loop[plan → learn → review]
            # → post final plan with last review attached. Loop terminates on
            # the first unambiguous GO or after MAX_REVIEW_ITERATIONS.
            plan, final_review, iterations, verdict_is_go = self._run_plan_review_loop(
                issue_number, slot_id
            )

            # Post final plan + review to issue regardless of verdict so
            # operators can see what was produced (NOGO banner is appended
            # inside _post_plan when verdict_is_go is False).
            self._post_plan(
                issue_number, plan, final_review=final_review, verdict_is_go=verdict_is_go
            )

            self.status_tracker.update_slot(
                slot_id, f"Completed {issue_ref(issue_number)} ({iterations} iter)"
            )

            if not verdict_is_go:
                return PlanResult(
                    issue_number=issue_number,
                    success=False,
                    error=(
                        "review loop exhausted all iterations without a GO verdict (NOGO-exhausted)"
                    ),
                )

            return PlanResult(issue_number=issue_number, success=True)

        except Exception as e:
            logger.error("Failed to plan %s: %s", issue_ref(issue_number), e)
            return PlanResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )
        finally:
            self.status_tracker.release_slot(slot_id)

    def _call_claude(
        self,
        prompt: str,
        *,
        model: str,
        agent: str,
        issue_number: int | str,
        max_retries: int = 3,
        timeout: int = 300,
        extra_args: list[str] | None = None,
    ) -> str:
        """Call Claude (delegates to claude_runner)."""
        return self.claude_runner.call_claude(
            prompt,
            model=model,
            agent=agent,
            issue_number=issue_number,
            max_retries=max_retries,
            timeout=timeout,
            extra_args=extra_args,
        )

    def _call_codex(
        self,
        prompt: str,
        *,
        model: str,
        max_retries: int = 3,
        timeout: int = 300,
    ) -> str:
        """Call Codex (delegates to claude_runner)."""
        return self.claude_runner.call_codex(
            prompt, model=model, max_retries=max_retries, timeout=timeout
        )

    def _ensure_mnemosyne(self, mnemosyne_root: Path) -> bool:
        """Clone ProjectMnemosyne if it does not exist locally.

        Uses a class-level threading lock and an fcntl file lock to prevent
        race conditions when multiple parallel workers call this simultaneously.

        Args:
            mnemosyne_root: Expected local path for ProjectMnemosyne

        Returns:
            True if the directory exists (or was cloned successfully), False otherwise

        """
        with Planner._mnemosyne_lock:
            # TOCTOU guard: re-check inside the lock
            if mnemosyne_root.exists():
                # Refresh stale clone with a fast-forward pull
                try:
                    subprocess.run(
                        ["git", "-C", str(mnemosyne_root), "pull", "--ff-only"],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    logger.debug("ProjectMnemosyne refreshed at %s", mnemosyne_root)
                except Exception as e:
                    logger.warning(
                        "Failed to refresh ProjectMnemosyne (using existing clone): %s", e
                    )
                return True

            lock_path = mnemosyne_root.parent / ".mnemosyne.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)

            with open(lock_path, "w") as lock_file:
                # POSIX-only file locking; on Windows fcntl is None and we
                # degrade gracefully by relying on the in-process thread lock
                # acquired by the surrounding `with self._mnemosyne_lock:`.
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    # Re-check after acquiring file lock
                    if mnemosyne_root.exists():
                        return True

                    logger.info("Cloning ProjectMnemosyne to %s...", mnemosyne_root)
                    subprocess.run(
                        [
                            "gh",
                            "repo",
                            "clone",
                            "HomericIntelligence/ProjectMnemosyne",
                            str(mnemosyne_root),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    logger.info("ProjectMnemosyne cloned successfully")
                    # NOTE: do NOT unlink lock_path here — the file-lock sentinel
                    # must remain on disk until the fd closes in the finally block.
                    # Unlinking while LOCK_EX is held lets a second process open a
                    # new inode at the same path and grab its own lock, breaking
                    # cross-process mutual exclusion (#370).
                    return True

                except subprocess.TimeoutExpired:
                    logger.warning(
                        "gh repo clone timed out after 120 s; ProjectMnemosyne unavailable this run"
                    )
                    return False

                except subprocess.CalledProcessError as e:
                    logger.warning("Failed to clone ProjectMnemosyne: %s", e.stderr or e)
                    return False

                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Search team knowledge base for relevant prior learnings.

        Args:
            issue_number: Issue number
            issue_title: Issue title
            issue_body: Issue body/description

        Returns:
            Advise findings text, or empty string if advise fails

        """
        try:
            # Locate ProjectMnemosyne
            repo_root = get_repo_root()
            mnemosyne_root = repo_root / "build" / "ProjectMnemosyne"

            if not mnemosyne_root.exists() and not self._ensure_mnemosyne(mnemosyne_root):
                return self._advise_skipped("ProjectMnemosyne unavailable")

            marketplace_path = mnemosyne_root / ".claude-plugin" / "marketplace.json"
            if not marketplace_path.exists():
                logger.warning(
                    "Marketplace file not found at %s; "
                    "attempting recovery re-clone of ProjectMnemosyne",
                    marketplace_path,
                )
                shutil.rmtree(mnemosyne_root, ignore_errors=True)
                if not self._ensure_mnemosyne(mnemosyne_root) or not marketplace_path.exists():
                    logger.error(
                        "Recovery failed: marketplace.json still missing at %s; "
                        "skipping advise step",
                        marketplace_path,
                    )
                    return self._advise_skipped(f"marketplace.json missing at {marketplace_path}")

            # Build advise prompt
            advise_prompt = get_advise_prompt(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                marketplace_path=str(marketplace_path),
                repo_root=str(repo_root),
            )

            # Call Claude with shorter timeout. /advise is light search work
            # so it runs on the cheap model.
            logger.info("Running advise for %s...", issue_ref(issue_number))
            findings = self._call_claude(
                advise_prompt,
                model=advise_model(),
                agent=AGENT_ADVISE,
                issue_number=issue_number,
                timeout=180,
            )

            return findings

        except Exception as e:
            logger.warning("Advise step failed for %s: %s", issue_ref(issue_number), e)
            return self._advise_skipped(f"unexpected error: {e}")

    @staticmethod
    def _advise_skipped(reason: str) -> str:
        """Return a marker string for plans that ran without advise findings.

        A silent ``""`` made it impossible for the implementer (or a human
        reading the plan) to tell whether advise wasn't attempted, was
        attempted but found nothing, or actually failed.
        """
        return f"<!-- advise step skipped: {reason} -->"

    def _generate_plan(
        self,
        issue_number: int,
        max_retries: int = 3,
        *,
        prior_review: str | None = None,
        cached_advise: str | None = None,
        cached_issue_data: dict[str, Any] | None = None,
    ) -> str:
        """Generate implementation plan (delegates to review loop)."""
        return self.review_loop.generate_plan(
            issue_number,
            max_retries=max_retries,
            prior_review=prior_review,
            cached_advise=cached_advise,
            cached_issue_data=cached_issue_data,
        )

    def _post_plan(
        self,
        issue_number: int,
        plan: str,
        *,
        final_review: str | None = None,
        verdict_is_go: bool = True,
    ) -> None:
        """Post plan to issue as a comment.

        Args:
            issue_number: Issue number
            plan: Plan text
            final_review: When set, the last reviewer output (Grade + Verdict +
                rationale) is appended in a collapsible section so the human
                reviewer can see why the loop terminated.
            verdict_is_go: When ``False`` a visible NOGO banner is prepended to
                the comment so operators can tell at a glance that the review loop
                exhausted all iterations without approval (#369).

        """
        nogo_banner = ""
        if not verdict_is_go:
            nogo_banner = (
                "> [!WARNING]\n"
                "> **NOGO-EXHAUSTED** — The strict review loop ran all "
                f"{MAX_REVIEW_ITERATIONS} iterations without an unambiguous GO verdict. "
                "This plan was posted for operator review but **should not be implemented** "
                "until a human approves it.\n\n"
            )

        comment_body = f"""# Implementation Plan

{nogo_banner}{plan}
"""

        if final_review:
            comment_body += f"""
---

<details>
<summary>Final review verdict (from strict review loop)</summary>

{final_review}

</details>
"""

        comment_body += """
---
*Generated by Claude Code Planner (strict review loop)*
"""

        gh_issue_comment(issue_number, comment_body)
        logger.info("Posted plan to %s", issue_ref(issue_number))

    # ------------------------------------------------------------------
    # Strict review loop — delegations to PlanReviewLoop
    # ------------------------------------------------------------------

    def _run_plan_review_loop(
        self, issue_number: int, slot_id: int
    ) -> tuple[str, str | None, int, bool]:
        """Run the bounded review loop (delegates to review_loop)."""
        return self.review_loop.run(issue_number, slot_id)

    def _capture_planner_learnings(self, issue_number: int, plan: str) -> str:
        """Capture planner learnings (delegates to review_loop)."""
        return self.review_loop.capture_planner_learnings(issue_number, plan)

    def _run_plan_review(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        plan_text: str,
        learnings: str,
        iteration: int,
        prior_review: str | None,
    ) -> str:
        """Run reviewer pass (delegates to review_loop)."""
        return self.review_loop.run_plan_review(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
            learnings=learnings,
            iteration=iteration,
            prior_review=prior_review,
        )

    def _print_summary(self) -> None:
        """Print summary of planning results."""
        total = len(self.results)
        successful = sum(1 for r in self.results.values() if r.success)
        already_planned = sum(1 for r in self.results.values() if r.plan_already_exists)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("Planning Summary")
        logger.info("=" * 60)
        logger.info("Total issues: %s", total)
        logger.info("Successfully planned: %s", successful - already_planned)
        logger.info("Already planned: %s", already_planned)
        logger.info("Failed: %s", failed)

        if failed > 0:
            logger.info("\nFailed issues:")
            for issue_num, result in self.results.items():
                if not result.success:
                    logger.info("  #%s: %s", issue_num, result.error)


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging

    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments for the planner CLI."""
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="Bulk plan GitHub issues using Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Plan all open issues (no arguments needed)
  %(prog)s

  # Plan specific issues
  %(prog)s --issues 123 456 789

  # Force re-plan even if plan exists
  %(prog)s --issues 123 --force

  # Dry run (no actual planning)
  %(prog)s --issues 123 --dry-run

  # Use custom system prompt
  %(prog)s --issues 123 --system-prompt .claude/agents/planner.md

  # Plan with more parallelism
  %(prog)s --issues 123 456 789 --parallel 5
        """,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        help="Issue numbers to plan (default: all open issues)",
    )
    add_agent_argument(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Suppress GitHub mutations (no issue comments posted). NOTE: Claude "
            "is still invoked to generate plans — dry-run still incurs full "
            "Claude token cost. It is for correctness rehearsal, not cost preview."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-planning even if plan already exists",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=3,
        choices=range(1, 33),
        metavar="N",
        help="Number of parallel workers, 1-32 (default: 3)",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        help="Path to system prompt file for Claude Code",
    )
    parser.add_argument(
        "--no-skip-closed",
        action="store_true",
        help="Plan closed issues (default: skip closed issues)",
    )
    parser.add_argument(
        "--no-advise",
        action="store_true",
        help="Skip the advise step (don't search team knowledge base before planning)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    add_json_arg(parser)

    return parser.parse_args()


def main() -> int:
    """Execute the issue planning workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt

    """
    args = _parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)
    log.info("Starting issue planner")

    if not args.issues:
        try:
            discovered = gh_list_open_issues()
        except GitHubRateLimitError as e:
            # Don't smear a 100-line traceback across the driver's loop output
            # when the only problem is that the GraphQL hourly budget is gone.
            # Exit cleanly so run_automation_loop.sh moves on to the next repo.
            log.error(
                "GitHub API rate-limited; cannot discover issues this run "
                "(reset at epoch %s). Skipping cleanly.",
                e.reset_epoch,
            )
            if args.json:
                emit_json_status(0, message="rate-limited; skipped", reset_epoch=e.reset_epoch)
            return 0
        log.info("No --issues given; discovered %s open issues: %s", len(discovered), discovered)
        args.issues = discovered

    # Dedupe while preserving first-seen order. dict.fromkeys is the
    # canonical "ordered set" trick. Without this, ``--issues 123 123``
    # would race two workers on the same issue and produce double-posts.
    args.issues = list(dict.fromkeys(args.issues))

    log.info("Issues to plan: %s", args.issues)

    try:
        options = PlannerOptions(
            issues=args.issues,
            agent=args.agent,
            dry_run=args.dry_run,
            force=args.force,
            parallel=args.parallel,
            system_prompt_file=args.system_prompt,
            skip_closed=not args.no_skip_closed,
            enable_advise=not args.no_advise,
        )

        planner = Planner(options)
        results = planner.run()

        # Compute work units for loop convergence (#613): new plans
        successful = sum(1 for r in results.values() if r.success)
        already_planned = sum(1 for r in results.values() if r.plan_already_exists)
        work_units = max(0, successful - already_planned)
        write_work_report(work_units)

        failed = [num for num, result in results.items() if not result.success]
        if failed:
            log.error("Failed to plan %s issue(s): %s", len(failed), failed)
            if args.json:
                emit_json_status(1, issues=args.issues, failed=failed)
            return 1

        log.info("Planning complete")
        if args.json:
            emit_json_status(0, issues=args.issues, failed=[])
        return 0
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning("Interrupted by user")
        if args.json:
            emit_json_status(130, message="interrupted")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
