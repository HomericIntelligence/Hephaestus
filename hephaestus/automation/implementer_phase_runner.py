"""Per-issue 6-phase pipeline runner for :class:`IssueImplementer`.

Extracted from :mod:`hephaestus.automation.implementer` as part of the #597
decomposition. The runner owns the body of ``_implement_issue`` and all of
its phase helpers (plan / impl / review-loop / test / PR / follow-up /
learn). It does NOT own:

* the ``states`` dict + lock — that lives on
  :class:`~hephaestus.automation.implementer_state.ImplementationStateManager`.
* the end-of-run summary — that lives on
  :class:`~hephaestus.automation.implementer_summary.ImplementationSummaryPrinter`.

The runner keeps a back-reference to the parent ``IssueImplementer`` so
that cross-method dispatch can flow back through the coordinator's
thin shims. That preserves the test-patch contract:
``patch.object(impl, "_has_plan", ...)`` still intercepts every callsite,
including the ones inside ``_implement_issue`` that were moved here.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from hephaestus.agents.runtime import (
    is_codex,
    resume_codex_session,
    run_codex_session,
    run_codex_text,
    session_agent_matches,
)
from hephaestus.github.rate_limit import wait_until

from .claude_invoke import (
    SESSION_EXPIRED_PHRASES,
    parse_review_verdict,
)
from .claude_models import implementer_model, reviewer_model
from .claude_timeouts import implementer_claude_timeout
from .follow_up import parse_follow_up_items, run_follow_up_issues
from .git_utils import issue_ref, pr_ref, run
from .learn import learn_needs_rerun, run_learn
from .models import (
    ImplementationPhase,
    ImplementationState,
    WorkerResult,
)
from .pr_manager import ensure_pr_created
from .prompts import (
    get_impl_loop_review_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)

# NOTE: ``is_plan_review_approved``, ``fetch_issue_info``,
# ``invoke_claude_with_session``, ``get_repo_slug``, ``current_trunk_githash``,
# and ``AGENT_IMPLEMENTER`` are deliberately NOT imported here. Existing tests
# patch them at ``hephaestus.automation.implementer.X`` so the call sites
# below look them up dynamically through that module via
# :meth:`._impl_module`. This preserves the test-patch contract after the
# #597 extraction.

if TYPE_CHECKING:
    from .implementer import IssueImplementer

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 3


def _claude_quota_reset_epoch(*texts: str) -> int | None:
    """Find a quota-reset epoch across one or more output streams.

    Mirrors the helper in :mod:`hephaestus.automation.implementer`. Kept
    local to the runner so the agent-call paths don't import back through
    the coordinator module.
    """
    from hephaestus.github.rate_limit import detect_claude_usage_cap, detect_rate_limit

    for text in texts:
        if not text:
            continue
        epoch = detect_rate_limit(text)
        if epoch is not None:
            return epoch
        epoch = detect_claude_usage_cap(text)
        if epoch is not None:
            return epoch
    return None


class ImplementationPhaseRunner:
    """Runs the per-issue implementation pipeline for one ``IssueImplementer``.

    The runner is constructed by :class:`IssueImplementer` and keeps a
    back-reference to it so cross-method dispatch (``_has_plan``,
    ``_save_state``, ``_run_claude_code``, …) can flow back through the
    coordinator. That keeps existing ``patch.object(impl, "_method")``
    test idioms working unchanged.
    """

    def __init__(self, impl: IssueImplementer) -> None:
        """Initialize the runner.

        Args:
            impl: Parent ``IssueImplementer``. Held by reference; the
                runner reads ``impl.options``, ``impl.state_dir``,
                ``impl.repo_root``, ``impl.worktree_manager``,
                ``impl.status_tracker``, ``impl.state_mgr``, and the
                ``_log`` / ``_get_state`` / ``_get_or_create_state`` /
                ``_save_state`` helper methods from it.

        """
        self.impl = impl

    # ------------------------------------------------------------------
    # Convenience accessors — keep method bodies readable without
    # rewriting them.
    # ------------------------------------------------------------------

    @property
    def options(self) -> Any:
        """Return the parent ImplementerOptions."""
        return self.impl.options

    @property
    def state_dir(self) -> Path:
        """Return the state directory used for on-disk artifacts."""
        return self.impl.state_dir

    @property
    def repo_root(self) -> Path:
        """Return the repository root used as default CWD."""
        return self.impl.repo_root

    @property
    def status_tracker(self) -> Any:
        """Return the shared :class:`StatusTracker`."""
        return self.impl.status_tracker

    @property
    def worktree_manager(self) -> Any:
        """Return the shared :class:`WorktreeManager`."""
        return self.impl.worktree_manager

    @property
    def state_lock(self) -> threading.Lock:
        """Return the lock guarding the state manager's in-memory dict."""
        return self.impl.state_mgr.lock

    @property
    def _impl_module(self) -> Any:
        """Return the ``hephaestus.automation.implementer`` module.

        Used for dynamic lookup of patchable symbols (``is_plan_review_approved``,
        ``fetch_issue_info``, ``invoke_claude_with_session``, ``get_repo_slug``,
        ``current_trunk_githash``, ``AGENT_IMPLEMENTER``) so that tests which
        ``patch("hephaestus.automation.implementer.X", ...)`` keep working
        after the #597 extraction moved the call sites out to this module.
        """
        from . import implementer as _impl_mod

        return _impl_mod

    # ------------------------------------------------------------------
    # Top-level per-issue pipeline
    # ------------------------------------------------------------------

    def _implement_issue(self, issue_number: int) -> WorkerResult:  # noqa: C901  # orchestration with many retry/outcome paths
        """Implement a single issue.

        Args:
            issue_number: Issue number to implement

        Returns:
            WorkerResult

        """
        impl = self.impl
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
            impl._log("info", f"Starting issue {issue_ref(issue_number)}", thread_id)

            # Initialize state
            state = impl._get_or_create_state(issue_number)

            branch_name = f"{issue_number}-auto-impl"

            # In dry-run mode skip all real side-effects (worktree creation,
            # Claude calls, PR creation).  This guard must come BEFORE
            # create_worktree() so --dry-run never leaves real build/.worktrees/
            # directories or branches behind (#371).
            if self.options.dry_run:
                impl._log(
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

            # Skip implementation entirely when an open PR already exists for
            # this issue. Re-running the agent would clobber in-flight work;
            # the open PR is handled by the later review-prs / address-review /
            # drive-green phases. Checked BEFORE create_worktree() so the skip
            # path costs nothing. Looked up via _impl_module so tests can patch
            # ``hephaestus.automation.implementer.find_pr_for_issue``.
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Checking for existing PR"
            )
            existing_pr = self._impl_module.find_pr_for_issue(issue_number)
            if existing_pr is not None:
                impl._log(
                    "info",
                    f"Issue #{issue_number}: open PR {pr_ref(existing_pr)} already exists — "
                    f"skipping implementation (handled by later phases)",
                    thread_id,
                )
                with self.state_lock:
                    state.phase = ImplementationPhase.CREATING_PR
                impl._save_state(state)
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    pr_number=existing_pr,
                    branch_name=branch_name,
                    already_has_pr=True,
                )

            # Create worktree (only in non-dry-run mode)
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Creating worktree"
            )
            worktree_path = self.worktree_manager.create_worktree(issue_number, branch_name)

            with self.state_lock:
                state.worktree_path = str(worktree_path)
                state.branch_name = branch_name
            impl._save_state(state)

            # Check for existing plan
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Checking plan")
            if not impl._has_plan(issue_number):
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: Generating plan"
                )
                impl._log("info", f"Issue #{issue_number} has no plan, generating...", thread_id)
                with self.state_lock:
                    state.phase = ImplementationPhase.PLANNING
                impl._save_state(state)
                impl._generate_plan(issue_number)

            # Gate on APPROVED plan-review verdict (#551). The legacy
            # ``_has_plan`` check above only verifies a plan comment EXISTS;
            # it does not look at the plan-reviewer's verdict, so a BLOCK
            # or REVISE plan (or a NOGO-exhausted plan that still starts
            # with "# Implementation Plan", see planner.py:692-700) used to
            # be implemented just like an APPROVED one. We now defer the
            # issue when the latest plan-review is anything other than
            # APPROVED, so the next loop's plan-review phase can re-evaluate
            # after the planner amends.
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Checking plan-review verdict"
            )
            if not self._impl_module.is_plan_review_approved(issue_number):
                impl._log(
                    "info",
                    f"Issue #{issue_number}: latest plan-review verdict is not "
                    f"APPROVED — deferring implementation until next loop",
                    thread_id,
                )
                with self.state_lock:
                    state.phase = ImplementationPhase.WAITING_FOR_PLAN_REVIEW
                impl._save_state(state)
                self.status_tracker.update_slot(
                    slot_id,
                    f"{issue_ref(issue_number)}: Waiting for APPROVED plan-review",
                )
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    branch_name=branch_name,
                    worktree_path=str(worktree_path),
                    plan_review_not_approved=True,
                )

            # Fetch issue info for context
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Fetching issue")
            with self.state_lock:
                state.phase = ImplementationPhase.IMPLEMENTING
            impl._save_state(state)

            # Run the selected implementation agent
            issue = self._impl_module.fetch_issue_info(issue_number)
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Running {self.options.agent}"
            )
            session_id = impl._run_claude_code(
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
            impl._save_state(state)

            # Strict review loop re-uses the selected agent session when a
            # session id was captured. Reviewer calls are always fresh so
            # their judgment is unbiased.
            with self.state_lock:
                state.phase = ImplementationPhase.REVIEWING
            impl._save_state(state)
            iterations, last_verdict, last_grade = impl._run_impl_review_loop(
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
            impl._save_state(state)

            # Verify commit, push, PR creation; then run /learn and follow-ups.
            pr_number = impl._finalize_pr(issue_number, branch_name, worktree_path, state, slot_id)
            impl._run_post_pr_followup(issue_number, worktree_path, state, slot_id)

            impl._log("info", f"Issue #{issue_number} completed: PR {pr_ref(pr_number)}", thread_id)

            return WorkerResult(
                issue_number=issue_number,
                success=True,
                pr_number=pr_number,
                branch_name=branch_name,
                worktree_path=str(worktree_path),
            )

        except subprocess.TimeoutExpired as e:
            error_msg = f"Timeout: {' '.join(e.cmd[:3])} exceeded {e.timeout}s"
            impl._log("error", error_msg, thread_id)

            # Show failure in UI before releasing slot
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
            )

            err_state = impl._get_state(issue_number)
            if err_state:
                with self.state_lock:
                    err_state.phase = ImplementationPhase.FAILED
                    err_state.error = error_msg
                    err_state.attempts += 1
                impl._save_state(err_state)

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=error_msg,
            )

        except subprocess.CalledProcessError as e:
            error_msg = f"Command failed (exit {e.returncode}): {' '.join(e.cmd[:3])}"
            impl._log("error", error_msg, thread_id)
            if e.stderr:
                impl._log("error", f"stderr: {e.stderr[:300]}", thread_id)

            # Show failure in UI before releasing slot
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
            )

            err_state = impl._get_state(issue_number)
            if err_state:
                with self.state_lock:
                    err_state.phase = ImplementationPhase.FAILED
                    err_state.error = str(e)
                    err_state.attempts += 1
                impl._save_state(err_state)

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )

        except RuntimeError as e:
            impl._log("error", f"Runtime error: {e}", thread_id)

            # Show failure in UI before releasing slot
            error_msg = str(e)[:80]
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
            )

            err_state = impl._get_state(issue_number)
            if err_state:
                with self.state_lock:
                    err_state.phase = ImplementationPhase.FAILED
                    err_state.error = str(e)
                    err_state.attempts += 1
                impl._save_state(err_state)

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )

        except Exception as e:  # broad catch: top-level worker boundary, must not crash thread pool
            impl._log("error", f"Unexpected {type(e).__name__}: {e}", thread_id)

            # Show failure in UI before releasing slot
            error_msg = str(e)[:80]
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
            )

            err_state = impl._get_state(issue_number)
            if err_state:
                with self.state_lock:
                    err_state.phase = ImplementationPhase.FAILED
                    err_state.error = str(e)
                    err_state.attempts += 1
                impl._save_state(err_state)

            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )
        finally:
            self.status_tracker.release_slot(slot_id)

    # ------------------------------------------------------------------
    # PR finalization + post-PR followup (extracted from _implement_issue
    # for SRP; preserved here verbatim).
    # ------------------------------------------------------------------

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

    def _run_post_pr_followup(
        self,
        issue_number: int,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
    ) -> None:
        """Run /learn and file follow-up issues after the PR is created."""
        impl = self.impl
        # Learn phase (after CREATING_PR, before COMPLETED)
        can_resume_session = self._can_resume_state_session(state)
        if self.options.enable_learn and can_resume_session and state.session_id:
            if slot_id is not None:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: Running learn"
                )
            with self.state_lock:
                state.phase = ImplementationPhase.LEARN
            impl._save_state(state)
            retro_success = self._run_learn(
                state.session_id,
                worktree_path,
                issue_number,
                slot_id,
                session_agent=state.session_agent,
            )
            with self.state_lock:
                state.learn_completed = retro_success
            impl._save_state(state)

        # Follow-up issues phase (after LEARN, before COMPLETED)
        if self.options.enable_follow_up and can_resume_session and state.session_id:
            if slot_id is not None:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: Identifying follow-ups"
                )
            with self.state_lock:
                state.phase = ImplementationPhase.FOLLOW_UP_ISSUES
            impl._save_state(state)
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
        impl._save_state(state)

    # ------------------------------------------------------------------
    # Plan-presence and plan-generation
    # ------------------------------------------------------------------

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
        """Generate plan for an issue using hephaestus-plan-issues."""
        import shutil

        # Prefer the installed entry point (works in any repo)
        entry_point = shutil.which("hephaestus-plan-issues")
        if entry_point:
            run(
                [entry_point, "--issues", str(issue_number), "--agent", self.options.agent],
                timeout=600,
            )
            return

        # Fall back to python -m invocation (works when PYTHONPATH is set).
        # On failure, fall through to the legacy scripts/plan_issues.py path.
        with contextlib.suppress(subprocess.SubprocessError, OSError):
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

    # ------------------------------------------------------------------
    # Follow-up / learn helpers
    # ------------------------------------------------------------------

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
        impl = self.impl
        results: dict[int, bool] = {}

        for issue_number, state in self.impl.state_mgr.states.items():
            # Only re-run for completed issues with failed learns
            if (
                state.phase != ImplementationPhase.COMPLETED
                or state.learn_completed
                or not self._can_resume_state_session(state)
            ):
                continue

            # Check if log indicates failure
            if not impl._learn_needs_rerun(issue_number):
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
            success = impl._run_learn(
                session_id,
                worktree_path,
                issue_number,
                slot_id=None,
                session_agent=state.session_agent,
            )

            # Update and save state
            with self.state_lock:
                state.learn_completed = success
            impl._save_state(state)

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
        """Run the bounded review loop for an implementation."""
        impl = self.impl
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
                    impl._log(
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
                resumed = impl._resume_impl_with_feedback(
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
                    impl._log(
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
            diff_text = impl._collect_diff(worktree_path, branch_name)
            files_changed = impl._collect_changed_files(worktree_path, branch_name)

            review_text = impl._run_impl_review(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                diff_text=diff_text,
                files_changed=files_changed,
                iteration=iteration,
                prior_review=prior_review,
            )
            impl._save_review_log(issue_number, iteration, review_text)
            iterations_run = iteration + 1

            verdict = parse_review_verdict(review_text)
            last_verdict = verdict.verdict
            last_grade = verdict.grade
            impl._log(
                "info",
                f"{issue_ref(issue_number)} R{iteration}: Verdict={verdict.verdict} "
                f"Grade={verdict.grade or '?'}",
                thread_id,
            )

            # A2-005: Persist review iteration progress so --resume can skip
            # already-completed iterations.  Persist BEFORE breaking out so
            # the final iteration's data is always on disk.
            impl._save_review_iteration_state(issue_number, iterations_run, review_text)

            if verdict.is_go:
                ref = issue_ref(issue_number)
                impl._log(
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
        """Resume the impl session and feed reviewer feedback as the next prompt."""
        impl = self.impl
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

        # Route through the centralized helper so create/resume semantics and
        # the SESSION_EXPIRED phrase list stay in one place. The deterministic
        # UUID matches what the initial impl session was created with, so the
        # passed-in ``session_id`` (legacy ``state.session_id``) is ignored on
        # the Claude path — it's still consumed by the codex branch above.
        # ``recreate_on_resume_failure=False`` propagates the underlying error
        # so we can preserve the "stop iterating on expiry" contract.
        _impl_mod = self._impl_module
        githash = _impl_mod.current_trunk_githash(self.repo_root)
        repo_slug = _impl_mod.get_repo_slug(self.repo_root)
        try:
            _impl_mod.invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=_impl_mod.AGENT_IMPLEMENTER,
                githash=githash,
                prompt=prompt,
                model=implementer_model(),
                cwd=worktree_path,
                timeout=implementer_claude_timeout(),
                permission_mode="dontAsk",
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                recreate_on_resume_failure=False,
            )
            return True
        except subprocess.CalledProcessError as e:
            combined = ((e.stderr or "") + (e.stdout or "")).lower()
            if any(phrase in combined for phrase in SESSION_EXPIRED_PHRASES):
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
                    impl._save_state(state)
            else:
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
        """Run a fresh-session reviewer against the current impl diff."""
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
        """Return the cumulative diff of *branch_name* against ``origin/main``."""
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
        """Persist review loop progress for ``--resume`` continuity (A2-005)."""
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
        """Load persisted review iteration progress for ``--resume`` (A2-005)."""
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

    # ------------------------------------------------------------------
    # Pre-PR test gate
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Agent invocation
    # ------------------------------------------------------------------

    def _run_claude_code(
        self, issue_number: int, worktree_path: Path, prompt: str, slot_id: int | None = None
    ) -> str | None:
        """Run the selected implementation agent in a worktree."""
        if self.options.dry_run:
            logger.info("[DRY RUN] Would run %s for issue #%s", self.options.agent, issue_number)
            return None

        self.state_dir.mkdir(parents=True, exist_ok=True)

        if is_codex(self.options.agent):
            return self.impl._run_codex_code(issue_number, worktree_path, prompt)

        return self.impl._run_claude_impl_session(issue_number, worktree_path, prompt)

    def _run_claude_impl_session(
        self, issue_number: int, worktree_path: Path, prompt: str
    ) -> str | None:
        """Run Claude implementation prompt and return its session id."""
        prompt_file = worktree_path / f".claude-prompt-{issue_number}.md"
        prompt_file.write_text(prompt)

        _impl_mod = self._impl_module
        githash = _impl_mod.current_trunk_githash(self.repo_root)
        repo_slug = _impl_mod.get_repo_slug(self.repo_root)

        try:
            stdout, _ = _impl_mod.invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=_impl_mod.AGENT_IMPLEMENTER,
                githash=githash,
                prompt=prompt,
                model=implementer_model(),
                cwd=worktree_path,
                timeout=implementer_claude_timeout(),
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
            )
            result = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
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

    # ------------------------------------------------------------------
    # PR creation (thin wrappers preserved for back-compat)
    # ------------------------------------------------------------------

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
