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
import os
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    add_agent_argument,
    is_codex,
    resume_codex_session,
    run_codex_session,
    session_agent_matches,
)

from .claude_models import implementer_model
from .claude_timeouts import ci_driver_claude_timeout
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
    - Up to ``max_fix_iterations`` fix attempts per failing PR (default 1)
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

    def run(self) -> dict[int, WorkerResult]:  # noqa: C901  # thread pool + finally + preserve report
        """Run the CI driver on all configured issues.

        Returns:
            Dictionary mapping issue number to WorkerResult.

        """
        logger.info(
            "Starting CI driver for %s issue(s) with %s parallel workers",
            len(self.options.issues),
            self.options.max_workers,
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

        logger.info("Found %s PR(s) to drive to green: %s", len(pr_map), pr_map)

        results: dict[int, WorkerResult] = {}

        try:
            with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
                futures: dict[Future[Any], int] = {}

                for idx, (issue_num, pr_num) in enumerate(pr_map.items()):
                    future = executor.submit(self._drive_issue, issue_num, pr_num, idx)
                    futures[future] = issue_num

                while futures:
                    try:
                        done, _pending = wait(
                            futures.keys(), timeout=1.0, return_when=FIRST_COMPLETED
                        )
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
                                logger.info("Issue #%s: CI drive completed", issue_num)
                            else:
                                logger.error(
                                    "Issue #%s: CI drive failed: %s", issue_num, result.error
                                )
                        except Exception as e:
                            logger.error("Issue #%s raised exception: %s", issue_num, e)
                            with self.lock:
                                results[issue_num] = WorkerResult(
                                    issue_number=issue_num,
                                    success=False,
                                    error=str(e),
                                )
        finally:
            # Always clean up worktrees, even on KeyboardInterrupt or exception.
            # Mirror the pattern from implementer.py:178-185.
            if not self.options.dry_run:
                try:
                    self.worktree_manager.cleanup_all()
                except Exception:
                    logger.exception("Error during worktree cleanup in CIDriver.run()")

            # Report any worktrees that were preserved due to uncommitted changes.
            preserved = self.worktree_manager.preserved
            if preserved:
                logger.info("Preserved worktrees (contain uncommitted changes):")
                for issue_num, path in preserved:
                    logger.info("  #%d: %s", issue_num, path)
                logger.info("Inspect or discard them with: git worktree remove --force <path>")

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
                logger.info("Issue #%s: no open PR found, skipping", issue_num)
        return pr_map

    def _drive_issue(  # noqa: C901  # poll loop + required-check classification + fix path
        self, issue_number: int, pr_number: int, slot_id: int
    ) -> WorkerResult:
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

        # Maximum wall-clock seconds to poll for pending CI checks before giving up.
        _ci_poll_max_wait: int = int(os.environ.get("HEPH_CI_POLL_MAX_WAIT", "600"))

        try:
            self.status_tracker.update_slot(acquired_slot, f"#{issue_number}: fetching checks")

            # 2. Get CI checks — bounded poll loop for pending state.
            # The module docstring advertises "Parallel CI check polling" but the
            # original code returned success=True immediately for pending checks,
            # which meant stalled PRs were silently declared complete.  We now
            # wait up to HEPH_CI_POLL_MAX_WAIT seconds (default 600 s) using
            # exponential backoff before giving up.
            poll_elapsed = 0
            poll_attempt = 0
            checks: list[dict[str, Any]] = []
            required_checks: list[dict[str, Any]] = []
            all_green = False
            failing: list[dict[str, Any]] = []

            while True:
                checks = gh_pr_checks(pr_number, dry_run=self.options.dry_run)
                if not checks:
                    logger.info("Issue #%s: no CI checks found for PR #%s", issue_number, pr_number)
                    return WorkerResult(
                        issue_number=issue_number, success=True, pr_number=pr_number
                    )

                # 3. Classify: required vs non-required
                required_checks = [c for c in checks if c.get("required", False)]
                if not required_checks:
                    # No required checks defined — treat ALL checks as required
                    required_checks = checks

                # 4. Check if all required checks have a definitive conclusion.
                # "queued" / "in_progress" / "waiting" / "requested" are pending states.
                all_concluded = all(c["status"] == "completed" for c in required_checks)

                if all_concluded:
                    # All checks have a conclusion; evaluate pass/fail below.
                    break

                # At least one check is still pending.
                sleep_secs = min(2**poll_attempt, 60)
                if poll_elapsed + sleep_secs > _ci_poll_max_wait:
                    logger.warning(
                        "Issue #%s: CI checks still pending after %ss (limit %ss), "
                        "treating as not yet failing",
                        issue_number,
                        poll_elapsed,
                        _ci_poll_max_wait,
                    )
                    return WorkerResult(
                        issue_number=issue_number, success=True, pr_number=pr_number
                    )

                self.status_tracker.update_slot(
                    acquired_slot,
                    f"#{issue_number}: waiting for CI checks (attempt {poll_attempt + 1}, "
                    f"{poll_elapsed}s elapsed)",
                )
                logger.debug(
                    "Issue #%s: CI checks pending, sleeping %ss (attempt %s, %ss elapsed)",
                    issue_number,
                    sleep_secs,
                    poll_attempt + 1,
                    poll_elapsed,
                )
                time.sleep(sleep_secs)
                poll_elapsed += sleep_secs
                poll_attempt += 1

            all_green = all(
                c.get("conclusion") in ("success", "skipped", "neutral") for c in required_checks
            )

            if all_green:
                self.status_tracker.update_slot(
                    acquired_slot, f"#{issue_number}: enabling auto-merge"
                )
                # DRY-RUN GUARD before auto-merge
                if self.options.dry_run:
                    logger.info(
                        "[dry_run] Would enable auto-merge for PR #%s (issue #%s)",
                        pr_number,
                        issue_number,
                    )
                    return WorkerResult(
                        issue_number=issue_number, success=True, pr_number=pr_number
                    )
                merge_ok = self._enable_auto_merge(pr_number)
                return WorkerResult(
                    issue_number=issue_number,
                    success=merge_ok,
                    pr_number=pr_number,
                    error=None if merge_ok else f"auto-merge failed for PR #{pr_number}",
                )

            # 5. Some required checks failed
            failing = [c for c in required_checks if c.get("conclusion") == "failure"]
            if not failing:
                # All concluded but none are green and none are "failure" —
                # e.g. all cancelled.  Nothing for us to fix.
                logger.info(
                    "Issue #%s: PR #%s checks concluded with non-green, non-failure conclusions "
                    "(e.g. cancelled)",
                    issue_number,
                    pr_number,
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
            logger.error("Issue #%s: unexpected error: %s", issue_number, e)
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
                    "[dry_run] Would run CI fix session for PR #%s (issue #%s, iteration %s)",
                    pr_number,
                    issue_number,
                    iteration + 1,
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
                    "Issue #%s: CI fix applied successfully (attempt %s)",
                    issue_number,
                    iteration + 1,
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            logger.warning("Issue #%s: CI fix attempt %s failed", issue_number, iteration + 1)

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
                logger.info("Found PR #%s for issue #%s via branch name", pr_number, issue_number)
                return int(pr_number)
        except Exception as e:
            logger.debug("Branch-name lookup failed for issue #%s: %s", issue_number, e)

        # Strategy 2: Search PR body for issue reference using the canonical
        # "Closes #N" pattern so we don't accidentally match a PR that merely
        # mentions the issue number in passing (e.g. "related to #123").
        try:
            result = _gh_call(
                [
                    "pr",
                    "list",
                    "--state",
                    "open",
                    "--search",
                    f"Closes #{issue_number} in:body",
                    "--json",
                    "number,title",
                    "--limit",
                    "5",
                ],
                check=False,
            )
            pr_data = json.loads(result.stdout or "[]")
            if pr_data:
                pr_number = pr_data[0]["number"]
                logger.info(
                    "Found PR #%s for issue #%s via body search (title: %r)",
                    pr_number,
                    issue_number,
                    pr_data[0].get("title", ""),
                )
                return int(pr_number)
        except Exception as e:
            logger.debug("Body search failed for issue #%s: %s", issue_number, e)

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
            logger.warning("Could not fetch branch for PR #%s: %s", pr_number, e)
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
                logger.debug("Could not read review state for issue #%s: %s", issue_number, e)

        # Fallback: create a new worktree for the PR head branch
        branch = self._get_pr_branch(pr_number)
        return self.worktree_manager.create_worktree(issue_number, branch)

    def _get_failing_ci_logs(self, pr_number: int) -> str:
        """Fetch combined failure logs for recent failed CI runs on a PR.

        Scopes the ``gh run list`` query to the PR's head branch so we only
        see runs that belong to this PR rather than the most-recent repo-wide
        runs (the previous repo-wide query could return runs for other PRs
        and even other branches, making the logs useless for fixing *this* PR).

        Args:
            pr_number: GitHub PR number.

        Returns:
            Combined log string, truncated to 10 000 characters.

        """
        try:
            branch = self._get_pr_branch(pr_number)
            result2 = _gh_call(
                [
                    "run",
                    "list",
                    "--branch",
                    branch,
                    "--status",
                    "failure",
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
                    logger.debug("Could not fetch log for run %s: %s", run_id, log_err)

            return "\n\n".join(logs)[:10000]

        except Exception as e:
            logger.warning("Could not fetch CI logs for PR #%s: %s", pr_number, e)
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
            logger.debug("No implementer state file for issue #%s", issue_number)
            return None

        try:
            data = json.loads(state_file.read_text())
            session_id: str | None = data.get("session_id")
            session_agent: str | None = data.get("session_agent")
            if session_id and not session_agent_matches(session_agent, self.options.agent):
                logger.info(
                    "Skipping impl session for issue #%s: session belongs to %s, "
                    "selected agent is %s",
                    issue_number,
                    session_agent or "claude",
                    self.options.agent,
                )
                return None
            if session_id:
                logger.debug("Loaded session_id for issue #%s: %s...", issue_number, session_id[:8])
            return session_id
        except Exception as e:
            logger.warning("Could not load session_id for issue #%s: %s", issue_number, e)
            return None

    def _run_ci_fix_session(  # noqa: C901  # provider resume/fallback paths are intentionally coupled
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

        try:
            if is_codex(self.options.agent):
                try:
                    if session_id:
                        try:
                            codex_result = resume_codex_session(
                                session_id,
                                prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                            )
                        except subprocess.CalledProcessError as e:
                            logger.warning(
                                "Issue #%s: Codex resume session %r failed for PR #%s; "
                                "falling back to fresh session: %s",
                                issue_number,
                                session_id,
                                pr_number,
                                (e.stderr or e.stdout or "")[:300],
                            )
                            codex_result = run_codex_session(
                                prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                                sandbox="workspace-write",
                            )
                    else:
                        codex_result = run_codex_session(
                            prompt,
                            cwd=worktree_path,
                            timeout=ci_driver_claude_timeout(),
                            sandbox="workspace-write",
                        )
                    logger.debug(
                        "Issue #%s: Codex CI fix output: %s",
                        issue_number,
                        codex_result.stdout[:500],
                    )
                except subprocess.CalledProcessError as e:
                    logger.error(
                        "Issue #%s: Codex CI fix session returned exit code %s: %s",
                        issue_number,
                        e.returncode,
                        (e.stderr or e.stdout or "")[:300],
                    )
                    return False

                try:
                    run(["git", "push", "origin", "HEAD"], cwd=worktree_path)
                    logger.info("Issue #%s: pushed CI fixes for PR #%s", issue_number, pr_number)
                    return True
                except Exception as push_err:
                    logger.error(
                        "Issue #%s: git push failed after CI fix: %s", issue_number, push_err
                    )
                    return False

            # Fresh sessions pin the implementer model; --resume sessions inherit
            # the original session's model and ignore --model.
            base_cmd = [
                "claude",
                "--model",
                implementer_model(),
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

            claude_result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=worktree_path,
                timeout=ci_driver_claude_timeout(),
            )

            # If --resume failed, retry without it
            if claude_result.returncode != 0 and session_id:
                logger.warning(
                    "Issue #%s: --resume session failed, retrying without it", issue_number
                )
                claude_result = subprocess.run(
                    base_cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    cwd=worktree_path,
                    timeout=ci_driver_claude_timeout(),
                )

            if claude_result.returncode == 0:
                # Push the fixes
                try:
                    run(["git", "push", "origin", "HEAD"], cwd=worktree_path)
                    logger.info("Issue #%s: pushed CI fixes for PR #%s", issue_number, pr_number)
                    return True
                except Exception as push_err:
                    logger.error(
                        "Issue #%s: git push failed after CI fix: %s", issue_number, push_err
                    )
                    return False

            logger.error(
                "Issue #%s: Claude CI fix session returned exit code %s: %s",
                issue_number,
                claude_result.returncode,
                claude_result.stderr[:300],
            )
            return False

        except subprocess.TimeoutExpired:
            logger.error(
                "Issue #%s: Claude CI fix session timed out for PR #%s", issue_number, pr_number
            )
            return False
        except Exception as e:
            logger.error(
                "Issue #%s: CI fix session failed for PR #%s: %s", issue_number, pr_number, e
            )
            return False

    def _enable_auto_merge(self, pr_number: int) -> bool:
        """Enable auto-merge for the given PR using rebase strategy.

        First attempts ``gh pr merge --auto --rebase``. On failure, if
        ``options.force_merge_on_stall`` is set, falls back to a direct
        squash merge (``gh pr merge --squash --delete-branch``). If both
        strategies fail, logs an ERROR and returns False.

        Args:
            pr_number: GitHub PR number.

        Returns:
            True if auto-merge was enabled (or fallback merge succeeded),
            False if both strategies failed.

        """
        try:
            _gh_call(["pr", "merge", str(pr_number), "--auto", "--rebase"])
            logger.info("Enabled auto-merge for PR #%s", pr_number)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(
                "Could not enable auto-merge (--rebase) for PR #%s: %s; "
                "will attempt squash-merge fallback if force_merge_on_stall is set",
                pr_number,
                e,
            )

        if not self.options.force_merge_on_stall:
            logger.error(
                "PR #%s: auto-merge failed and force_merge_on_stall is not set; "
                "skipping squash-merge fallback",
                pr_number,
            )
            return False

        # Fallback: direct squash merge
        try:
            _gh_call(["pr", "merge", str(pr_number), "--squash", "--delete-branch"])
            logger.info("Squash-merged PR #%s via fallback", pr_number)
            return True
        except subprocess.CalledProcessError as fallback_err:
            logger.error(
                "PR #%s: both auto-merge and squash-merge fallback failed: %s",
                pr_number,
                fallback_err,
            )
            return False

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
    parser.add_argument(
        "--force-run",
        action="store_true",
        help=(
            "Bypass the final-loop-only gate. By default, the driver refuses to "
            "run unless HEPH_LOOP_INDEX == HEPH_TOTAL_LOOPS or both are unset; "
            "use --force-run to override (e.g. ad-hoc invocation outside the "
            "automation loop). Setting HEPH_CI_DRIVER_FORCE=1 has the same effect."
        ),
    )

    return parser.parse_args()


def _final_loop_gate_passes(force: bool) -> tuple[bool, str]:
    """Return (allowed, reason) for the final-loop-only gate.

    The shell loop sets HEPH_LOOP_INDEX (1-based current loop) and
    HEPH_TOTAL_LOOPS so this module can refuse to run on non-final loops.
    Both unset means "not running under the loop" — allowed (CI debugging,
    one-shot invocations). Either set without the other is treated as a
    misconfiguration and the gate fails closed.
    """
    if force or os.environ.get("HEPH_CI_DRIVER_FORCE") == "1":
        return True, "force flag set"

    idx_raw = os.environ.get("HEPH_LOOP_INDEX")
    total_raw = os.environ.get("HEPH_TOTAL_LOOPS")
    if idx_raw is None and total_raw is None:
        return True, "no loop env set (standalone invocation)"
    if idx_raw is None or total_raw is None:
        return False, (
            "only one of HEPH_LOOP_INDEX/HEPH_TOTAL_LOOPS is set "
            f"(idx={idx_raw!r}, total={total_raw!r}); fail-closed"
        )
    try:
        idx = int(idx_raw)
        total = int(total_raw)
    except ValueError:
        return False, (f"non-integer loop env: idx={idx_raw!r} total={total_raw!r}")
    if idx != total:
        return False, f"loop {idx}/{total} — drive-green is final-loop-only"
    return True, f"final loop {idx}/{total}"


def main() -> int:
    """Execute the CI driver workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt.

    """
    args = _parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)

    allowed, reason = _final_loop_gate_passes(force=args.force_run)
    if not allowed:
        log.error(
            "ci_driver refused to run: %s. Pass --force-run (or set "
            "HEPH_CI_DRIVER_FORCE=1) to override.",
            reason,
        )
        return 2
    log.debug("ci_driver gate passed: %s", reason)

    log.info("Starting CI driver for issues: %s", args.issues)

    try:
        options = CIDriverOptions(
            issues=args.issues,
            agent=args.agent,
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            enable_ui=not args.no_ui,
            verbose=args.verbose,
        )

        driver = CIDriver(options)
        results = driver.run()

        failed = [num for num, result in results.items() if not result.success]
        if failed:
            log.error("CI drive failed for %s issue(s): %s", len(failed), failed)
            return 1

        log.info("CI driver complete")
        return 0

    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
