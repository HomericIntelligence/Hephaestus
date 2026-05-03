"""Read-only PR review automation using Claude Code in parallel worktrees.

Provides:
- Parallel PR analysis across multiple issues
- Read-only two-phase workflow: analysis then inline comment posting
- Git worktree isolation per PR (for code reading only)
- State persistence and UI monitoring

This module does NOT commit, push, or fix code. Fixing is handled by
address_review.py in a separate phase.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .curses_ui import CursesUI, ThreadLogManager
from .git_utils import get_repo_root, run
from .github_api import _gh_call, fetch_issue_info, gh_pr_review_post, write_secure
from .models import ReviewerOptions, ReviewPhase, ReviewState, WorkerResult
from .prompts import get_pr_review_analysis_prompt
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


def _parse_json_block(text: str) -> dict[str, Any]:
    """Extract the last ```json ... ``` block from Claude's response.

    Args:
        text: Claude's full response text

    Returns:
        Parsed dict with keys "comments" and "summary", or defaults if not found

    """
    matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not matches:
        return {"comments": [], "summary": "No structured output from analysis"}
    try:
        return dict(json.loads(matches[-1]))
    except json.JSONDecodeError:
        return {"comments": [], "summary": "Failed to parse structured output from analysis"}


class PRReviewer:
    """Posts inline review comments on open PRs linked to specified issues.

    Features:
    - Parallel PR analysis in isolated git worktrees (read-only)
    - Two-phase workflow: analysis session then inline comment posting
    - State persistence for observability
    - Real-time curses UI for status monitoring

    This class does NOT commit, push, or fix code.
    """

    def __init__(self, options: ReviewerOptions):
        """Initialize PR reviewer.

        Args:
            options: Reviewer configuration options

        """
        self.options = options
        self.repo_root = get_repo_root()
        self.state_dir = self.repo_root / ".issue_implementer"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.worktree_manager = WorktreeManager()
        self.status_tracker = StatusTracker(options.max_workers)
        self.log_manager = ThreadLogManager()

        self.states: dict[int, ReviewState] = {}
        self.state_lock = threading.Lock()

        self.ui: CursesUI | None = None

    def _log(self, level: str, msg: str, thread_id: int | None = None) -> None:
        """Log to both standard logger and UI thread buffer.

        Args:
            level: Log level ("error", "warning", or "info")
            msg: Message to log
            thread_id: Thread ID (defaults to current thread)

        """
        getattr(logger, level)(msg)
        tid = thread_id or threading.get_ident()
        prefix = {"error": "ERROR", "warning": "WARN", "info": ""}.get(level, "")
        ui_msg = f"{prefix}: {msg}" if prefix else msg
        self.log_manager.log(tid, ui_msg)

    def run(self) -> dict[int, WorkerResult]:
        """Run the PR reviewer.

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        logger.info(f"Starting PR review for issues: {self.options.issues}")

        # Discover PRs for all issues
        pr_map = self._discover_prs(self.options.issues)

        if not pr_map:
            logger.warning("No open PRs found for the specified issues")
            return {}

        logger.info(f"Found {len(pr_map)} PR(s) to review: {pr_map}")

        # Start UI if enabled
        if not self.options.dry_run and self.options.enable_ui:
            self.ui = CursesUI(self.status_tracker, self.log_manager)
            self.ui.start()

        try:
            results = self._review_all(pr_map)
            return results
        finally:
            if self.ui:
                self.ui.stop()
            if not self.options.dry_run:
                self.worktree_manager.cleanup_all()

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:
        """Find open PRs linked to the given issue numbers.

        First tries branch name lookup ({issue}-auto-impl), then falls back
        to searching the PR body for the issue reference.

        Args:
            issue_numbers: List of issue numbers to find PRs for

        Returns:
            Mapping of issue_number -> pr_number for found PRs

        """
        pr_map: dict[int, int] = {}

        for issue_num in issue_numbers:
            pr_number = self._find_pr_for_issue(issue_num)
            if pr_number is not None:
                pr_map[issue_num] = pr_number
            else:
                logger.warning(f"No open PR found for issue #{issue_num}")

        return pr_map

    def _find_pr_for_issue(self, issue_number: int) -> int | None:
        """Find the open PR for a single issue.

        Args:
            issue_number: GitHub issue number

        Returns:
            PR number if found, None otherwise

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

    def _gather_pr_context(
        self,
        pr_number: int,
        issue_number: int,
        worktree_path: Path,
    ) -> dict[str, str]:
        """Gather all context needed for PR analysis.

        Fetches diff, CI status, existing comments, and issue body.

        Args:
            pr_number: GitHub PR number
            issue_number: Linked GitHub issue number
            worktree_path: Path to worktree (for cwd)

        Returns:
            Dictionary with keys: pr_diff, issue_body, ci_status,
            review_comments, pr_description

        """
        context: dict[str, str] = {
            "pr_diff": "",
            "issue_body": "",
            "ci_status": "",
            "review_comments": "",
            "pr_description": "",
        }

        # Fetch PR diff
        with contextlib.suppress(Exception):
            result = _gh_call(["pr", "diff", str(pr_number)], check=False)
            context["pr_diff"] = (result.stdout or "")[:8000]  # Cap to avoid huge diffs

        # Fetch PR description and reviews/comments
        with contextlib.suppress(Exception):
            result = _gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "body,reviews,comments",
                ],
            )
            pr_data = json.loads(result.stdout or "{}")
            context["pr_description"] = pr_data.get("body", "")

            # Aggregate review comments
            review_parts: list[str] = []
            for review in pr_data.get("reviews", []):
                state = review.get("state", "")
                author = review.get("author", {}).get("login", "unknown")
                body = review.get("body", "")
                if body:
                    review_parts.append(f"[{state}] @{author}: {body}")
            for comment in pr_data.get("comments", []):
                author = comment.get("author", {}).get("login", "unknown")
                body = comment.get("body", "")
                if body:
                    review_parts.append(f"@{author}: {body}")
            context["review_comments"] = "\n".join(review_parts)

        # Fetch CI check status
        with contextlib.suppress(Exception):
            result = _gh_call(
                ["pr", "checks", str(pr_number), "--json", "name,state,conclusion"],
                check=False,
            )
            checks = json.loads(result.stdout or "[]")
            status_lines = [
                f"{c.get('name', '?')}: {c.get('conclusion') or c.get('state', '?')}"
                for c in checks
            ]
            context["ci_status"] = "\n".join(status_lines)

        # Fetch issue body
        with contextlib.suppress(Exception):
            issue = fetch_issue_info(issue_number)
            context["issue_body"] = issue.body

        return context

    def _run_analysis_session(
        self,
        pr_number: int,
        issue_number: int,
        worktree_path: Path,
        context: dict[str, str],
        slot_id: int | None = None,
    ) -> dict[str, Any]:
        """Run the read-only Claude analysis session to generate inline review comments.

        Args:
            pr_number: GitHub PR number
            issue_number: Linked issue number
            worktree_path: Path to worktree
            context: PR context from _gather_pr_context
            slot_id: Worker slot ID for status updates

        Returns:
            Parsed analysis result dict with keys "comments" and "summary"

        """
        if self.options.dry_run:
            logger.info(f"[DRY RUN] Would run analysis session for PR #{pr_number}")
            return {"comments": [], "summary": "[DRY RUN] analysis skipped"}

        prompt = get_pr_review_analysis_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            pr_diff=context.get("pr_diff", ""),
            issue_body=context.get("issue_body", ""),
            ci_status=context.get("ci_status", ""),
            pr_description=context.get("pr_description", ""),
        )

        prompt_file = worktree_path / f".claude-pr-review-{issue_number}.md"
        prompt_file.write_text(prompt)

        log_file = self.state_dir / f"pr-review-analysis-{issue_number}.log"

        try:
            result = run(
                [
                    "claude",
                    str(prompt_file),
                    "--output-format",
                    "json",
                    "--permission-mode",
                    "dontAsk",
                    "--allowedTools",
                    "Read,Glob,Grep,Bash",
                ],
                cwd=worktree_path,
                timeout=1200,  # 20 minutes
            )
            log_file.write_text(result.stdout or "")

            # Extract the response text from Claude's JSON wrapper
            try:
                data = json.loads(result.stdout or "{}")
                response_text: str = data.get("result", result.stdout or "")
            except (json.JSONDecodeError, AttributeError):
                response_text = result.stdout or ""

            parsed = _parse_json_block(response_text)
            logger.info(
                f"Analysis complete for PR #{pr_number}; "
                f"found {len(parsed.get('comments', []))} inline comment(s)"
            )
            return parsed

        except subprocess.CalledProcessError as e:
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            error_output = f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            log_file.write_text(error_output)
            raise RuntimeError(
                f"Analysis session failed for PR #{pr_number}: {e.stderr or e.stdout}"
            ) from e
        except subprocess.TimeoutExpired as e:
            log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
            raise RuntimeError(f"Analysis session timed out for PR #{pr_number}") from e
        finally:
            with contextlib.suppress(Exception):
                prompt_file.unlink()

    def _save_state(self, state: ReviewState) -> None:
        """Save review state to disk.

        Args:
            state: ReviewState to persist

        """
        state_file = self.state_dir / f"review-{state.issue_number}.json"
        write_secure(state_file, state.model_dump_json(indent=2))

    def _get_or_create_state(self, issue_number: int, pr_number: int) -> ReviewState:
        """Get or create review state for an issue.

        Args:
            issue_number: GitHub issue number
            pr_number: GitHub PR number

        Returns:
            Existing or new ReviewState

        """
        with self.state_lock:
            if issue_number not in self.states:
                self.states[issue_number] = ReviewState(
                    issue_number=issue_number,
                    pr_number=pr_number,
                )
            return self.states[issue_number]

    def _fail_review(
        self,
        issue_number: int,
        error_msg: str,
        slot_id: int,
    ) -> WorkerResult:
        """Record a review failure, update state and tracker, and return a failed WorkerResult.

        Args:
            issue_number: GitHub issue number
            error_msg: Human-readable error description
            slot_id: Worker slot ID for status updates

        Returns:
            WorkerResult with success=False

        """
        self.status_tracker.update_slot(slot_id, f"#{issue_number}: FAILED - {error_msg[:50]}")
        err_state = self.states.get(issue_number)
        if err_state:
            with self.state_lock:
                err_state.phase = ReviewPhase.FAILED
                err_state.error = error_msg
            self._save_state(err_state)
        return WorkerResult(issue_number=issue_number, success=False, error=error_msg)

    def _review_pr(self, issue_number: int, pr_number: int) -> WorkerResult:
        """Analyze and post inline review comments for a single PR.

        Flow: ANALYZING -> POSTING -> COMPLETED (or FAILED at any step)

        Args:
            issue_number: GitHub issue number
            pr_number: GitHub PR number

        Returns:
            WorkerResult

        """
        slot_id = self.status_tracker.acquire_slot()
        if slot_id is None:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error="Failed to acquire worker slot",
            )

        thread_id = threading.get_ident()

        try:
            self.status_tracker.update_slot(
                slot_id, f"#{issue_number}: PR #{pr_number} Creating worktree"
            )
            self._log(
                "info", f"Starting review of PR #{pr_number} for issue #{issue_number}", thread_id
            )

            state = self._get_or_create_state(issue_number, pr_number)

            # Create worktree on the PR branch (read-only usage)
            branch_name = f"{issue_number}-auto-impl"
            worktree_path = self.worktree_manager.create_worktree(issue_number, branch_name)

            with self.state_lock:
                state.worktree_path = str(worktree_path)
                state.branch_name = branch_name
            self._save_state(state)

            # Gather context
            self.status_tracker.update_slot(
                slot_id, f"#{issue_number}: PR #{pr_number} Gathering context"
            )
            context = self._gather_pr_context(pr_number, issue_number, worktree_path)

            # Phase: ANALYZING — run Claude read-only analysis
            self.status_tracker.update_slot(slot_id, f"#{issue_number}: PR #{pr_number} Analyzing")
            with self.state_lock:
                state.phase = ReviewPhase.ANALYZING
            self._save_state(state)

            analysis = self._run_analysis_session(
                pr_number, issue_number, worktree_path, context, slot_id
            )

            comments: list[dict[str, Any]] = analysis.get("comments", [])
            summary: str = analysis.get("summary", "")

            # Phase: POSTING — post inline review comments to GitHub
            self.status_tracker.update_slot(slot_id, f"#{issue_number}: PR #{pr_number} Posting")
            with self.state_lock:
                state.phase = ReviewPhase.POSTING
            self._save_state(state)

            if self.options.dry_run:
                self._log(
                    "info",
                    f"[DRY RUN] Would post {len(comments)} inline comment(s) on PR #{pr_number}",
                    thread_id,
                )
                thread_ids: list[str] = []
            else:
                thread_ids = gh_pr_review_post(
                    pr_number=pr_number,
                    comments=comments,
                    summary=summary,
                    dry_run=False,
                )
                self._log(
                    "info",
                    f"Posted {len(thread_ids)} review thread(s) on PR #{pr_number}",
                    thread_id,
                )

            with self.state_lock:
                state.posted_thread_ids = thread_ids
                state.phase = ReviewPhase.COMPLETED
                state.completed_at = datetime.now(timezone.utc)
            self._save_state(state)

            self._log(
                "info", f"PR #{pr_number} review complete for issue #{issue_number}", thread_id
            )

            return WorkerResult(
                issue_number=issue_number,
                success=True,
                pr_number=pr_number,
                branch_name=branch_name,
                worktree_path=str(worktree_path),
            )

        except subprocess.TimeoutExpired as e:
            error_msg = f"Timeout: {' '.join(str(c) for c in e.cmd[:3])} exceeded {e.timeout}s"
            self._log("error", error_msg, thread_id)
            return self._fail_review(issue_number, error_msg, slot_id)

        except subprocess.CalledProcessError as e:
            error_msg = (
                f"Command failed (exit {e.returncode}): {' '.join(str(c) for c in e.cmd[:3])}"
            )
            self._log("error", error_msg, thread_id)
            return self._fail_review(issue_number, error_msg, slot_id)

        except RuntimeError as e:
            self._log("error", f"Runtime error: {e}", thread_id)
            return self._fail_review(issue_number, str(e)[:80], slot_id)

        except Exception as e:
            self._log("error", f"Unexpected {type(e).__name__}: {e}", thread_id)
            return self._fail_review(issue_number, str(e)[:80], slot_id)

        finally:
            time.sleep(1)
            self.status_tracker.release_slot(slot_id)

    def _review_all(self, pr_map: dict[int, int]) -> dict[int, WorkerResult]:
        """Review all PRs in parallel.

        Args:
            pr_map: Mapping of issue_number -> pr_number

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}

            # Submit all PRs upfront (no dependency ordering needed for review)
            for issue_num, pr_num in pr_map.items():
                future = executor.submit(self._review_pr, issue_num, pr_num)
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
                        results[issue_num] = result
                        if result.success:
                            logger.info(f"Issue #{issue_num} PR review completed")
                        else:
                            logger.error(f"Issue #{issue_num} PR review failed: {result.error}")
                    except Exception as e:
                        logger.error(f"Issue #{issue_num} raised exception: {e}")
                        results[issue_num] = WorkerResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

        self._print_summary(results)
        return results

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print review summary.

        Args:
            results: Mapping of issue number to WorkerResult

        """
        total = len(results)
        successful = sum(1 for r in results.values() if r.success)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("PR Review Summary")
        logger.info("=" * 60)
        logger.info(f"Total PRs: {total}")
        logger.info(f"Successful: {successful}")
        logger.info(f"Failed: {failed}")

        if failed > 0:
            logger.info("\nFailed issues:")
            for issue_num, result in results.items():
                if not result.success:
                    logger.info(f"  #{issue_num}: {result.error}")


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
    """Parse command line arguments for the reviewer CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Analyze open PRs linked to GitHub issues using Claude Code "
            "and post inline review comments (read-only — does not fix code)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Review PRs for specific issues
  %(prog)s --issues 595 596

  # Review with dry run
  %(prog)s --issues 595 --dry-run

  # Review with more workers
  %(prog)s --issues 595 596 --max-workers 5
        """,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        required=True,
        help="Issue numbers whose linked PRs should be reviewed",
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
        help="Show what would be done without actually posting any review comments",
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
    """Execute the PR review workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt

    """
    args = _parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)
    log.info(f"Starting PR review for issues: {args.issues}")

    from hephaestus.automation.models import ReviewerOptions
    from hephaestus.utils.terminal import terminal_guard

    options = ReviewerOptions(
        issues=args.issues,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        enable_ui=not args.no_ui,
    )

    with terminal_guard():
        try:
            reviewer = PRReviewer(options)
            results = reviewer.run()

            failed = [num for num, result in results.items() if not result.success]
            if failed:
                log.error(f"Failed to review {len(failed)} PR(s) for issue(s): {failed}")
                return 1

            log.info("PR review complete")
            return 0
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
