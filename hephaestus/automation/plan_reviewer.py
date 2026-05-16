"""Plan review automation: reads issue plans and posts review comments.

Provides:
- Parallel plan review across multiple issues
- Duplicate review detection (skips already-reviewed issues)
- Plan detection using the same markers as the planner
- Dry-run support with early return before any GitHub writes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import add_agent_argument, is_codex, run_codex_text
from hephaestus.github.rate_limit import wait_until

from .claude_invoke import scan_quota_reset
from .claude_models import reviewer_model
from .claude_timeouts import plan_reviewer_claude_timeout
from .git_utils import issue_ref
from .github_api import _gh_call, gh_issue_comment, gh_issue_json
from .models import PLAN_COMMENT_MARKERS, PlanReviewerOptions, WorkerResult
from .prompts import get_plan_review_prompt
from .status_tracker import StatusTracker

logger = logging.getLogger(__name__)

# Plan-comment markers live in models.PLAN_COMMENT_MARKERS — re-exported here
# under the existing private name so test files that imported it still work.
_PLAN_MARKERS = PLAN_COMMENT_MARKERS

# Prefix used by this reviewer when posting review comments.
_REVIEW_PREFIX = "## 🔍 Plan Review"


class PlanReviewer:
    """Reviews implementation plans posted to GitHub issues by the planner.

    Features:
    - Parallel review across multiple issues
    - Skips issues that already have a plan review comment
    - Skips issues that have no plan comment yet
    - Dry-run mode exits before any GitHub write operation
    """

    def __init__(self, options: PlanReviewerOptions) -> None:
        """Initialize the plan reviewer.

        Args:
            options: Plan reviewer configuration options.

        """
        self.options = options
        self.status_tracker = StatusTracker(options.max_workers)
        self.lock = threading.Lock()

    def run(self) -> dict[int, WorkerResult]:
        """Run the plan reviewer on all issues.

        Returns:
            Dictionary mapping issue number to WorkerResult.

        """
        logger.info(
            "Reviewing plans for %s issue(s) with %s parallel workers",
            len(self.options.issues),
            self.options.max_workers,
        )

        if not self.options.issues:
            logger.warning("No issues to review")
            return {}

        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}

            for idx, issue_num in enumerate(self.options.issues):
                future = executor.submit(self._review_issue, issue_num, idx)
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
                            logger.info("Issue #%s: plan review completed", issue_num)
                        else:
                            logger.error(
                                "Issue #%s: plan review failed: %s", issue_num, result.error
                            )
                    except Exception as e:
                        logger.error("Issue #%s raised exception: %s", issue_num, e)
                        with self.lock:
                            results[issue_num] = WorkerResult(
                                issue_number=issue_num,
                                success=False,
                                error=str(e),
                            )

        self._print_summary(results)
        return results

    def _review_issue(self, issue_number: int, slot_id: int) -> WorkerResult:
        """Review the plan for a single issue.

        Args:
            issue_number: GitHub issue number to review.
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
            self.status_tracker.update_slot(acquired_slot, f"{issue_ref(issue_number)}: checking")

            # --- Read-only checks (safe in dry-run) ---

            # Skip if already reviewed
            if self._has_existing_review(issue_number):
                logger.info("Issue #%s: already has a plan review, skipping", issue_number)
                return WorkerResult(issue_number=issue_number, success=True)

            # Skip if no plan exists
            plan_text = self._get_latest_plan(issue_number)
            if plan_text is None:
                logger.info("Issue #%s: no plan comment found, skipping", issue_number)
                return WorkerResult(issue_number=issue_number, success=True)

            # Fetch issue details for context
            self.status_tracker.update_slot(
                acquired_slot, f"{issue_ref(issue_number)}: fetching issue"
            )
            try:
                issue_data = gh_issue_json(issue_number)
            except Exception as e:
                return WorkerResult(
                    issue_number=issue_number,
                    success=False,
                    error=f"Failed to fetch issue: {e}",
                )

            issue_title: str = issue_data.get("title", f"Issue #{issue_number}")
            issue_body: str = issue_data.get("body", "")

            # Run Claude analysis
            self.status_tracker.update_slot(
                acquired_slot, f"{issue_ref(issue_number)}: running Claude"
            )
            review_text = self._run_claude_analysis(
                issue_number, issue_title, issue_body, plan_text
            )
            if review_text is None:
                return WorkerResult(
                    issue_number=issue_number,
                    success=False,
                    error="Claude analysis returned no output",
                )

            # --- DRY-RUN GUARD: no GitHub writes beyond this point ---
            if self.options.dry_run:
                logger.info(
                    "[DRY RUN] Would post plan review to issue #%s:\n%s\n%s...",
                    issue_number,
                    _REVIEW_PREFIX,
                    review_text[:200],
                )
                return WorkerResult(issue_number=issue_number, success=True)

            # Post review comment
            self.status_tracker.update_slot(
                acquired_slot, f"{issue_ref(issue_number)}: posting review"
            )
            self._post_review(issue_number, review_text)

            return WorkerResult(issue_number=issue_number, success=True)

        except Exception as e:
            logger.error("Issue #%s: unexpected error: %s", issue_number, e)
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e)[:80],
            )

        finally:
            self.status_tracker.release_slot(acquired_slot)

    def _fetch_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        """Fetch all comments for an issue, caching the result per instance.

        Both ``_has_existing_review`` and ``_get_latest_plan`` call this
        helper so the ``gh issue view --comments`` API is hit only once per
        issue per worker invocation (#A3-009).

        Args:
            issue_number: GitHub issue number.

        Returns:
            List of comment dicts (may be empty on error).

        """
        cache_attr = "_comments_cache"
        if not hasattr(self, cache_attr):
            object.__setattr__(self, cache_attr, {})
        cache: dict[int, list[dict[str, Any]]] = getattr(self, cache_attr)

        if issue_number in cache:
            return cache[issue_number]

        try:
            result = _gh_call(
                [
                    "issue",
                    "view",
                    str(issue_number),
                    "--comments",
                    "--json",
                    "comments",
                ],
            )
            data = json.loads(result.stdout)
            comments: list[dict[str, Any]] = data.get("comments", [])
        except Exception as e:
            logger.warning("Failed to fetch comments for issue #%s: %s", issue_number, e)
            comments = []

        cache[issue_number] = comments
        return comments

    def _get_latest_plan(self, issue_number: int) -> str | None:
        """Return the body of the last comment that looks like a plan.

        Uses :meth:`_fetch_issue_comments` so the API call is shared with
        :meth:`_has_existing_review`.

        Args:
            issue_number: GitHub issue number.

        Returns:
            Plan comment body text, or None if no plan comment is found.

        """
        comments = self._fetch_issue_comments(issue_number)

        # Walk in reverse to find the *last* plan comment
        for comment in reversed(comments):
            body: str = comment.get("body", "")
            if any(marker in body for marker in _PLAN_MARKERS):
                logger.debug("Found plan comment for issue #%s", issue_number)
                return body

        return None

    def _has_existing_review(self, issue_number: int) -> bool:
        """Check whether any comment is already a plan review.

        Uses :meth:`_fetch_issue_comments` so the API call is shared with
        :meth:`_get_latest_plan`.

        Args:
            issue_number: GitHub issue number.

        Returns:
            True if a review comment already exists.

        """
        comments = self._fetch_issue_comments(issue_number)

        for comment in comments:
            body: str = comment.get("body", "")
            if body.startswith(_REVIEW_PREFIX):
                logger.debug("Found existing review for issue #%s", issue_number)
                return True

        return False

    def _run_claude_analysis(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        plan_text: str,
        max_retries: int = 3,
    ) -> str | None:
        """Run Claude to produce a plan review.

        Calls ``claude --print`` with the review prompt piped to stdin.
        No filesystem tools are needed — the review is purely text-based.

        Why a retry loop: a 429 from the Claude CLI used to be silently
        swallowed (caught as generic Exception → None), so a single Anthropic
        outage corrupted the entire review phase. This now mirrors the
        planner's rate-limit handling — see :func:`scan_quota_reset` and
        :func:`wait_until`.

        Args:
            issue_number: GitHub issue number.
            issue_title: Issue title.
            issue_body: Issue body/description.
            plan_text: The full plan text to review.
            max_retries: Maximum retry attempts on rate-limit detection.

        Returns:
            Review text produced by Claude, or None on failure.

        """
        prompt = get_plan_review_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
        )

        if is_codex(self.options.agent):
            return self._run_codex_analysis(issue_number, prompt, max_retries=max_retries)

        env = os.environ.copy()
        # Avoid nested-session guard used by the planner / implementer
        env["CLAUDECODE"] = ""

        try:
            result = subprocess.run(
                [
                    "claude",
                    "--model",
                    reviewer_model(),
                    "--print",
                    "--output-format",
                    "text",
                    "--allowedTools",
                    "Read,Glob,Grep",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=plan_reviewer_claude_timeout(),
                env=env,
            )

            if result.returncode != 0:
                stderr = result.stderr or ""
                stdout = result.stdout or ""
                reset_epoch = scan_quota_reset(stderr, stdout)
                if reset_epoch is not None and max_retries > 0:
                    if reset_epoch > 0:
                        wait_until(reset_epoch)
                    else:
                        time.sleep(5)
                    return self._run_claude_analysis(
                        issue_number,
                        issue_title,
                        issue_body,
                        plan_text,
                        max_retries=max_retries - 1,
                    )

                logger.error(
                    "Claude returned exit code %s for issue #%s: %s",
                    result.returncode,
                    issue_number,
                    (stderr or stdout)[:200],
                )
                return None

            claude_output: str = (result.stdout or "").strip()
            if not claude_output:
                logger.error("Claude returned empty output for issue #%s", issue_number)
                return None

            return claude_output

        except subprocess.TimeoutExpired:
            logger.error("Claude timed out reviewing plan for issue #%s", issue_number)
            return None
        except FileNotFoundError:
            logger.error("'claude' CLI not found in PATH; cannot run plan review")
            return None
        except Exception as e:
            logger.error("Unexpected error calling Claude for issue #%s: %s", issue_number, e)
            return None

    def _run_codex_analysis(
        self,
        issue_number: int,
        prompt: str,
        max_retries: int = 3,
    ) -> str | None:
        """Run Codex to produce a plan review."""
        try:
            result = run_codex_text(
                prompt,
                cwd=Path.cwd(),
                timeout=plan_reviewer_claude_timeout(),
                sandbox="read-only",
            )
            output = (result.stdout or "").strip()
            if not output:
                logger.error("Codex returned empty output for issue #%s", issue_number)
                return None
            return output
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            stdout = e.stdout or ""
            reset_epoch = scan_quota_reset(stderr, stdout)
            if reset_epoch is not None and max_retries > 0:
                if reset_epoch > 0:
                    wait_until(reset_epoch)
                else:
                    time.sleep(5)
                return self._run_codex_analysis(
                    issue_number,
                    prompt,
                    max_retries=max_retries - 1,
                )
            logger.error(
                "Codex returned exit code %s for issue #%s: %s",
                e.returncode,
                issue_number,
                (stderr or stdout)[:200],
            )
            return None
        except subprocess.TimeoutExpired:
            logger.error("Codex timed out reviewing plan for issue #%s", issue_number)
            return None
        except FileNotFoundError:
            logger.error("'codex' CLI not found in PATH; cannot run plan review")
            return None
        except Exception as e:
            logger.error("Unexpected error calling Codex for issue #%s: %s", issue_number, e)
            return None

    def _post_review(self, issue_number: int, review_text: str) -> None:
        """Post the plan review as a comment on the issue.

        Args:
            issue_number: GitHub issue number.
            review_text: Review body text from Claude.

        """
        comment_body = f"{_REVIEW_PREFIX}\n\n{review_text}"
        gh_issue_comment(issue_number, comment_body)
        logger.info("Posted plan review to issue #%s", issue_number)

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print a summary of plan review results.

        Args:
            results: Mapping of issue number to WorkerResult.

        """
        total = len(results)
        successful = sum(1 for r in results.values() if r.success)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("Plan Review Summary")
        logger.info("=" * 60)
        logger.info("Total issues: %s", total)
        logger.info("Successful: %s", successful)
        logger.info("Failed: %s", failed)

        if failed > 0:
            logger.info("Failed issues:")
            for issue_num, result in results.items():
                if not result.success:
                    logger.info("  #%s: %s", issue_num, result.error)


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
    """Parse command line arguments for the plan reviewer CLI."""
    parser = argparse.ArgumentParser(
        description="Review implementation plans posted to GitHub issues using Claude",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Review plans for specific issues
  %(prog)s --issues 123 456 789

  # Dry run (no GitHub writes)
  %(prog)s --issues 123 --dry-run

  # Review with more workers
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
        help="Issue numbers whose plans should be reviewed",
    )
    add_agent_argument(parser)
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
        help=(
            "Suppress GitHub mutations (no review comments posted). NOTE: Claude "
            "is still invoked to analyse plans — dry-run still incurs full "
            "Claude token cost. It is for correctness rehearsal, not cost preview."
        ),
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
    """Execute the plan review workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt.

    """
    args = _parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)

    # Dedupe while preserving order — ``--issues 123 123`` would otherwise
    # post two reviews on the same issue.
    args.issues = list(dict.fromkeys(args.issues))

    log.info("Starting plan review for issues: %s", args.issues)

    try:
        options = PlanReviewerOptions(
            issues=args.issues,
            agent=args.agent,
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            enable_ui=not args.no_ui,
            verbose=args.verbose,
        )

        reviewer = PlanReviewer(options)
        results = reviewer.run()

        failed = [num for num, result in results.items() if not result.success]
        if failed:
            log.error("Failed to review %s plan(s) for issue(s): %s", len(failed), failed)
            return 1

        log.info("Plan review complete")
        return 0

    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
