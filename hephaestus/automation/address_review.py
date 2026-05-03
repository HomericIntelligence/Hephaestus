"""Address unresolved PR review threads using Claude Code.

Provides:
- Parallel processing of issues with unresolved review threads
- Session resume for the original implementer's Claude session
- Selective thread resolution based on Claude's reported fixes
- State persistence and UI monitoring

This module finds PRs with unresolved review threads, resumes the original
implementer's Claude session (or starts a fresh one), runs Claude to fix the
code, then resolves only the threads Claude explicitly reports as addressed.
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
from .github_api import (
    _gh_call,
    gh_pr_list_unresolved_threads,
    gh_pr_resolve_thread,
    write_secure,
)
from .models import AddressReviewOptions, ReviewPhase, ReviewState, WorkerResult
from .prompts import get_address_review_prompt
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


class AddressReviewer:
    """Addresses unresolved PR review threads using Claude Code.

    Features:
    - Parallel processing across multiple issues
    - Session resume from implementer's saved Claude session
    - Selective thread resolution (only resolves threads Claude explicitly fixed)
    - State persistence for observability
    - Real-time curses UI for status monitoring
    """

    def __init__(self, options: AddressReviewOptions) -> None:
        """Initialize address reviewer.

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
        """Run the address review workflow.

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        logger.info(f"Starting address review for issues: {self.options.issues}")

        # Pre-discover PRs — only submit workers for issues that have an open PR.
        # This prevents Claude from being launched for issues with no PR at all.
        pr_map = self._discover_prs(self.options.issues)
        if not pr_map:
            logger.warning("No open PRs found for the specified issues — nothing to address")
            return {}

        logger.info(f"Found {len(pr_map)} PR(s) to address: {pr_map}")

        # Start UI if enabled and not dry run
        if not self.options.dry_run and self.options.enable_ui:
            self.ui = CursesUI(self.status_tracker, self.log_manager)
            self.ui.start()

        try:
            results = self._address_all(pr_map)
            return results
        finally:
            if self.ui:
                self.ui.stop()
            if not self.options.dry_run:
                self.worktree_manager.cleanup_all()

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

    def _address_all(self, pr_map: dict[int, int]) -> dict[int, WorkerResult]:
        """Address all issues in parallel.

        Args:
            pr_map: Mapping of issue_number -> pr_number (pre-filtered to issues with PRs)

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}

            for idx, (issue_num, pr_num) in enumerate(pr_map.items()):
                slot_id = idx % self.options.max_workers
                future = executor.submit(self._address_issue, issue_num, pr_num, slot_id)
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
                            logger.info(f"Issue #{issue_num} address review completed")
                        else:
                            logger.error(
                                f"Issue #{issue_num} address review failed: {result.error}"
                            )
                    except Exception as e:
                        logger.error(f"Issue #{issue_num} raised exception: {e}")
                        results[issue_num] = WorkerResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

        self._print_summary(results)
        return results

    def _address_issue(self, issue_number: int, pr_number: int, slot_id: int) -> WorkerResult:
        """Address unresolved review threads for a single issue.

        The pr_number is pre-discovered by run() — no Claude agent is ever launched
        for issues that have no open PR.

        Flow:
        1. List unresolved threads
        2. Load impl session_id from state file
        3. Load/create review state
        4. Checkout worktree for the PR branch
        5. Run Claude fix session
        6. Parse JSON from Claude output
        7. DRY-RUN GUARD: return before any writes if dry_run
        8. Commit changes if any
        9. Push branch
        10. Resolve addressed threads
        11. Update review state
        12. Return WorkerResult

        Args:
            issue_number: GitHub issue number
            pr_number: Pre-discovered open PR number for this issue
            slot_id: Worker slot ID for status updates

        Returns:
            WorkerResult

        """
        thread_id = threading.get_ident()
        self.status_tracker.update_slot(slot_id, f"#{issue_number}: Starting")
        self._log("info", f"Addressing PR #{pr_number} for issue #{issue_number}", thread_id)

        try:
            # Step 2: List unresolved threads
            self.status_tracker.update_slot(slot_id, f"#{issue_number}: Listing threads")
            threads = gh_pr_list_unresolved_threads(pr_number, dry_run=self.options.dry_run)
            if not threads:
                self._log(
                    "info",
                    f"No unresolved threads on PR #{pr_number} for issue #{issue_number}",
                    thread_id,
                )
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    pr_number=pr_number,
                )

            self._log(
                "info",
                f"Found {len(threads)} unresolved thread(s) on PR #{pr_number}",
                thread_id,
            )

            # Step 3: Load impl session_id
            session_id = self._load_impl_session_id(issue_number)

            # Step 4: Load or create review state
            review_state = self._load_review_state(issue_number)
            if review_state is None:
                branch_name = f"{issue_number}-auto-impl"
                review_state = ReviewState(
                    issue_number=issue_number,
                    pr_number=pr_number,
                    branch_name=branch_name,
                )
            else:
                # Update PR number in case it changed
                review_state.pr_number = pr_number

            branch_name = review_state.branch_name or f"{issue_number}-auto-impl"

            # Step 5: Checkout worktree
            self.status_tracker.update_slot(slot_id, f"#{issue_number}: Setting up worktree")
            worktree_path = self._get_or_create_worktree(issue_number, branch_name, review_state)

            with self.state_lock:
                review_state.worktree_path = str(worktree_path)
                review_state.branch_name = branch_name
                review_state.phase = ReviewPhase.FIXING
            self._save_review_state(review_state)

            # Step 6: Run Claude fix session
            self.status_tracker.update_slot(slot_id, f"#{issue_number}: Running Claude fix")
            fix_result = self._run_fix_session(
                issue_number=issue_number,
                pr_number=pr_number,
                worktree_path=worktree_path,
                threads=threads,
                session_id=session_id if self.options.resume_impl_session else None,
            )

            addressed: list[str] = fix_result.get("addressed", [])
            replies: dict[str, str] = fix_result.get("replies", {})

            self._log(
                "info",
                f"Claude addressed {len(addressed)} thread(s) on PR #{pr_number}",
                thread_id,
            )

            # Step 8: DRY-RUN GUARD
            if self.options.dry_run:
                self._log(
                    "info",
                    f"[DRY RUN] Would resolve {len(addressed)} thread(s) "
                    f"and push for PR #{pr_number}",
                    thread_id,
                )
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    pr_number=pr_number,
                    branch_name=branch_name,
                    worktree_path=str(worktree_path),
                )

            # Step 9: Commit changes if any
            self.status_tracker.update_slot(slot_id, f"#{issue_number}: Committing")
            self._commit_if_changes(issue_number, worktree_path)

            # Step 10: Push branch
            self.status_tracker.update_slot(slot_id, f"#{issue_number}: Pushing")
            self._push_branch(branch_name, worktree_path)

            # Step 11: Resolve addressed threads
            self.status_tracker.update_slot(slot_id, f"#{issue_number}: Resolving threads")
            self._resolve_addressed_threads(addressed, replies)

            # Step 12: Update review state
            with self.state_lock:
                existing_ids = set(review_state.addressed_thread_ids)
                for tid in addressed:
                    existing_ids.add(tid)
                review_state.addressed_thread_ids = list(existing_ids)
                review_state.phase = ReviewPhase.COMPLETED
                review_state.completed_at = datetime.now(timezone.utc)
            self._save_review_state(review_state)

            self.status_tracker.update_slot(slot_id, f"#{issue_number}: Done")
            self._log(
                "info",
                f"Address review complete for issue #{issue_number} (PR #{pr_number})",
                thread_id,
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
            return self._fail(issue_number, error_msg, slot_id)

        except subprocess.CalledProcessError as e:
            error_msg = (
                f"Command failed (exit {e.returncode}): {' '.join(str(c) for c in e.cmd[:3])}"
            )
            self._log("error", error_msg, thread_id)
            return self._fail(issue_number, error_msg, slot_id)

        except RuntimeError as e:
            self._log("error", f"Runtime error: {e}", thread_id)
            return self._fail(issue_number, str(e)[:80], slot_id)

        except Exception as e:
            self._log("error", f"Unexpected {type(e).__name__}: {e}", thread_id)
            return self._fail(issue_number, str(e)[:80], slot_id)

        finally:
            time.sleep(1)
            self.status_tracker.release_slot(slot_id)

    def _find_pr_for_issue(self, issue_number: int) -> int | None:
        """Find the open PR for a single issue.

        Tries branch name lookup first, then falls back to body search.

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

        # Strategy 2: Check review state for stored pr_number
        review_state = self._load_review_state(issue_number)
        if review_state and review_state.pr_number:
            # Verify the PR is still open
            try:
                result = _gh_call(
                    [
                        "pr",
                        "view",
                        str(review_state.pr_number),
                        "--json",
                        "number,state",
                    ],
                    check=False,
                )
                pr_data = json.loads(result.stdout or "{}")
                if pr_data.get("state", "").upper() == "OPEN":
                    logger.info(
                        f"Found PR #{review_state.pr_number} for issue #{issue_number} "
                        "via review state"
                    )
                    return int(review_state.pr_number)
            except Exception as e:
                logger.debug(f"Review state PR lookup failed for issue #{issue_number}: {e}")

        # Strategy 3: Search PR body for issue reference
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

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Load the implementer's Claude session ID from state file.

        Args:
            issue_number: GitHub issue number

        Returns:
            Session ID string if found, None otherwise

        """
        state_file = self.state_dir / f"issue-{issue_number}.json"
        if not state_file.exists():
            logger.warning(
                f"No implementation state for issue #{issue_number}, will use fresh session"
            )
            return None
        try:
            data = json.loads(state_file.read_text())
            session_id: str | None = data.get("session_id")
            return session_id
        except Exception as e:
            logger.warning(f"Could not load impl session for #{issue_number}: {e}")
            return None

    def _load_review_state(self, issue_number: int) -> ReviewState | None:
        """Load review state from disk.

        Args:
            issue_number: GitHub issue number

        Returns:
            ReviewState if state file exists and is valid, None otherwise

        """
        state_file = self.state_dir / f"review-{issue_number}.json"
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text())
            return ReviewState.model_validate(data)
        except Exception as e:
            logger.warning(f"Could not load review state for #{issue_number}: {e}")
            return None

    def _save_review_state(self, state: ReviewState) -> None:
        """Save review state to disk.

        Args:
            state: ReviewState to persist

        """
        state_file = self.state_dir / f"review-{state.issue_number}.json"
        write_secure(state_file, state.model_dump_json(indent=2))

    def _get_or_create_worktree(
        self,
        issue_number: int,
        branch_name: str,
        review_state: ReviewState,
    ) -> Path:
        """Get existing worktree or create a new one for the PR branch.

        Reuses the worktree path from review state if it still exists on disk.
        Otherwise creates a new worktree via WorktreeManager.

        Args:
            issue_number: GitHub issue number
            branch_name: PR branch name
            review_state: Current review state (may contain existing worktree path)

        Returns:
            Path to worktree directory

        """
        # Try to reuse existing worktree from review state
        if review_state.worktree_path:
            existing_path = Path(review_state.worktree_path)
            if existing_path.exists() and (existing_path / ".git").exists():
                logger.info(
                    f"Reusing existing worktree at {existing_path} for issue #{issue_number}"
                )
                # Register with worktree manager so cleanup works
                with self.worktree_manager.lock:
                    self.worktree_manager.worktrees[issue_number] = existing_path
                return existing_path

        # Create new worktree
        logger.info(f"Creating new worktree for issue #{issue_number} on branch {branch_name}")
        return self.worktree_manager.create_worktree(issue_number, branch_name)

    def _run_fix_session(
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        threads: list[dict[str, Any]],
        session_id: str | None,
    ) -> dict[str, Any]:
        """Run Claude fix session to address review threads.

        Builds the address review prompt and runs Claude with --resume if a
        session_id is provided. Falls back to a fresh session if --resume fails.

        Args:
            issue_number: GitHub issue number
            pr_number: GitHub PR number
            worktree_path: Path to git worktree containing PR branch
            threads: List of unresolved thread dicts (id, path, line, body)
            session_id: Previous Claude session ID to resume, or None for fresh session

        Returns:
            Parsed dict with "addressed" and "replies" keys

        """
        if self.options.dry_run:
            logger.info(f"[DRY RUN] Would run fix session for PR #{pr_number}")
            return {"addressed": [], "replies": {}}

        threads_json = json.dumps(
            [
                {
                    "thread_id": t["id"],
                    "path": t["path"],
                    "line": t.get("line"),
                    "body": t["body"],
                }
                for t in threads
            ]
        )

        prompt = get_address_review_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            worktree_path=str(worktree_path),
            threads_json=threads_json,
        )

        prompt_file = worktree_path / f".claude-address-review-{issue_number}.md"
        prompt_file.write_text(prompt)
        log_file = self.state_dir / f"address-review-{issue_number}.log"

        def _build_cmd(with_resume: bool, sid: str | None = None) -> list[str]:
            base = ["claude", str(prompt_file), "--output-format", "json"]
            if with_resume and sid:
                base += ["--resume", sid]
            base += [
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "Read,Write,Edit,Glob,Grep,Bash",
            ]
            return base

        try:
            # Attempt with session resume first if we have a session_id
            if session_id:
                try:
                    result = run(
                        _build_cmd(with_resume=True, sid=session_id),
                        cwd=worktree_path,
                        timeout=1800,  # 30 minutes
                    )
                except subprocess.CalledProcessError as e:
                    stderr = e.stderr or ""
                    # Fall back to fresh session on session-not-found errors
                    if any(
                        phrase in stderr.lower()
                        for phrase in ("session not found", "invalid session", "session expired")
                    ):
                        logger.warning(
                            f"Session {session_id!r} not found for issue #{issue_number}; "
                            "falling back to fresh session"
                        )
                        result = run(
                            _build_cmd(with_resume=False),
                            cwd=worktree_path,
                            timeout=1800,
                        )
                    else:
                        raise
            else:
                result = run(
                    _build_cmd(with_resume=False),
                    cwd=worktree_path,
                    timeout=1800,
                )

            log_file.write_text(result.stdout or "")

            # Extract response text from Claude's JSON wrapper
            try:
                data = json.loads(result.stdout or "{}")
                response_text: str = data.get("result", result.stdout or "")
            except (json.JSONDecodeError, AttributeError):
                response_text = result.stdout or ""

            parsed = self._parse_json_block(response_text)
            logger.info(
                f"Fix session complete for PR #{pr_number}; "
                f"addressed {len(parsed.get('addressed', []))} thread(s)"
            )
            return parsed

        except subprocess.CalledProcessError as e:
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            error_output = f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            log_file.write_text(error_output)
            raise RuntimeError(
                f"Fix session failed for PR #{pr_number}: {e.stderr or e.stdout}"
            ) from e
        except subprocess.TimeoutExpired as e:
            log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
            raise RuntimeError(f"Fix session timed out for PR #{pr_number}") from e
        finally:
            with contextlib.suppress(Exception):
                prompt_file.unlink()

    def _parse_json_block(self, text: str) -> dict[str, Any]:
        """Extract the last ```json ... ``` block from Claude's response.

        Args:
            text: Claude's full response text

        Returns:
            Parsed dict with "addressed" and "replies" keys, or defaults if not found

        """
        matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if not matches:
            return {"addressed": [], "replies": {}}
        try:
            return dict(json.loads(matches[-1]))
        except json.JSONDecodeError:
            return {"addressed": [], "replies": {}}

    def _resolve_addressed_threads(self, addressed: list[str], replies: dict[str, str]) -> None:
        """Resolve the review threads that Claude explicitly fixed.

        Only resolves threads listed in ``addressed``. Skips threads that fail
        to resolve with a warning rather than aborting the whole workflow.

        Args:
            addressed: List of thread_id strings Claude reported as fixed
            replies: Mapping of thread_id to one-line reply describing the fix

        """
        for thread_id in addressed:
            reply = replies.get(thread_id, "Addressed in code.")
            try:
                gh_pr_resolve_thread(thread_id, reply, dry_run=self.options.dry_run)
            except Exception as e:
                logger.warning(f"Could not resolve thread {thread_id}: {e}")

    def _commit_if_changes(self, issue_number: int, worktree_path: Path) -> None:
        """Commit any pending changes in the worktree.

        Silently skips if there are no changes to commit.

        Args:
            issue_number: GitHub issue number (used in commit message)
            worktree_path: Path to git worktree

        """
        result = run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
        )
        if not result.stdout.strip():
            logger.info(f"No changes to commit for issue #{issue_number}")
            return

        try:
            from .pr_manager import commit_changes

            commit_changes(issue_number, worktree_path)
            logger.info(f"Committed fix changes for issue #{issue_number}")
        except RuntimeError as e:
            # commit_changes raises RuntimeError if nothing to commit; already checked above
            logger.warning(f"Commit skipped for issue #{issue_number}: {e}")

    def _push_branch(self, branch_name: str, worktree_path: Path) -> None:
        """Push the branch to origin.

        Args:
            branch_name: Branch name to push
            worktree_path: Path to git worktree

        Raises:
            RuntimeError: If push fails

        """
        try:
            run(
                ["git", "push", "origin", branch_name],
                cwd=worktree_path,
            )
            logger.info(f"Pushed branch {branch_name} to origin")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to push branch {branch_name}: {e}") from e

    def _fail(
        self,
        issue_number: int,
        error_msg: str,
        slot_id: int,
    ) -> WorkerResult:
        """Record a failure, update state and tracker, and return a failed WorkerResult.

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
            self._save_review_state(err_state)
        return WorkerResult(issue_number=issue_number, success=False, error=error_msg)

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print address review summary.

        Args:
            results: Mapping of issue number to WorkerResult

        """
        total = len(results)
        successful = sum(1 for r in results.values() if r.success)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("Address Review Summary")
        logger.info("=" * 60)
        logger.info(f"Total issues: {total}")
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
    """Parse command line arguments for the address review CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Find PRs with unresolved review threads and use Claude Code to fix the code, "
            "then resolve only the threads Claude explicitly addresses."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Address review threads for specific issues
  %(prog)s --issues 595 596

  # Dry run — show what would be done without any GitHub writes or git pushes
  %(prog)s --issues 595 --dry-run

  # Use more parallel workers
  %(prog)s --issues 595 596 597 --max-workers 5
        """,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        required=True,
        help="Issue numbers whose linked PRs should have review threads addressed",
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
        help="Show what would be done without actually resolving threads or pushing code",
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
    """Execute the address review workflow.

    Returns:
        Exit code: 0 on success, 1 if any issue failed, 130 on keyboard interrupt

    """
    args = _parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)
    log.info(f"Starting address review for issues: {args.issues}")

    from hephaestus.utils.terminal import terminal_guard

    options = AddressReviewOptions(
        issues=args.issues,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        enable_ui=not args.no_ui,
        verbose=args.verbose,
    )

    with terminal_guard():
        try:
            reviewer = AddressReviewer(options)
            results = reviewer.run()

            failed = [num for num, result in results.items() if not result.success]
            if failed:
                log.error(f"Failed to address review for {len(failed)} issue(s): {failed}")
                return 1

            log.info("Address review complete")
            return 0
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
