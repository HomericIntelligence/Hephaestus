"""CI driver automation: polls CI checks and drives PRs to green.

Provides:
- Parallel CI check polling across multiple issues
- Automatic fix session on red required checks
- Auto-merge enablement when all required checks are green
- Dry-run support with early return before any GitHub write or git push
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from .git_utils import get_repo_root, run
from .github_api import _gh_call, gh_pr_checks
from .models import CIDriverOptions, WorkerResult
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


class CIDriver:
    """Drives open PRs toward green CI by fixing failures and enabling auto-merge.

    Features:
    - Parallel CI check polling across multiple issues
    - Distinguishes required vs non-required checks
    - Single fix iteration per failing PR (configurable via max_fix_iterations)
    - Enables auto-merge once all required checks are green
    - Dry-run mode exits before any write or push
    """

    def __init__(self, options: CIDriverOptions) -> None:
        """Initialize the CI driver.

        Args:
            options: CI driver configuration options.

        """
        self.options = options
        self.repo_root = get_repo_root()
        self.state_dir = self.repo_root / ".issue_implementer"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.worktree_manager = WorktreeManager()
        self.status_tracker = StatusTracker(options.max_workers)
        self.lock = threading.Lock()

    def run(self) -> dict[int, WorkerResult]:
        """Run the CI driver on all configured issues.

        Returns:
            Dictionary mapping issue number to WorkerResult.

        """
        logger.info(
            f"Starting CI driver for {len(self.options.issues)} issue(s) "
            f"with {self.options.max_workers} parallel workers"
        )

        if not self.options.issues:
            logger.warning("No issues to process")
            return {}

        # Pre-discover PRs — only submit workers for issues that have an open PR.
        # This prevents Claude from being launched for issues with no PR at all.
        pr_map = self._discover_prs(self.options.issues)
        if not pr_map:
            logger.warning("No open PRs found for the specified issues — nothing to drive")
            return {}

        logger.info(f"Found {len(pr_map)} PR(s) to drive to green: {pr_map}")

        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}

            for idx, (issue_num, pr_num) in enumerate(pr_map.items()):
                future = executor.submit(self._drive_issue, issue_num, pr_num, idx)
                futures[future] = issue_num

            while futures:
                try:
                    done, _pending = wait(futures.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
                except Exception:
                    time.sleep(0.1)
                    continue

                for future in done:
                    issue_num = futures.pop(future)
                    try:
                        result = future.result()
                        with self.lock:
                            results[issue_num] = result
                        if result.success:
                            logger.info(f"Issue #{issue_num}: CI drive completed")
                        else:
                            logger.error(f"Issue #{issue_num}: CI drive failed: {result.error}")
                    except Exception as e:
                        logger.error(f"Issue #{issue_num} raised exception: {e}")
                        with self.lock:
                            results[issue_num] = WorkerResult(
                                issue_number=issue_num,
                                success=False,
                                error=str(e),
                            )

        self._print_summary(results)
        return results

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:
        """Pre-discover open PRs for all issues.

        Args:
            issue_numbers: Issue numbers to check

        Returns:
            Mapping of issue_number -> pr_number for issues that have an open PR

        """
        pr_map: dict[int, int] = {}
        for issue_num in issue_numbers:
            pr_number = self._find_pr_for_issue(issue_num)
            if pr_number is not None:
                pr_map[issue_num] = pr_number
            else:
                logger.info(f"Issue #{issue_num}: no open PR found, skipping")
        return pr_map

    def _drive_issue(self, issue_number: int, pr_number: int, slot_id: int) -> WorkerResult:
        """Drive a single issue's PR toward green CI.

        The pr_number is pre-discovered by run() — no Claude agent is ever launched
        for issues that have no open PR.

        Args:
            issue_number: GitHub issue number.
            pr_number: Pre-discovered open PR number for this issue.
            slot_id: Worker slot ID for status tracking.

        Returns:
            WorkerResult indicating success or failure.

        """
        acquired_slot: int | None = self.status_tracker.acquire_slot()
        if acquired_slot is None:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error="Failed to acquire worker slot",
            )

        try:
            self.status_tracker.update_slot(acquired_slot, f"#{issue_number}: fetching checks")

            # 2. Get CI checks
            checks = gh_pr_checks(pr_number, dry_run=self.options.dry_run)
            if not checks:
                logger.info(f"Issue #{issue_number}: no CI checks found for PR #{pr_number}")
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            # 3. Classify: required vs non-required
            required_checks = [c for c in checks if c.get("required", False)]
            if not required_checks:
                # No required checks defined — treat ALL checks as required
                required_checks = checks

            # 4. Check if all required checks are green
            all_completed = all(c["status"] == "completed" for c in required_checks)
            all_green = all_completed and all(
                c.get("conclusion") in ("success", "skipped", "neutral") for c in required_checks
            )

            if all_green:
                self.status_tracker.update_slot(
                    acquired_slot, f"#{issue_number}: enabling auto-merge"
                )
                # DRY-RUN GUARD before auto-merge
                if self.options.dry_run:
                    logger.info(
                        f"[dry_run] Would enable auto-merge for PR #{pr_number} "
                        f"(issue #{issue_number})"
                    )
                    return WorkerResult(
                        issue_number=issue_number, success=True, pr_number=pr_number
                    )
                self._enable_auto_merge(pr_number)
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            # 5. Some required checks failed — check if any are still pending
            failing = [c for c in required_checks if c.get("conclusion") == "failure"]
            if not failing:
                # Checks still pending — not our job to wait here
                logger.info(
                    f"Issue #{issue_number}: PR #{pr_number} has pending CI checks, not yet failing"
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            # 6. Attempt fix iterations
            fix_result = self._attempt_ci_fixes(issue_number, pr_number, acquired_slot)
            if fix_result is not None:
                return fix_result

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                pr_number=pr_number,
                error=f"CI fix failed after {self.options.max_fix_iterations} attempt(s)",
            )

        except Exception as e:
            logger.error(f"Issue #{issue_number}: unexpected error: {e}")
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e)[:200],
            )

        finally:
            self.status_tracker.release_slot(acquired_slot)

    def _attempt_ci_fixes(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
    ) -> WorkerResult | None:
        """Attempt CI fix iterations for a failing PR.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            acquired_slot: Worker slot ID for status tracking.

        Returns:
            WorkerResult on success or dry-run, None if all iterations failed.

        """
        for iteration in range(self.options.max_fix_iterations):
            self.status_tracker.update_slot(
                acquired_slot,
                f"#{issue_number}: fetching CI logs (attempt {iteration + 1})",
            )
            ci_logs = self._get_failing_ci_logs(pr_number)
            session_id = self._load_impl_session_id(issue_number)
            worktree_path = self._get_worktree_path(issue_number, pr_number)

            if self.options.dry_run:
                logger.info(
                    f"[dry_run] Would run CI fix session for PR #{pr_number} "
                    f"(issue #{issue_number}, iteration {iteration + 1})"
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            self.status_tracker.update_slot(
                acquired_slot,
                f"#{issue_number}: running CI fix session (attempt {iteration + 1})",
            )
            fixed = self._run_ci_fix_session(
                issue_number, pr_number, worktree_path, ci_logs, session_id
            )
            if fixed:
                logger.info(
                    f"Issue #{issue_number}: CI fix applied successfully (attempt {iteration + 1})"
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            logger.warning(f"Issue #{issue_number}: CI fix attempt {iteration + 1} failed")

        return None

    def _find_pr_for_issue(self, issue_number: int) -> int | None:
        """Find the open PR for a single issue.

        Tries branch-name lookup first, then body search.

        Args:
            issue_number: GitHub issue number.

        Returns:
            PR number if found, None otherwise.

        """
        # Strategy 1: Look for branch named {issue}-auto-impl
        branch_name = f"{issue_number}-auto-impl"
        try:
            result = _gh_call(
                [
                    "pr",
                    "list",
                    "--head",
                    branch_name,
                    "--state",
                    "open",
                    "--json",
                    "number",
                    "--limit",
                    "1",
                ],
                check=False,
            )
            pr_data = json.loads(result.stdout or "[]")
            if pr_data:
                pr_number = pr_data[0]["number"]
                logger.info(f"Found PR #{pr_number} for issue #{issue_number} via branch name")
                return int(pr_number)
        except Exception as e:
            logger.debug(f"Branch-name lookup failed for issue #{issue_number}: {e}")

        # Strategy 2: Search PR body for issue reference
        try:
            result = _gh_call(
                [
                    "pr",
                    "list",
                    "--state",
                    "open",
                    "--search",
                    f"#{issue_number} in:body",
                    "--json",
                    "number",
                    "--limit",
                    "5",
                ],
                check=False,
            )
            pr_data = json.loads(result.stdout or "[]")
            if pr_data:
                pr_number = pr_data[0]["number"]
                logger.info(f"Found PR #{pr_number} for issue #{issue_number} via body search")
                return int(pr_number)
        except Exception as e:
            logger.debug(f"Body search failed for issue #{issue_number}: {e}")

        return None

    def _get_pr_branch(self, pr_number: int) -> str:
        """Get the head branch name of a PR.

        Args:
            pr_number: GitHub PR number.

        Returns:
            Branch name string.

        """
        try:
            result = _gh_call(
                ["pr", "view", str(pr_number), "--json", "headRefName"],
                check=False,
            )
            data = json.loads(result.stdout or "{}")
            branch: str = data.get("headRefName", f"pr-{pr_number}")
            return branch
        except Exception as e:
            logger.warning(f"Could not fetch branch for PR #{pr_number}: {e}")
            return f"pr-{pr_number}"

    def _get_worktree_path(self, issue_number: int, pr_number: int) -> Path:
        """Resolve the worktree path for a given issue/PR.

        Tries review state first, then creates a new worktree if needed.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.

        Returns:
            Path to the worktree directory.

        """
        review_state_file = self.state_dir / f"review-{issue_number}.json"
        if review_state_file.exists():
            try:
                data = json.loads(review_state_file.read_text())
                if data.get("worktree_path"):
                    wt = Path(data["worktree_path"])
                    if wt.exists():
                        return wt
            except Exception as e:
                logger.debug(f"Could not read review state for issue #{issue_number}: {e}")

        # Fallback: create a new worktree for the PR head branch
        branch = self._get_pr_branch(pr_number)
        return self.worktree_manager.create_worktree(issue_number, branch)

    def _get_failing_ci_logs(self, pr_number: int) -> str:
        """Fetch combined failure logs for recent failed CI runs on a PR.

        Args:
            pr_number: GitHub PR number.

        Returns:
            Combined log string, truncated to 10 000 characters.

        """
        try:
            result2 = _gh_call(
                [
                    "run",
                    "list",
                    "--limit",
                    "10",
                    "--json",
                    "databaseId,conclusion,name,headSha",
                ],
                check=False,
            )
            runs: list[dict[str, Any]] = json.loads(result2.stdout or "[]")
            failed_runs = [r for r in runs if r.get("conclusion") == "failure"][:3]

            logs: list[str] = []
            for run_info in failed_runs:
                run_id = run_info.get("databaseId")
                run_name = run_info.get("name", str(run_id))
                if not run_id:
                    continue
                try:
                    log_result = _gh_call(
                        ["run", "view", str(run_id), "--log-failed"],
                        check=False,
                    )
                    logs.append(f"=== {run_name} ===\n{log_result.stdout[:3000]}")
                except Exception as log_err:
                    logger.debug(f"Could not fetch log for run {run_id}: {log_err}")

            return "\n\n".join(logs)[:10000]

        except Exception as e:
            logger.warning(f"Could not fetch CI logs for PR #{pr_number}: {e}")
            return ""

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Load the Claude session ID from the implementer's saved state.

        Args:
            issue_number: GitHub issue number.

        Returns:
            Session ID string, or None if not found.

        """
        state_file = self.state_dir / f"state-{issue_number}.json"
        if not state_file.exists():
            logger.debug(f"No implementer state file for issue #{issue_number}")
            return None

        try:
            data = json.loads(state_file.read_text())
            session_id: str | None = data.get("session_id")
            if session_id:
                logger.debug(f"Loaded session_id for issue #{issue_number}: {session_id[:8]}...")
            return session_id
        except Exception as e:
            logger.warning(f"Could not load session_id for issue #{issue_number}: {e}")
            return None

    def _run_ci_fix_session(
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        ci_logs: str,
        session_id: str | None,
    ) -> bool:
        """Invoke Claude to fix CI failures, then push the result.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            worktree_path: Path to the checked-out worktree.
            ci_logs: Combined CI failure log text.
            session_id: Optional Claude session ID to resume.

        Returns:
            True if the fix session succeeded and the branch was pushed.

        """
        prompt = (
            f"Fix the CI failures for PR #{pr_number} (issue #{issue_number}).\n\n"
            f"Working directory: {worktree_path}\n\n"
            f"CI failure logs:\n{ci_logs}\n\n"
            "Fix the code to make the CI checks pass. After fixing:\n"
            "1. Run: pixi run python -m pytest tests/ -v\n"
            "2. Run: pre-commit run --all-files\n"
            "3. Commit changes (do NOT push)\n\n"
            f"Commit message: fix: Address CI failures for PR #{pr_number}\n"
        )

        prompt_file_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, dir=worktree_path
            ) as f:
                f.write(prompt)
                prompt_file_path = Path(f.name)

            base_cmd = [
                "claude",
                "--print",
                "--output-format",
                "json",
                "--allowedTools",
                "Read,Write,Edit,Glob,Grep,Bash",
                "--dangerously-skip-permissions",
            ]

            if session_id:
                cmd = [
                    "claude",
                    "--resume",
                    session_id,
                    "--print",
                    "--output-format",
                    "json",
                    "--allowedTools",
                    "Read,Write,Edit,Glob,Grep,Bash",
                    "--dangerously-skip-permissions",
                ]
            else:
                cmd = base_cmd

            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=worktree_path,
                timeout=1800,
            )

            # If --resume failed, retry without it
            if result.returncode != 0 and session_id:
                logger.warning(
                    f"Issue #{issue_number}: --resume session failed, retrying without it"
                )
                result = subprocess.run(
                    base_cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    cwd=worktree_path,
                    timeout=1800,
                )

            if result.returncode == 0:
                # Push the fixes
                try:
                    run(["git", "push", "origin", "HEAD"], cwd=worktree_path)
                    logger.info(f"Issue #{issue_number}: pushed CI fixes for PR #{pr_number}")
                    return True
                except Exception as push_err:
                    logger.error(f"Issue #{issue_number}: git push failed after CI fix: {push_err}")
                    return False

            logger.error(
                f"Issue #{issue_number}: Claude CI fix session returned exit code "
                f"{result.returncode}: {result.stderr[:300]}"
            )
            return False

        except subprocess.TimeoutExpired:
            logger.error(
                f"Issue #{issue_number}: Claude CI fix session timed out for PR #{pr_number}"
            )
            return False
        except Exception as e:
            logger.error(f"Issue #{issue_number}: CI fix session failed for PR #{pr_number}: {e}")
            return False
        finally:
            if prompt_file_path is not None:
                with contextlib.suppress(Exception):
                    prompt_file_path.unlink()

    def _enable_auto_merge(self, pr_number: int) -> None:
        """Enable auto-merge for the given PR using rebase strategy.

        Args:
            pr_number: GitHub PR number.

        """
        try:
            _gh_call(["pr", "merge", str(pr_number), "--auto", "--rebase"])
            logger.info(f"Enabled auto-merge for PR #{pr_number}")
        except Exception as e:
            logger.warning(f"Could not enable auto-merge for PR #{pr_number}: {e}")

    def _parse_json_block(self, text: str) -> dict[str, Any]:
        """Extract and parse the first JSON block from a text string.

        Args:
            text: Input text that may contain a JSON block.

        Returns:
            Parsed dictionary, or empty dict if no valid JSON found.

        """
        import re

        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            with contextlib.suppress(json.JSONDecodeError):
                return dict(json.loads(match.group(1)))

        # Try raw JSON
        with contextlib.suppress(json.JSONDecodeError):
            return dict(json.loads(text))

        return {}

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print a summary of CI drive results.

        Args:
            results: Mapping of issue number to WorkerResult.

        """
        total = len(results)
        successful = sum(1 for r in results.values() if r.success)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("CI Driver Summary")
        logger.info("=" * 60)
        logger.info(f"Total issues: {total}")
        logger.info(f"Successful: {successful}")
        logger.info(f"Failed: {failed}")

        if failed > 0:
            logger.info("Failed issues:")
            for issue_num, result in results.items():
                if not result.success:
                    logger.info(f"  #{issue_num}: {result.error}")


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging.

    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments for the CI driver CLI."""
    parser = argparse.ArgumentParser(
        description="Drive PRs to green CI: fix failures and enable auto-merge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Drive CI for specific issues
  %(prog)s --issues 123 456 789

  # Dry run (no GitHub writes or git pushes)
  %(prog)s --issues 123 --dry-run

  # Run with more workers
  %(prog)s --issues 123 456 --max-workers 5

  # Verbose output
  %(prog)s --issues 123 -v
        """,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        required=True,
        help="Issue numbers whose PRs should be driven to green CI",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=3,
        choices=range(1, 33),
        metavar="N",
        help="Maximum number of parallel workers, 1-32 (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without any GitHub writes or git pushes",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable curses UI (use plain logging instead)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    return parser.parse_args()


def main() -> int:
    """Execute the CI driver workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt.

    """
    args = _parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)
    log.info(f"Starting CI driver for issues: {args.issues}")

    try:
        options = CIDriverOptions(
            issues=args.issues,
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            enable_ui=not args.no_ui,
            verbose=args.verbose,
        )

        driver = CIDriver(options)
        results = driver.run()

        failed = [num for num, result in results.items() if not result.success]
        if failed:
            log.error(f"CI drive failed for {len(failed)} issue(s): {failed}")
            return 1

        log.info("CI driver complete")
        return 0

    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
