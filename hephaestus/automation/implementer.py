"""Bulk issue implementation using Claude Code in parallel worktrees.

Provides:
- Dependency-aware parallel implementation
- Git worktree isolation
- State persistence and resume
- CI fix automation
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from hephaestus.agents.runtime import (
    add_agent_argument,
    is_codex,
    resume_codex_session,
    run_codex_session,
    run_codex_text,
    session_agent_matches,
)
from hephaestus.github.rate_limit import (
    detect_claude_usage_cap,
    detect_rate_limit,
    wait_until,
)

from .claude_invoke import (
    SESSION_EXPIRED_PHRASES as _SESSION_EXPIRED_PHRASES,
)
from .claude_invoke import invoke_claude_with_session, parse_review_verdict
from .claude_models import implementer_model, reviewer_model
from .claude_timeouts import implementer_claude_timeout
from .curses_ui import CursesUI, ThreadLogManager
from .dependency_resolver import CyclicDependencyError, DependencyResolver
from .follow_up import parse_follow_up_items, run_follow_up_issues
from .git_utils import get_repo_root, get_repo_slug, issue_ref, pr_ref, run
from .github_api import fetch_issue_info, gh_list_open_issues
from .learn import learn_needs_rerun, run_learn
from .models import (
    ImplementationPhase,
    ImplementationState,
    ImplementerOptions,
    IssueState,
    WorkerResult,
)
from .pr_manager import commit_changes, create_pr, ensure_pr_created
from .prompts import (
    get_impl_loop_review_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)
from .session_naming import AGENT_IMPLEMENTER
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

MAX_REVIEW_ITERATIONS = 3

# Default Claude implementation timeout in seconds. Actual runtime value is
# read from the ``HEPH_IMPLEMENTER_CLAUDE_TIMEOUT`` env-var by
# :func:`.claude_timeouts.implementer_claude_timeout`; this constant serves
# as the documented default and can be used in tests.
_CLAUDE_IMPL_TIMEOUT: int = 1800


logger = logging.getLogger(__name__)


def _claude_quota_reset_epoch(*texts: str) -> int | None:
    """Find a quota-reset epoch across one or more output streams.

    Inspects each text for either form of rate-limit message — the GitHub-CLI
    "Limit reached ..." form or the Claude-CLI "out of extra usage ·
    resets ..." form. Uses ``is not None`` chaining so an epoch of ``0``
    (rate-limited, reset time unknown) is preserved instead of being
    mistaken for "no rate limit".
    """
    for text in texts:
        for detect in (detect_rate_limit, detect_claude_usage_cap):
            epoch = detect(text)
            if epoch is not None:
                return epoch
    return None


class IssueImplementer:
    """Implements GitHub issues in parallel using Claude Code.

    Features:
    - Dependency resolution and topological ordering
    - Parallel execution in isolated git worktrees
    - State persistence for resume capability
    - Automatic CI fix attempts
    - Real-time curses UI for status monitoring
    """

    def __init__(self, options: ImplementerOptions):
        """Initialize issue implementer.

        Args:
            options: Implementer configuration options

        """
        self.options = options
        self.repo_root = get_repo_root()
        self.state_dir = self.repo_root / ".issue_implementer"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.resolver = DependencyResolver(skip_closed=options.skip_closed)
        self.worktree_manager = WorktreeManager()
        self.status_tracker = StatusTracker(options.max_workers)
        self.log_manager = ThreadLogManager()

        self.states: dict[int, ImplementationState] = {}
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
        """Run the implementer.

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        # Health check mode
        if self.options.health_check:
            return self._health_check()

        # Load issues or epic and resolve dependencies
        if self.options.issues:
            logger.info("Loading issues: %s", self.options.issues)
            self._load_issues(self.options.issues)
        else:
            logger.info("Loading epic #%s", self.options.epic_number)
            self.resolver.load_epic(self.options.epic_number)

        # Detect cycles
        try:
            self.resolver.detect_cycles()
        except CyclicDependencyError as e:
            logger.error("Dependency cycle detected: %s", e)
            return {}

        # Analyze only mode
        if self.options.analyze_only:
            return self._analyze_dependencies()

        # Always load state to detect failed learns
        self._load_state()

        # Re-run failed learns before normal processing
        if self.options.enable_learn:
            retro_results = self._rerun_failed_learns()
            if retro_results:
                logger.info("Re-ran %s failed learn(s)", len(retro_results))

        # Start UI if enabled and not in dry run
        if not self.options.dry_run and self.options.enable_ui:
            self.ui = CursesUI(self.status_tracker, self.log_manager)
            self.ui.start()

        try:
            # Implement issues
            results = self._implement_all()
            return results
        finally:
            # Stop UI
            if self.ui:
                self.ui.stop()

            # Cleanup worktrees
            if not self.options.dry_run:
                self.worktree_manager.cleanup_all()

    def _load_issues(self, issue_numbers: list[int]) -> None:
        """Load specific issues into the dependency graph.

        Args:
            issue_numbers: List of issue numbers to load

        """
        from .github_api import fetch_issue_info, prefetch_issue_states

        # Prefetch states for efficiency
        cached_states = prefetch_issue_states(issue_numbers)

        for issue_num in issue_numbers:
            if self.options.skip_closed and cached_states.get(issue_num) == IssueState.CLOSED:
                logger.info("Skipping closed issue #%s", issue_num)
                self.resolver.completed.add(issue_num)
                continue

            try:
                issue = fetch_issue_info(issue_num)
                self.resolver.add_issue(issue)

                # Load dependencies recursively
                self.resolver._load_dependencies(issue, cached_states)

            except (
                Exception
            ) as e:  # broad catch: network errors, API failures, JSON parsing all possible
                logger.error("Failed to load issue #%s: %s", issue_num, e)

        logger.info("Loaded %s issues", len(self.resolver.graph.issues))

    def _health_check(self) -> dict[int, WorkerResult]:
        """Perform health check of dependencies and environment.

        Returns:
            Empty results dictionary

        """
        logger.info("Running health check...")

        # Check gh CLI
        try:
            run(["gh", "--version"], check=True)
            logger.info("gh CLI available")
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
            logger.error("gh CLI not available: %s", e)

        # Check git
        try:
            run(["git", "--version"], check=True)
            logger.info("git available")
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
            logger.error("git not available: %s", e)

        # Check selected agent runtime
        agent_binary = "codex" if is_codex(self.options.agent) else "claude"
        agent_name = "Codex" if is_codex(self.options.agent) else "Claude Code"
        try:
            run([agent_binary, "--version"], check=True)
            logger.info("%s available", agent_name)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
            logger.error("%s not available: %s", agent_name, e)

        # Check repository
        try:
            branch = run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
            ).stdout.strip()
            logger.info("In git repository (branch: %s)", branch)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
            logger.error("Not in git repository: %s", e)

        logger.info("Health check complete")
        return {}

    def _analyze_dependencies(self) -> dict[int, WorkerResult]:
        """Analyze and display dependency graph.

        Returns:
            Empty results dictionary

        """
        logger.info("Dependency Analysis")
        logger.info("=" * 60)

        stats = self.resolver.get_stats()
        logger.info("Total issues: %s", stats["total_issues"])
        logger.info("Completed: %s", stats["completed_issues"])
        logger.info("Remaining: %s", stats["remaining_issues"])
        logger.info("Ready: %s", stats["ready_issues"])

        # Show topological order
        try:
            order = self.resolver.topological_sort()
            logger.info("\nImplementation order:")
            for i, issue_num in enumerate(order, 1):
                issue = self.resolver.graph.issues[issue_num]
                deps = self.resolver.graph.get_dependencies(issue_num)
                dep_str = f" (depends on: {deps})" if deps else ""
                logger.info("  %s. #%s: %s%s", i, issue_num, issue.title, dep_str)
        except CyclicDependencyError as e:
            logger.error("Failed to compute topological order: %s", e)

        return {}

    def _implement_all(self) -> dict[int, WorkerResult]:  # noqa: C901  # orchestration with many retry/outcome paths
        """Implement all issues with dependency awareness.

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}
            active_issues: set[int] = set()

            while True:
                # Get ready issues
                ready = self.resolver.get_ready_issues()

                # Submit new work
                submitted_any = False
                for issue in ready:
                    if issue.number not in active_issues and issue.number not in results:
                        future = executor.submit(self._implement_issue, issue.number)
                        futures[future] = issue.number
                        active_issues.add(issue.number)
                        submitted_any = True

                # Check for completed work
                if not futures:
                    # No active futures and no more work to do
                    break

                # Wait for at least one to complete
                try:
                    done, _pending = wait(futures.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
                except Exception:  # broad catch: thread pool can raise various internal errors
                    # Timeout or error - check if we should continue
                    if not submitted_any and not futures:
                        break
                    # Add backoff when no work available
                    time.sleep(0.1)
                    continue

                # Process completed futures
                for future in done:
                    issue_num = futures[future]
                    active_issues.remove(issue_num)
                    del futures[future]

                    try:
                        result = future.result()
                        results[issue_num] = result

                        if result.success:
                            self.resolver.mark_completed(issue_num)
                            logger.info("Issue #%s completed successfully", issue_num)
                        else:
                            logger.error("Issue #%s failed: %s", issue_num, result.error)

                    except Exception as e:  # broad catch: worker threads can raise any exception
                        logger.error("Issue #%s raised exception: %s", issue_num, e)
                        results[issue_num] = WorkerResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

                # If no futures pending and no new work submitted, we're done
                if not futures and not ready:
                    break

        # Detect and log issues that were skipped due to unresolved dependencies
        attempted_issues = set(results.keys())
        all_issues = set(self.resolver.graph.issues.keys())
        skipped_issues = all_issues - attempted_issues - self.resolver.completed

        if skipped_issues:
            logger.warning("Skipped %s issue(s) due to failed dependencies:", len(skipped_issues))
            for issue_num in sorted(skipped_issues):
                deps = self.resolver.graph.get_dependencies(issue_num)
                failed_deps = [d for d in deps if d not in self.resolver.completed]
                logger.warning("  #%s: blocked by failed issue(s) %s", issue_num, failed_deps)

        self._print_summary(results)
        return results

    def _finalize_pr(
        self,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
    ) -> int:
        """Ensure commit is pushed and PR is created, then persist the PR number.

        Extracted from :meth:`_implement_issue` to satisfy SRP: this unit owns
        only the "gate the work behind a merged PR" concern, independently of
        the learn/follow-up post-processing that follows.

        Args:
            issue_number: GitHub issue number.
            branch_name: Implementation branch name.
            worktree_path: Path to the git worktree.
            state: Mutable implementation state (pr_number updated in-place).
            slot_id: Worker slot id for status updates.

        Returns:
            PR number created or located by :func:`ensure_pr_created`.

        """
        with self.state_lock:
            state.phase = ImplementationPhase.CREATING_PR
        self._save_state(state)

        # A2-004: optional pre-PR test gate (opt-in via run_pre_pr_tests=True).
        if self.options.run_pre_pr_tests:
            if slot_id is not None:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: Running pre-PR tests"
                )
            tests_passed = self._run_tests_in_worktree(worktree_path, issue_number)
            if not tests_passed:
                logger.warning(
                    "#%d: pre-PR tests failed; PR will still be created but "
                    "manual review is required before merging",
                    issue_number,
                )

        pr_number = self._ensure_pr_created(issue_number, branch_name, worktree_path, slot_id)
        with self.state_lock:
            state.pr_number = pr_number
        self._save_state(state)
        return pr_number

    def _run_post_pr_followup(
        self,
        issue_number: int,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
    ) -> None:
        """Run /learn and file follow-up issues after the PR is created.

        Extracted from :meth:`_implement_issue` to satisfy SRP: this unit owns
        only the knowledge-capture and issue-hygiene concerns that follow a
        successful PR, independently of the PR-creation logic.

        Marks ``state.phase = COMPLETED`` and sets ``state.completed_at``
        regardless of whether the optional learn/follow-up steps succeed, so
        the issue is never left stuck in a non-terminal phase.

        Args:
            issue_number: GitHub issue number.
            worktree_path: Path to the git worktree.
            state: Mutable implementation state (updated in-place).
            slot_id: Worker slot id for status updates.

        """
        # Learn phase (after CREATING_PR, before COMPLETED)
        can_resume_session = self._can_resume_state_session(state)
        if self.options.enable_learn and can_resume_session and state.session_id:
            if slot_id is not None:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: Running learn"
                )
            with self.state_lock:
                state.phase = ImplementationPhase.LEARN
            self._save_state(state)
            retro_success = self._run_learn(
                state.session_id,
                worktree_path,
                issue_number,
                slot_id,
                session_agent=state.session_agent,
            )
            with self.state_lock:
                state.learn_completed = retro_success
            self._save_state(state)

        # Follow-up issues phase (after LEARN, before COMPLETED)
        if self.options.enable_follow_up and can_resume_session and state.session_id:
            if slot_id is not None:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: Identifying follow-ups"
                )
            with self.state_lock:
                state.phase = ImplementationPhase.FOLLOW_UP_ISSUES
            self._save_state(state)
            self._run_follow_up_issues(
                state.session_id,
                worktree_path,
                issue_number,
                slot_id,
                session_agent=state.session_agent,
            )

        # Mark as completed
        with self.state_lock:
            state.phase = ImplementationPhase.COMPLETED
            state.completed_at = datetime.now(timezone.utc)
        self._save_state(state)

    def _implement_issue(self, issue_number: int) -> WorkerResult:  # noqa: C901  # orchestration with many retry/outcome paths
        """Implement a single issue.

        Args:
            issue_number: Issue number to implement

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
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Starting")
            self._log("info", f"Starting issue {issue_ref(issue_number)}", thread_id)

            # Initialize state
            state = self._get_or_create_state(issue_number)

            branch_name = f"{issue_number}-auto-impl"

            # In dry-run mode skip all real side-effects (worktree creation,
            # Claude calls, PR creation).  This guard must come BEFORE
            # create_worktree() so --dry-run never leaves real .worktrees/
            # directories or branches behind (#371).
            if self.options.dry_run:
                self._log(
                    "info",
                    f"[DRY RUN] Would create worktree, run {self.options.agent}, review, "
                    f"create PR for #{issue_number}",
                    thread_id,
                )
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    branch_name=branch_name,
                    worktree_path=None,
                )

            # Create worktree (only in non-dry-run mode)
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Creating worktree"
            )
            worktree_path = self.worktree_manager.create_worktree(issue_number, branch_name)

            with self.state_lock:
                state.worktree_path = str(worktree_path)
                state.branch_name = branch_name
            self._save_state(state)

            # Check for existing plan
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Checking plan")
            if not self._has_plan(issue_number):
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: Generating plan"
                )
                self._log("info", f"Issue #{issue_number} has no plan, generating...", thread_id)
                with self.state_lock:
                    state.phase = ImplementationPhase.PLANNING
                self._save_state(state)
                self._generate_plan(issue_number)

            # Fetch issue info for context
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Fetching issue")
            with self.state_lock:
                state.phase = ImplementationPhase.IMPLEMENTING
            self._save_state(state)

            # Run the selected implementation agent
            issue = fetch_issue_info(issue_number)
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Running {self.options.agent}"
            )
            session_id = self._run_claude_code(
                issue_number,
                worktree_path,
                get_implementation_prompt(
                    issue_number=issue_number,
                    issue_title=issue.title,
                    issue_body=issue.body,
                    branch_name=branch_name,
                    worktree_path=str(worktree_path),
                    repo_root=str(self.repo_root),
                ),
                slot_id=slot_id,
            )
            with self.state_lock:
                state.session_id = session_id
                state.session_agent = self.options.agent if session_id else None
            self._save_state(state)

            # Strict review loop re-uses the selected agent session when a
            # session id was captured. Reviewer calls are always fresh so
            # their judgment is unbiased.
            with self.state_lock:
                state.phase = ImplementationPhase.REVIEWING
            self._save_state(state)
            iterations, last_verdict, last_grade = self._run_impl_review_loop(
                issue_number=issue_number,
                worktree_path=worktree_path,
                branch_name=branch_name,
                issue_title=issue.title,
                issue_body=issue.body,
                session_id=session_id,
                slot_id=slot_id,
                thread_id=thread_id,
                state=state,
            )
            with self.state_lock:
                state.review_iterations = iterations
                state.last_review_verdict = last_verdict
                state.last_review_grade = last_grade
            self._save_state(state)

            # Verify commit, push, PR creation; then run /learn and follow-ups.
            pr_number = self._finalize_pr(issue_number, branch_name, worktree_path, state, slot_id)
            self._run_post_pr_followup(issue_number, worktree_path, state, slot_id)

            self._log("info", f"Issue #{issue_number} completed: PR {pr_ref(pr_number)}", thread_id)

            return WorkerResult(
                issue_number=issue_number,
                success=True,
                pr_number=pr_number,
                branch_name=branch_name,
                worktree_path=str(worktree_path),
            )

        except subprocess.TimeoutExpired as e:
            error_msg = f"Timeout: {' '.join(e.cmd[:3])} exceeded {e.timeout}s"
            self._log("error", error_msg, thread_id)

            # Show failure in UI before releasing slot
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
            )

            err_state = self._get_state(issue_number)
            if err_state:
                with self.state_lock:
                    err_state.phase = ImplementationPhase.FAILED
                    err_state.error = error_msg
                    err_state.attempts += 1
                self._save_state(err_state)

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=error_msg,
            )

        except subprocess.CalledProcessError as e:
            error_msg = f"Command failed (exit {e.returncode}): {' '.join(e.cmd[:3])}"
            self._log("error", error_msg, thread_id)
            if e.stderr:
                self._log("error", f"stderr: {e.stderr[:300]}", thread_id)

            # Show failure in UI before releasing slot
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
            )

            err_state = self._get_state(issue_number)
            if err_state:
                with self.state_lock:
                    err_state.phase = ImplementationPhase.FAILED
                    err_state.error = str(e)
                    err_state.attempts += 1
                self._save_state(err_state)

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )

        except RuntimeError as e:
            self._log("error", f"Runtime error: {e}", thread_id)

            # Show failure in UI before releasing slot
            error_msg = str(e)[:80]
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
            )

            err_state = self._get_state(issue_number)
            if err_state:
                with self.state_lock:
                    err_state.phase = ImplementationPhase.FAILED
                    err_state.error = str(e)
                    err_state.attempts += 1
                self._save_state(err_state)

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )

        except Exception as e:  # broad catch: top-level worker boundary, must not crash thread pool
            self._log("error", f"Unexpected {type(e).__name__}: {e}", thread_id)

            # Show failure in UI before releasing slot
            error_msg = str(e)[:80]
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
            )

            err_state = self._get_state(issue_number)
            if err_state:
                with self.state_lock:
                    err_state.phase = ImplementationPhase.FAILED
                    err_state.error = str(e)
                    err_state.attempts += 1
                self._save_state(err_state)

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )
        finally:
            self.status_tracker.release_slot(slot_id)

    def _has_plan(self, issue_number: int) -> bool:
        """Check if issue has an implementation plan."""
        try:
            result = run(
                ["gh", "issue", "view", str(issue_number), "--comments", "--json", "comments"],
                capture_output=True,
            )
            data = json.loads(result.stdout)
            comments = data.get("comments", [])

            for comment in comments:
                body = comment.get("body", "")
                if "Implementation Plan" in body or "## Plan" in body:
                    return True

            return False
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            return False

    def _generate_plan(self, issue_number: int) -> None:
        """Generate plan for an issue using hephaestus-plan-issues.

        Prefers the installed entry point, falls back to a local
        scripts/plan_issues.py if present (legacy ProjectScylla layout).
        """
        import shutil

        # Prefer the installed entry point (works in any repo)
        entry_point = shutil.which("hephaestus-plan-issues")
        if entry_point:
            run(
                [entry_point, "--issues", str(issue_number), "--agent", self.options.agent],
                timeout=600,
            )
            return

        # Fall back to python -m invocation (works when PYTHONPATH is set)
        try:
            run(
                [
                    sys.executable,
                    "-m",
                    "hephaestus.automation.planner",
                    "--issues",
                    str(issue_number),
                    "--agent",
                    self.options.agent,
                ],
                timeout=600,
            )
            return
        except (subprocess.SubprocessError, OSError):
            pass

        # Legacy fallback: local scripts/plan_issues.py (ProjectScylla layout)
        plan_script = self.repo_root / "scripts" / "plan_issues.py"
        if plan_script.exists():
            run(
                [sys.executable, str(plan_script), "--issues", str(issue_number)],
                timeout=600,
            )
            return

        raise RuntimeError(
            "Could not find hephaestus-plan-issues entry point, "
            "hephaestus.automation.planner module, or "
            f"scripts/plan_issues.py in {self.repo_root}"
        )

    def _parse_follow_up_items(self, text: str) -> list[dict[str, Any]]:
        """Parse follow-up items from Claude's JSON response."""
        return parse_follow_up_items(text)

    def _can_resume_state_session(self, state: ImplementationState) -> bool:
        """Return True when the saved session can be resumed by the selected agent."""
        if not state.session_id:
            return False
        if session_agent_matches(state.session_agent, self.options.agent):
            return True
        logger.info(
            "Skipping session resume for issue #%s: session belongs to %s, selected agent is %s",
            state.issue_number,
            state.session_agent or "claude",
            self.options.agent,
        )
        return False

    def _run_follow_up_issues(
        self,
        session_id: str,
        worktree_path: Path,
        issue_number: int,
        slot_id: int | None = None,
        *,
        session_agent: str | None = None,
    ) -> None:
        """Resume the selected agent session to identify and file follow-up issues."""
        run_follow_up_issues(
            session_id,
            worktree_path,
            issue_number,
            self.state_dir,
            self.status_tracker,
            slot_id,
            dry_run=self.options.dry_run,
            agent=self.options.agent,
            session_agent=session_agent,
        )

    def _learn_needs_rerun(self, issue_number: int) -> bool:
        """Check if learn log indicates failure."""
        return learn_needs_rerun(issue_number, self.state_dir)

    def _rerun_failed_learns(self) -> dict[int, bool]:
        """Re-run failed learns for completed issues.

        Returns:
            Dictionary mapping issue number to success status

        """
        results: dict[int, bool] = {}

        for issue_number, state in self.states.items():
            # Only re-run for completed issues with failed learns
            if (
                state.phase != ImplementationPhase.COMPLETED
                or state.learn_completed
                or not self._can_resume_state_session(state)
            ):
                continue

            # Check if log indicates failure
            if not self._learn_needs_rerun(issue_number):
                continue

            # Verify worktree exists
            if not state.worktree_path:
                logger.warning("Skipping learn re-run for #%s: no worktree_path", issue_number)
                continue

            session_id = state.session_id
            if session_id is None:
                continue

            worktree_path = Path(state.worktree_path)
            if not worktree_path.exists():
                logger.warning("Skipping learn re-run for #%s: worktree not found", issue_number)
                continue

            # Re-run learn
            logger.info("Re-running failed learn for issue #%s", issue_number)
            success = self._run_learn(
                session_id,
                worktree_path,
                issue_number,
                slot_id=None,
                session_agent=state.session_agent,
            )

            # Update and save state
            with self.state_lock:
                state.learn_completed = success
            self._save_state(state)

            results[issue_number] = success

        if results:
            success_count = sum(1 for s in results.values() if s)
            logger.info(
                "Re-ran %s learn(s): %s succeeded, %s failed",
                len(results),
                success_count,
                len(results) - success_count,
            )

        return results

    def _run_learn(
        self,
        session_id: str,
        worktree_path: Path,
        issue_number: int,
        slot_id: int | None = None,
        *,
        session_agent: str | None = None,
    ) -> bool:
        """Resume the selected agent session to run /learn."""
        return run_learn(
            session_id,
            worktree_path,
            issue_number,
            self.state_dir,
            slot_id,
            agent=self.options.agent,
            session_agent=session_agent,
        )

    # ------------------------------------------------------------------
    # Strict review loop for implementer sessions
    # ------------------------------------------------------------------

    def _run_impl_review_loop(
        self,
        *,
        issue_number: int,
        worktree_path: Path,
        branch_name: str,
        issue_title: str,
        issue_body: str,
        session_id: str | None,
        slot_id: int | None,
        thread_id: int | None,
        state: ImplementationState | None = None,
    ) -> tuple[int, str | None, str | None]:
        """Run the bounded review loop for an implementation.

        Agent sessions are re-used across iterations. Claude uses
        ``claude --resume <session_id>``; Codex uses
        ``codex exec resume <session_id>`` with the session UUID captured from
        the JSONL ``session_meta`` event. The reviewer is a separate, fresh
        session each iteration so its judgment is unbiased.

        Iteration 0 reviews the just-completed initial run. Iterations 1 and 2
        first resume the impl session with feedback, then re-review the
        resulting diff.

        Args:
            issue_number: GitHub issue number.
            worktree_path: Path to the git worktree (for diff and resume CWD).
            branch_name: Implementation branch name (for diff base resolution).
            issue_title: Issue title (review prompt context).
            issue_body: Issue body (review prompt context).
            session_id: Implementer's agent session id. ``None`` if capture
                failed — the loop runs a single review (iteration 0) and stops,
                since we cannot re-iterate without a session to resume.
            slot_id: Worker slot id for status updates.
            thread_id: Thread id for log routing.

        Returns:
            Tuple of (iterations executed, last verdict string, last grade letter).

        """
        last_verdict: str | None = None
        last_grade: str | None = None
        prior_review: str | None = None
        iterations_run = 0

        for iteration in range(MAX_REVIEW_ITERATIONS):
            # Iterations 1+ resume the impl session with the prior reviewer's
            # critique, so the implementer can fix the flagged issues before
            # the next review.
            if iteration > 0:
                if session_id is None:
                    ref = issue_ref(issue_number)
                    self._log(
                        "warning",
                        f"{ref}: cannot iterate (no session_id from initial run); "
                        "stopping review loop",
                        thread_id,
                    )
                    break
                if slot_id is not None:
                    self.status_tracker.update_slot(
                        slot_id, f"{issue_ref(issue_number)}: addressing review [R{iteration}]"
                    )
                resumed = self._resume_impl_with_feedback(
                    session_id=session_id,
                    worktree_path=worktree_path,
                    issue_number=issue_number,
                    review_text=prior_review or "",
                    prev_iteration=iteration - 1,
                    verdict=last_verdict or "NOGO",
                    state=state,
                )
                if not resumed:
                    ref = issue_ref(issue_number)
                    self._log(
                        "warning",
                        f"{ref}: resume failed at R{iteration}; stopping review loop",
                        thread_id,
                    )
                    break

            # Compute the diff and changed-files list for the reviewer.
            if slot_id is not None:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: reviewing impl [R{iteration}]"
                )
            diff_text = self._collect_diff(worktree_path, branch_name)
            files_changed = self._collect_changed_files(worktree_path, branch_name)

            review_text = self._run_impl_review(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                diff_text=diff_text,
                files_changed=files_changed,
                iteration=iteration,
                prior_review=prior_review,
            )
            self._save_review_log(issue_number, iteration, review_text)
            iterations_run = iteration + 1

            verdict = parse_review_verdict(review_text)
            last_verdict = verdict.verdict
            last_grade = verdict.grade
            self._log(
                "info",
                f"{issue_ref(issue_number)} R{iteration}: Verdict={verdict.verdict} "
                f"Grade={verdict.grade or '?'}",
                thread_id,
            )

            # A2-005: Persist review iteration progress so --resume can skip
            # already-completed iterations.  Persist BEFORE breaking out so
            # the final iteration's data is always on disk.
            self._save_review_iteration_state(issue_number, iterations_run, review_text)

            if verdict.is_go:
                ref = issue_ref(issue_number)
                self._log(
                    "info",
                    f"{ref}: GO on iteration {iteration} — review loop terminated",
                    thread_id,
                )
                break

            # Save this review for next iteration's context
            prior_review = review_text

        # A2-003: Surface AMBIGUOUS verdict distinctly so operators can triage
        # without inspecting raw log files.
        if last_verdict == "AMBIGUOUS" or (
            iterations_run == MAX_REVIEW_ITERATIONS and last_verdict not in (None, "GO")
        ):
            logger.warning(
                "#%d: review loop ended without clear GO — "
                "final verdict=%r after %d iteration(s); "
                "PR created but manual review is recommended",
                issue_number,
                last_verdict,
                iterations_run,
            )

        return iterations_run, last_verdict, last_grade

    def _resume_impl_with_feedback(
        self,
        *,
        session_id: str,
        worktree_path: Path,
        issue_number: int,
        review_text: str,
        prev_iteration: int,
        verdict: str,
        state: ImplementationState | None = None,
    ) -> bool:
        """Resume the impl session and feed reviewer feedback as the next prompt.

        Claude resumes with ``claude --resume <session_id>`` and Codex resumes
        with ``codex exec resume <session_id>``. On Claude ``CalledProcessError``
        the method distinguishes two cases (#372):

        * **Session-expired** — the ``--resume`` target no longer exists in
          the Claude CLI's session store. Detected by checking ``e.stderr`` /
          ``e.stdout`` against :data:`_SESSION_EXPIRED_PHRASES`. When detected
          sets ``state.error = "session_expired:<session_id>"`` (if *state* is
          provided) and returns ``False`` so the loop stops gracefully.
          Partial work in the worktree is preserved for later inspection.

        * **Transient / other failure** — logs at ERROR with the full stderr so
          operators see useful diagnostics rather than a bare warning.

        Args:
            session_id: Agent session id to resume.
            worktree_path: CWD for the claude invocation.
            issue_number: For log messages.
            review_text: Reviewer critique to feed back to the implementer.
            prev_iteration: Zero-based index of the review that produced the
                critique (used in the prompt and log messages).
            verdict: Last verdict string (``"NOGO"`` / ``"AMBIGUOUS"``).
            state: Optional mutable implementation state; updated in-place
                when session expiry is detected.

        Returns:
            ``True`` if the resume completed successfully, ``False`` otherwise.

        """
        prompt = get_impl_resume_feedback_prompt(
            issue_number=issue_number,
            prev_iteration=prev_iteration,
            verdict=verdict,
            review_text=review_text,
        )
        if is_codex(self.options.agent):
            try:
                result = resume_codex_session(
                    session_id,
                    prompt,
                    cwd=worktree_path,
                    timeout=implementer_claude_timeout(),
                )
                log_file = (
                    self.state_dir / f"codex-feedback-{issue_number}-r{prev_iteration + 1}.log"
                )
                log_file.write_text(result.stdout or "")
                return True
            except subprocess.CalledProcessError as e:
                logger.error(
                    "#%d: Codex failed to address R%d feedback (exit=%d): %s",
                    issue_number,
                    prev_iteration + 1,
                    e.returncode,
                    (e.stderr or e.stdout or "")[:500],
                )
                return False
            except subprocess.TimeoutExpired:
                logger.error(
                    "#%d: Codex timed out addressing R%d feedback",
                    issue_number,
                    prev_iteration + 1,
                )
                return False

        try:
            run(
                [
                    "claude",
                    "--resume",
                    session_id,
                    prompt,
                    "--print",
                    "--model",
                    implementer_model(),
                    "--permission-mode",
                    "dontAsk",
                    "--allowedTools",
                    "Read,Write,Edit,Glob,Grep,Bash",
                ],
                cwd=worktree_path,
                timeout=implementer_claude_timeout(),
            )
            return True
        except subprocess.CalledProcessError as e:
            # Distinguish session-expired from generic transient failures.
            stderr = (e.stderr or "").lower()
            stdout = (e.stdout or "").lower()
            combined = stderr + stdout
            if any(phrase in combined for phrase in _SESSION_EXPIRED_PHRASES):
                # Session pruned — partial work may still be committable;
                # don't treat this as an unrecoverable failure.
                error_tag = f"session_expired:{session_id}"
                logger.warning(
                    "#%d: impl session %r expired before R%d; "
                    "stopping review loop (partial work preserved)",
                    issue_number,
                    session_id,
                    prev_iteration + 1,
                )
                if state is not None:
                    with self.state_lock:
                        state.error = error_tag
                    self._save_state(state)
            else:
                # Unknown / transient error — log at ERROR with full stderr so
                # operators can diagnose it.
                logger.error(
                    "#%d: failed to resume impl session for R%d (exit=%d): %s",
                    issue_number,
                    prev_iteration + 1,
                    e.returncode,
                    (e.stderr or e.stdout or "")[:500],
                )
            return False
        except Exception as e:  # broad: resume is best-effort, never crash the loop
            logger.error(
                "#%d: unexpected error resuming impl session for R%d: %s",
                issue_number,
                prev_iteration + 1,
                e,
            )
            return False

    def _run_impl_review(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        diff_text: str,
        files_changed: str,
        iteration: int,
        prior_review: str | None,
    ) -> str:
        """Run a fresh-session reviewer against the current impl diff.

        Uses ``reviewer_model()`` (Sonnet by default) per the per-phase model
        selection in :mod:`hephaestus.automation.claude_models`. On reviewer
        call failure, returns a synthetic NoGo so the loop fails safe.
        """
        prompt = get_impl_loop_review_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            diff_text=diff_text,
            files_changed=files_changed,
            iteration=iteration,
            prior_review=prior_review,
        )
        try:
            if is_codex(self.options.agent):
                result = run_codex_text(
                    prompt,
                    cwd=self.repo_root,
                    timeout=600,
                    sandbox="read-only",
                )
                output = (result.stdout or "").strip()
                if not output:
                    raise RuntimeError("reviewer returned empty output")
                return output

            env = os.environ.copy()
            env["CLAUDECODE"] = ""
            result = subprocess.run(
                [
                    "claude",
                    "--print",
                    "--model",
                    reviewer_model(),
                    "--output-format",
                    "text",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                check=True,
                timeout=600,
                env=env,
            )
            output = (result.stdout or "").strip()
            if not output:
                raise RuntimeError("reviewer returned empty output")
            return output
        except Exception as e:
            logger.error(
                "#%s R%s: impl reviewer call failed: %s; treating as NOGO so the loop continues",
                issue_number,
                iteration,
                e,
            )
            return (
                f"Reviewer invocation failed at iteration {iteration}: {e}\n\n"
                "Grade: F\nVerdict: NOGO\n"
            )

    def _collect_diff(self, worktree_path: Path, branch_name: str) -> str:
        """Return the cumulative diff of *branch_name* against ``origin/main``.

        Falls back to ``HEAD~1..HEAD`` if origin/main is unavailable. Truncates
        to ~200KB to keep the reviewer prompt manageable.
        """
        try:
            result = run(
                ["git", "diff", "origin/main...HEAD"],
                cwd=worktree_path,
                capture_output=True,
                check=False,
                timeout=60,
            )
            diff = result.stdout or ""
            if not diff.strip():
                fb = run(
                    ["git", "diff", "HEAD~1..HEAD"],
                    cwd=worktree_path,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                diff = fb.stdout or ""
        except Exception as e:
            logger.warning("diff collection failed for %s: %s", branch_name, e)
            return ""

        max_chars = 200_000
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n\n[... diff truncated at {max_chars} chars ...]\n"
        return diff

    def _collect_changed_files(self, worktree_path: Path, branch_name: str) -> str:
        """Return a newline-separated list of changed files vs ``origin/main``."""
        try:
            result = run(
                ["git", "diff", "--name-only", "origin/main...HEAD"],
                cwd=worktree_path,
                capture_output=True,
                check=False,
                timeout=30,
            )
            files = (result.stdout or "").strip()
            if files:
                return files
            fb = run(
                ["git", "diff", "--name-only", "HEAD~1..HEAD"],
                cwd=worktree_path,
                capture_output=True,
                check=False,
                timeout=30,
            )
            return (fb.stdout or "").strip()
        except Exception as e:
            logger.warning("changed-files collection failed for %s: %s", branch_name, e)
            return ""

    def _save_review_log(self, issue_number: int, iteration: int, review_text: str) -> None:
        """Persist a per-iteration review log for later inspection."""
        try:
            log_file = self.state_dir / f"review-{issue_number}-r{iteration}.log"
            log_file.write_text(review_text)
        except Exception as e:
            logger.warning("#%s: failed to save review log r%s: %s", issue_number, iteration, e)

    def _save_review_iteration_state(
        self, issue_number: int, iterations_run: int, prior_review: str
    ) -> None:
        """Persist review loop progress for ``--resume`` continuity (A2-005).

        Writes two files per issue:

        * ``review-iter-{N}.json`` — JSON with ``iterations_run`` so resume
          can skip already-completed iterations.
        * ``review-prior-{N}.txt`` — the last reviewer critique so it can be
          reloaded as ``prior_review`` on resume.

        Failures are logged and swallowed — persistence is best-effort.
        """
        try:
            iter_file = self.state_dir / f"review-iter-{issue_number}.json"
            iter_file.write_text(json.dumps({"iterations_run": iterations_run}))
        except Exception as e:
            logger.warning("#%d: failed to persist review iteration count: %s", issue_number, e)
        try:
            prior_file = self.state_dir / f"review-prior-{issue_number}.txt"
            prior_file.write_text(prior_review)
        except Exception as e:
            logger.warning("#%d: failed to persist prior review text: %s", issue_number, e)

    def _load_review_iteration_state(self, issue_number: int) -> tuple[int, str | None]:
        """Load persisted review iteration progress for ``--resume`` (A2-005).

        Returns:
            Tuple of ``(iterations_run, prior_review_text)`` loaded from disk.
            Returns ``(0, None)`` if no persisted state exists.

        """
        iterations_run = 0
        prior_review: str | None = None
        try:
            iter_file = self.state_dir / f"review-iter-{issue_number}.json"
            if iter_file.exists():
                data = json.loads(iter_file.read_text())
                iterations_run = int(data.get("iterations_run", 0))
        except Exception as e:
            logger.warning(
                "#%d: failed to load persisted review iteration count: %s", issue_number, e
            )
        try:
            prior_file = self.state_dir / f"review-prior-{issue_number}.txt"
            if prior_file.exists():
                prior_review = prior_file.read_text()
        except Exception as e:
            logger.warning("#%d: failed to load persisted prior review text: %s", issue_number, e)
        return iterations_run, prior_review

    def _run_tests_in_worktree(self, worktree_path: Path, issue_number: int) -> bool:
        """Run the unit test suite inside the worktree as a pre-PR gate (A2-004).

        Invokes ``pixi run pytest tests/unit -q --tb=short`` with a generous
        timeout. On non-zero exit, logs a warning and returns ``False`` so the
        caller can decide whether to block the PR or log-and-continue.

        Args:
            worktree_path: Path to the git worktree where the tests are run.
            issue_number: For log messages.

        Returns:
            ``True`` if all tests pass, ``False`` on failure or timeout.

        """
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

    def _run_claude_code(
        self, issue_number: int, worktree_path: Path, prompt: str, slot_id: int | None = None
    ) -> str | None:
        """Run the selected implementation agent in a worktree.

        Args:
            issue_number: Issue number
            worktree_path: Path to worktree
            prompt: Implementation prompt
            slot_id: Worker slot ID for status updates

        Returns:
            Session ID if captured, None otherwise

        """
        if self.options.dry_run:
            logger.info("[DRY RUN] Would run %s for issue #%s", self.options.agent, issue_number)
            return None

        self.state_dir.mkdir(parents=True, exist_ok=True)

        if is_codex(self.options.agent):
            return self._run_codex_code(issue_number, worktree_path, prompt)

        return self._run_claude_impl_session(issue_number, worktree_path, prompt)

    def _run_claude_impl_session(
        self, issue_number: int, worktree_path: Path, prompt: str
    ) -> str | None:
        """Run Claude implementation prompt and return its session id."""
        prompt_file = worktree_path / f".claude-prompt-{issue_number}.md"
        prompt_file.write_text(prompt)

        githash = os.environ.get("HEPH_TRUNK_GITHASH", "unknown")
        repo_slug = get_repo_slug(self.repo_root)

        try:
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_IMPLEMENTER,
                githash=githash,
                prompt=prompt,
                model=implementer_model(),
                cwd=worktree_path,
                timeout=implementer_claude_timeout(),
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
            )
            result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=stdout, stderr=""
            )
            # Parse session_id from JSON output
            try:
                data = json.loads(result.stdout)

                # The CLI sometimes returns exit 0 with ``is_error: true`` in
                # JSON (e.g. usage caps in some channels). Treat that as a
                # failure so the orchestrator can wait/retry instead of
                # silently logging a useless session_id.
                if isinstance(data, dict) and data.get("is_error"):
                    err_text = str(data.get("result") or "")
                    log_file = self.state_dir / f"claude-{issue_number}.log"
                    log_file.write_text(result.stdout or "")
                    reset_epoch = _claude_quota_reset_epoch(err_text)
                    if reset_epoch is not None and reset_epoch > 0:
                        logger.warning(
                            "Claude usage cap hit for issue #%s; waiting for reset", issue_number
                        )
                        wait_until(reset_epoch)
                    raise RuntimeError(f"Claude Code failed: {err_text or 'is_error=true'}")

                session_id = data.get("session_id")

                # Save successful output to log file
                log_file = self.state_dir / f"claude-{issue_number}.log"
                log_file.write_text(result.stdout or "")

                return cast(str | None, session_id)
            except (json.JSONDecodeError, AttributeError):
                logger.warning("Could not parse session_id for issue #%s", issue_number)
                logger.debug("Claude stdout: %s", result.stdout[:500])

                # Save output even if JSON parsing failed
                log_file = self.state_dir / f"claude-{issue_number}.log"
                log_file.write_text(result.stdout or "")

                return None
        except subprocess.CalledProcessError as e:
            logger.error("Claude Code failed for issue #%s", issue_number)
            logger.error("Exit code: %s", e.returncode)
            if e.stdout:
                logger.error("Stdout: %s", e.stdout[:1000])
            if e.stderr:
                logger.error("Stderr: %s", e.stderr[:1000])

            # Save failure output to log file
            log_file = self.state_dir / f"claude-{issue_number}.log"
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            output = f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            log_file.write_text(output)

            # If the failure was a quota cap, block until reset rather than
            # letting the orchestrator burn through every remaining issue in
            # seconds. The Claude CLI puts its 429 message in stdout JSON.
            reset_epoch = _claude_quota_reset_epoch(stderr, stdout)
            if reset_epoch is not None and reset_epoch > 0:
                logger.warning(
                    "Claude usage cap hit for issue #%s; waiting for reset", issue_number
                )
                wait_until(reset_epoch)

            raise RuntimeError(f"Claude Code failed: {e.stderr or e.stdout}") from e
        except subprocess.TimeoutExpired as e:
            # Save timeout info to log file
            log_file = self.state_dir / f"claude-{issue_number}.log"
            log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")

            raise RuntimeError("Claude Code timed out") from e
        finally:
            # Clean up temp file
            with contextlib.suppress(Exception):
                prompt_file.unlink()

    def _run_codex_code(self, issue_number: int, worktree_path: Path, prompt: str) -> str | None:
        """Run Codex implementation prompt in a worktree."""
        log_file = self.state_dir / f"codex-{issue_number}.log"
        try:
            result = run_codex_session(
                prompt,
                cwd=worktree_path,
                timeout=implementer_claude_timeout(),
                sandbox="workspace-write",
            )
            log_file.write_text(result.stdout or "")
            return result.session_id
        except subprocess.CalledProcessError as e:
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            output = f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            log_file.write_text(output)
            reset_epoch = _claude_quota_reset_epoch(stderr, stdout)
            if reset_epoch is not None and reset_epoch > 0:
                logger.warning("Codex usage cap hit for issue #%s; waiting for reset", issue_number)
                wait_until(reset_epoch)
            raise RuntimeError(f"Codex failed: {stderr or stdout}") from e
        except subprocess.TimeoutExpired as e:
            log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
            raise RuntimeError("Codex timed out") from e

    def _commit_changes(self, issue_number: int, worktree_path: Path) -> None:
        """Commit changes in worktree."""
        commit_changes(issue_number, worktree_path)

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
        )

    def _create_pr(self, issue_number: int, branch_name: str) -> int:
        """Create pull request for issue."""
        return create_pr(issue_number, branch_name, self.options.auto_merge)

    def _get_or_create_state(self, issue_number: int) -> ImplementationState:
        """Get or create implementation state for an issue."""
        with self.state_lock:
            if issue_number not in self.states:
                self.states[issue_number] = ImplementationState(issue_number=issue_number)
            return self.states[issue_number]

    def _get_state(self, issue_number: int) -> ImplementationState | None:
        """Get implementation state for an issue."""
        with self.state_lock:
            return self.states.get(issue_number)

    def _save_state(self, state: ImplementationState) -> None:
        """Save implementation state to disk."""
        from .github_api import write_secure

        state_file = self.state_dir / f"issue-{state.issue_number}.json"
        # Use write_secure for atomic writes
        write_secure(state_file, state.model_dump_json(indent=2))

    def _load_state(self) -> None:
        """Load all implementation states from disk."""
        for state_file in self.state_dir.glob("issue-*.json"):
            try:
                with open(state_file) as f:
                    state = ImplementationState.model_validate_json(f.read())
                    with self.state_lock:
                        self.states[state.issue_number] = state
                logger.info("Loaded state for issue #%s", state.issue_number)
            except (json.JSONDecodeError, ValueError, OSError) as e:
                logger.error("Failed to load state from %s: %s", state_file, e)

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print implementation summary."""
        import sys

        total = len(results)
        successful = sum(1 for r in results.values() if r.success)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("Implementation Summary")
        logger.info("=" * 60)
        logger.info("Total issues: %s", total)
        logger.info("Successful: %s", successful)
        logger.info("Failed: %s", failed)

        if successful > 0:
            logger.info("\nSuccessful PRs:")
            for issue_num, result in results.items():
                if result.success and result.pr_number:
                    logger.info("  #%s: PR #%s", issue_num, result.pr_number)

        if failed > 0:
            logger.info("\nFailed issues:")
            for issue_num, result in results.items():
                if not result.success:
                    logger.info("  #%s: %s", issue_num, result.error)

        preserved = self.worktree_manager.preserved
        if preserved:
            issue_nums = [n for n, _ in preserved]
            script = sys.argv[0]
            issues_arg = " ".join(str(n) for n in issue_nums)
            logger.info("\nPreserved worktrees (contain uncommitted changes):")
            for issue_num, path in preserved:
                logger.info("  #%s: %s", issue_num, path)
            logger.info("\nRerun these issues after inspecting/cleaning the worktrees:")
            logger.info("  %s --issues %s --resume", script, issues_arg)
            logger.info("To discard them instead:")
            for _, path in preserved:
                logger.info("  git worktree remove --force %s", path)


def _setup_logging(verbose: bool = False, log_dir: Path | None = None) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging
        log_dir: Optional directory to write log files

    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "run.log", mode="a")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        logging.getLogger().addHandler(fh)


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments for the implementer CLI."""
    parser = argparse.ArgumentParser(
        description="Bulk implement GitHub issues using Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Implement all open issues (no arguments needed)
  %(prog)s

  # Implement all issues in an epic
  %(prog)s --epic 123

  # Implement specific issues
  %(prog)s --issues 595 596 597

  # Analyze dependencies without implementing
  %(prog)s --epic 123 --analyze

  # Resume previous implementation
  %(prog)s --epic 123 --resume

  # Health check
  %(prog)s --health-check

  # Dry run
  %(prog)s --issues 595 --dry-run
        """,
    )

    parser.add_argument(
        "--epic",
        type=int,
        help="Epic issue number containing sub-issues",
    )
    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        help="Specific issue numbers to implement (alternative to --epic)",
    )
    add_agent_argument(parser)
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze dependencies without implementing",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Run health check of dependencies and environment",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume previous implementation from saved state",
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
        "--no-skip-closed",
        action="store_true",
        help="Implement closed issues (default: skip closed issues)",
    )
    parser.add_argument(
        "--no-auto-merge",
        action="store_true",
        help="Don't enable auto-merge on created PRs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually doing it",
    )
    parser.add_argument(
        "--no-learn",
        action="store_true",
        help="Disable /learn after implementation (enabled by default)",
    )
    parser.add_argument(
        "--no-follow-up",
        action="store_true",
        help="Disable automatic filing of follow-up issues (enabled by default)",
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

    args = parser.parse_args()

    if args.epic and args.issues:
        parser.error("Cannot specify both --epic and --issues")

    return args


def main() -> int:
    """Execute the issue implementation workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt

    """
    args = _parse_args()

    state_dir = get_repo_root() / ".issue_implementer"
    _setup_logging(args.verbose, log_dir=state_dir)

    log = logging.getLogger(__name__)

    # Auto-discover all open issues when neither --issues nor --epic is given
    if not args.health_check and not args.epic and not args.issues:
        discovered = gh_list_open_issues()
        log.info(
            "No --issues/--epic given; discovered %s open issues: %s", len(discovered), discovered
        )
        args.issues = discovered

    options = ImplementerOptions(
        epic_number=args.epic or 0,
        issues=args.issues or [],
        agent=args.agent,
        analyze_only=args.analyze,
        health_check=args.health_check,
        resume=args.resume,
        max_workers=args.max_workers,
        skip_closed=not args.no_skip_closed,
        auto_merge=not args.no_auto_merge,
        dry_run=args.dry_run,
        enable_learn=not args.no_learn,
        enable_follow_up=not args.no_follow_up,
        enable_ui=not args.no_ui,
    )

    if args.health_check:
        log.info("Running health check")
    elif args.issues:
        log.info("Starting implementation of issues: %s", args.issues)
    else:
        log.info("Starting implementation of epic #%s", args.epic)

    from hephaestus.utils.terminal import terminal_guard

    with terminal_guard():
        try:
            implementer = IssueImplementer(options)
            results = implementer.run()

            if not args.health_check and not args.analyze:
                failed = [num for num, result in results.items() if not result.success]
                if failed:
                    log.error("Failed to implement %s issue(s): %s", len(failed), failed)
                    return 1

            log.info("Complete")
            return 0
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
