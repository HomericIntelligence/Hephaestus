"""Per-issue 3-stage pipeline runner for :class:`IssueImplementer`.

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
from typing import TYPE_CHECKING, Any, Literal, cast

from hephaestus.agents.runtime import (
    is_codex,
    resume_codex_session,
    run_codex_session,
    run_codex_text,
    session_agent_matches,
)
from hephaestus.github.rate_limit import wait_until

from .address_review import (
    run_address_fix_session,
)
from .advise_runner import run_advise
from .claude_invoke import (
    INFRA_ERROR_REVIEW_TEXT,
    SESSION_EXPIRED_PHRASES,
    parse_review_verdict,
)
from .claude_models import advise_model, implementer_model, reviewer_model
from .claude_timeouts import advise_claude_timeout, implementer_claude_timeout
from .follow_up import parse_follow_up_items, run_follow_up_issues
from .git_utils import (
    get_repo_slug,
    is_clean_working_tree,
    issue_ref,
    pr_ref,
    run,
    sync_worktree_to_remote_branch,
)
from .github_api import (
    gh_current_login,
    gh_issue_add_labels,
    gh_pr_list_unresolved_threads,
)
from .learn import compact_session, learn_needs_rerun, run_learn
from .models import (
    PLAN_COMMENT_MARKER,
    ImplementationPhase,
    ImplementationState,
    WorkerResult,
)
from .planner_state import _comments_contain_plan
from .pr_manager import (
    commit_changes,
    enable_auto_merge_after_implementation_go,
    ensure_pr_auto_merge_deferred,
    ensure_pr_created,
    mark_pr_implementation_go,
    mark_pr_implementation_no_go,
    pr_has_implementation_state_label,
)
from .pr_reviewer import gather_impl_review_context, review_pr_inline
from .prompts import (
    get_advise_prompt_builder,
    get_dirty_reused_worktree_decision_prompt,
    get_impl_loop_review_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)
from .review_validator import validate_prior_comments_addressed
from .state_labels import STATE_SKIP

# NOTE: ``is_plan_review_go``, ``fetch_issue_info``,
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

DirtyWorktreeDecision = Literal["commit", "stash"]


def _parse_dirty_reused_worktree_decision(text: str) -> DirtyWorktreeDecision:
    """Parse an exact final-line COMMIT/STASH dirty-worktree decision."""
    lines = [line.strip().upper() for line in (text or "").splitlines() if line.strip()]
    if lines and lines[-1] == "COMMIT":
        return "commit"
    return "stash"


def _parse_dirty_worktree_decision(text: str) -> DirtyWorktreeDecision:
    """Backward-compatible wrapper for the dirty reused-worktree parser."""
    return _parse_dirty_reused_worktree_decision(text)


def _prepend_advise(advise_findings: str, prompt: str) -> str:
    """Prepend advise findings as a context block to an implementation prompt.

    Returns ``prompt`` unchanged when there are no real findings — an empty
    string or an ``advise_runner.advise_skipped`` HTML-comment marker (which
    records *why* advise produced nothing) carries no guidance worth injecting.
    """
    findings = advise_findings.strip()
    if not findings or findings.startswith("<!-- advise step skipped"):
        return prompt
    return f"## Prior Learnings from Team Knowledge Base\n\n{findings}\n\n---\n\n{prompt}"


def _is_automation_owned_thread(thread: dict[str, Any], current_login: str | None) -> bool:
    """Return True for unresolved review threads the automation may resolve on GO."""
    authors = {str(author).strip() for author in thread.get("authors", []) if str(author).strip()}
    author = (thread.get("author") or "").strip()
    if author:
        authors.add(author)
    for comment in thread.get("comments", []):
        comment_author = (comment.get("author") or "").strip()
        if comment_author:
            authors.add(comment_author)

    if current_login and current_login in authors:
        return True
    automation_bot_logins = {
        "github-actions[bot]",
        "hephaestus[bot]",
    }
    return bool(authors & automation_bot_logins)


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

        Used for dynamic lookup of patchable symbols (``is_plan_review_go``,
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

            # When an open PR already exists for this issue, skip the
            # plan/implement steps (the PR is already opened) but STILL drive it
            # through the in-loop review → address cycle so a green PR earns the
            # ``state:implementation-go`` label that drive-green requires to arm
            # auto-merge. A pre-existing PR that never gets reviewed here would
            # otherwise deadlock: implementer skips it, drive-green only reads
            # the label and refuses to merge without it. Looked up via
            # _impl_module so tests can patch
            # ``hephaestus.automation.implementer.find_pr_for_issue``.
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Checking for existing PR"
            )
            existing_pr = self._impl_module.find_pr_for_issue(issue_number)
            if existing_pr is not None:
                return self._review_existing_pr(
                    issue_number=issue_number,
                    existing_pr=existing_pr,
                    branch_name=branch_name,
                    state=state,
                    slot_id=slot_id,
                    thread_id=thread_id,
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

            # Gate on a GO plan-review verdict (#551). The legacy ``_has_plan``
            # check above only verifies a plan comment EXISTS; it does not look
            # at the plan-reviewer's verdict, so a NOGO plan (or a NOGO-exhausted
            # plan that still starts with "# Implementation Plan", see
            # planner.py:692-700) used to be implemented just like a GO one. We
            # now defer the issue when the latest plan-review is anything other
            # than GO, so the planner can re-plan and the reviewer re-evaluate.
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Checking plan-review verdict"
            )
            if not self._impl_module.is_plan_review_go(issue_number):
                impl._log(
                    "info",
                    f"Issue #{issue_number}: latest plan-review verdict is not "
                    f"GO — deferring implementation until next loop",
                    thread_id,
                )
                with self.state_lock:
                    state.phase = ImplementationPhase.WAITING_FOR_PLAN_REVIEW
                impl._save_state(state)
                self.status_tracker.update_slot(
                    slot_id,
                    f"{issue_ref(issue_number)}: Waiting for GO plan-review",
                )
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    branch_name=branch_name,
                    worktree_path=str(worktree_path),
                    plan_review_not_go=True,
                )

            # Fetch issue info for context
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Fetching issue")
            with self.state_lock:
                state.phase = ImplementationPhase.IMPLEMENTING
            impl._save_state(state)

            issue = self._impl_module.fetch_issue_info(issue_number)

            # Advise-first (#30): pull prior learnings from ProjectMnemosyne
            # before the implementation session.  For Claude agents the advise
            # prompt is sent as the *first turn* of the implementer's own
            # session (cwd=worktree_path) so the findings live in the transcript
            # and are automatically visible to the implementation turn that
            # follows via --resume.  For Codex agents the old separate-session
            # path is retained because Codex has no multi-turn session model.
            implementation_advise_findings = ""
            if self.options.enable_advise and not is_codex(self.options.agent):
                self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Advising")
                implementation_advise_findings = impl._run_advise_as_implementer_turn(
                    issue_number, issue.title, issue.body, worktree_path
                )

            # Run the selected implementation agent
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Running {self.options.agent}"
            )
            # Codex: run advise separately (returns findings text) then inject.
            codex_advise_findings = ""
            if self.options.enable_advise and is_codex(self.options.agent):
                self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Advising")
                codex_advise_findings = impl._run_advise(issue_number, issue.title, issue.body)
                implementation_advise_findings = codex_advise_findings

            session_id = impl._run_claude_code(
                issue_number,
                worktree_path,
                _prepend_advise(
                    codex_advise_findings,
                    get_implementation_prompt(
                        issue_number=issue_number,
                        issue_title=issue.title,
                        issue_body=issue.body,
                        branch_name=branch_name,
                        worktree_path=str(worktree_path),
                        repo_root=str(self.repo_root),
                    ),
                ),
                slot_id=slot_id,
            )
            with self.state_lock:
                state.session_id = session_id
                state.session_agent = self.options.agent if session_id else None
            impl._save_state(state)

            # Create the PR up-front so the in-loop reviewer (Stage 2, #28) has
            # a concrete PR to post INLINE review threads against. Verify commit,
            # push, PR creation. ``_finalize_pr`` is idempotent — ``ensure_pr_
            # created`` is a fallback that no-ops when the agent already opened
            # the PR.
            pr_number = impl._finalize_pr(issue_number, branch_name, worktree_path, state, slot_id)
            ensure_pr_auto_merge_deferred(pr_number)

            # Strict review loop now absorbs the former ``review-prs`` and
            # ``address-review`` phases: each iteration runs a FRESH reviewer
            # session that posts inline PR threads, then resumes Session 2
            # (AGENT_IMPLEMENTER) to address them, looping until GO / no
            # blocking unresolved threads. Reviewer calls are always fresh so
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
                pr_number=pr_number,
                advise_findings=implementation_advise_findings,
            )
            with self.state_lock:
                state.review_iterations = iterations
                state.last_review_verdict = last_verdict
                state.last_review_grade = last_grade
            impl._save_state(state)

            self._apply_impl_review_verdict(
                issue_number=issue_number,
                pr_number=pr_number,
                last_verdict=last_verdict,
                slot_id=slot_id,
                thread_id=thread_id,
            )

            # impl-learnings + follow-up filing stay in Session 2 (#28 §B),
            # resuming AGENT_IMPLEMENTER AFTER the loop converges.
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
            # "No changes produced" means the branch has 0 commits vs main —
            # the implementation already landed via a prior merged PR. Treat
            # this as success and apply state:skip so future loops don't
            # re-attempt the issue.
            msg = str(e)
            if "no commits vs" in msg.lower() or "no changes produced" in msg.lower():
                impl._log(
                    "info",
                    f"Issue #{issue_number}: no new commits vs main — "
                    "work already merged; applying state:skip",
                    thread_id,
                )
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: already implemented — state:skip"
                )
                with contextlib.suppress(Exception):
                    gh_issue_add_labels(issue_number, [STATE_SKIP])
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                )

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
        if (
            self.options.enable_learn
            and not state.learn_completed
            and can_resume_session
            and state.session_id
        ):
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
            if retro_success and not is_codex(self.options.agent):
                self._compact_implementer_session(issue_number, worktree_path)

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
        """Check if issue has an implementation plan.

        Delegates to :func:`planner_state._comments_contain_plan` so the
        prefix-anchored check stays in sync with the planner. Substring
        matching here previously caused the implementer to mistake a
        ``## 🔍 Plan Review`` comment (which quotes the plan body) for the
        plan itself — the same bug class fixed in #455/#468/#484 (#715).

        Note: ``_comments_contain_plan`` is a private helper but is the
        canonical implementation per its own docstring; cross-module reuse
        here is intentional to avoid a third copy of the same prefix logic.
        """
        try:
            result = run(
                ["gh", "issue", "view", str(issue_number), "--comments", "--json", "comments"],
                capture_output=True,
            )
            data = json.loads(result.stdout)
            comments = data.get("comments", [])
            return _comments_contain_plan(comments)
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
            model=implementer_model(),
        )

    def _compact_implementer_session(self, issue_number: int, worktree_path: Path) -> None:
        """Compact the implementer session after /learn (#842). Non-fatal."""
        repo_slug = get_repo_slug(self.repo_root)
        compact_session(
            repo=repo_slug,
            issue=issue_number,
            agent=self._impl_module.AGENT_IMPLEMENTER,
            cwd=worktree_path,
            model=implementer_model(),
        )

    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Search ProjectMnemosyne for prior learnings — planner's separate-session path.

        Used by the planner (and Codex) where advise runs under ``AGENT_ADVISE``
        (a distinct, cheap, read-only session) and returns text findings for the
        caller to inject into its own prompt context.  Claude implementer sessions
        use :meth:`_run_advise_as_implementer_turn` instead, which makes advise
        the *first turn* of the implementer's own session so the findings live in
        the transcript and inform the implementation turn directly.
        """
        _impl_mod = self._impl_module

        def _invoke(prompt: str) -> str:
            if is_codex(self.options.agent):
                result = run_codex_text(
                    prompt,
                    cwd=self.repo_root,
                    timeout=advise_claude_timeout(),
                    sandbox="read-only",
                )
                return (result.stdout or "").strip()
            repo_slug = _impl_mod.get_repo_slug(self.repo_root)
            stdout, _ = _impl_mod.invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=_impl_mod.AGENT_ADVISE,
                prompt=prompt,
                model=advise_model(),
                cwd=self.repo_root,
                timeout=advise_claude_timeout(),
                output_format="text",
            )
            return (stdout or "").strip()

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=_invoke,
            build_prompt=get_advise_prompt_builder(self.options.agent),
        )

    def _run_advise_as_implementer_turn(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        worktree_path: Path,
    ) -> str:
        """Send the advise prompt as the first turn of the implementer's Claude session.

        Unlike :meth:`_run_advise`, which creates a separate ``AGENT_ADVISE``
        session and returns text, this method sends the advise prompt directly to
        ``AGENT_IMPLEMENTER`` (using ``cwd=worktree_path`` so the session transcript
        is co-located with the subsequent implementation turn).  The advise findings
        live in the implementer's own transcript, so turn 2 (the implementation
        prompt) automatically inherits them via ``--resume`` — no text injection
        needed.

        Codex does not support this two-turn flow; callers must guard with
        ``is_codex`` and fall back to :meth:`_run_advise` + text injection for
        Codex agents.

        On first run ``invoke_claude_with_session`` auto-creates the session
        (``transcript.exists()`` is False).  On subsequent loops the same
        deterministic session UUID auto-resumes so the advise findings accumulate
        across iterations.

        Any failure degrades gracefully inside ``run_advise`` and returns an
        empty string, so the caller can still proceed to the implementation turn.
        """
        _impl_mod = self._impl_module
        repo_slug = _impl_mod.get_repo_slug(self.repo_root)

        # Fetch plan and plan-review from GitHub comments to give advise the full
        # context of what's been planned (same anchored selection as
        # _fetch_plan_and_review so PLAN_REVIEW comments are distinguished from
        # the PLAN comment).
        plan_text, plan_review_text = self._fetch_plan_and_review(issue_number)

        def _build_prompt_with_plan(**kw: object) -> str:
            # Claude runs with cwd=worktree_path, so pass worktree_path as the
            # relativization root.  marketplace.json lives under build/ in the main
            # repo, not under the worktree, so _relativize_path will fall back to
            # the absolute path — which is always readable regardless of cwd.
            kw["repo_root"] = str(worktree_path)
            base_prompt = get_advise_prompt_builder(self.options.agent)(**kw)
            if not plan_text and not plan_review_text:
                return base_prompt
            parts = []
            if plan_text:
                parts.append(f"## Implementation Plan\n\n{plan_text}")
            if plan_review_text:
                parts.append(f"## Plan Review\n\n{plan_review_text}")
            plan_block = "\n\n".join(parts)
            return f"{plan_block}\n\n---\n\n{base_prompt}"

        def _invoke(prompt: str) -> str:
            stdout, _ = _impl_mod.invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=_impl_mod.AGENT_IMPLEMENTER,
                prompt=prompt,
                model=implementer_model(),
                cwd=worktree_path,
                timeout=advise_claude_timeout(),
                output_format="text",
            )
            return (stdout or "").strip()

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=_invoke,
            build_prompt=_build_prompt_with_plan,
        )

    def _apply_impl_review_verdict(
        self,
        *,
        issue_number: int,
        pr_number: int,
        last_verdict: str | None,
        slot_id: int | None,
        thread_id: int | None,
    ) -> None:
        """Label a PR from the review loop's final verdict and arm auto-merge.

        Shared by both the fresh-implementation path and the existing-PR review
        path so the GO → ``mark_pr_implementation_go`` (+ auto-merge) /
        non-GO → ``mark_pr_implementation_no_go`` mapping cannot drift between
        them.

        An ``ERROR`` verdict (reviewer-infrastructure failure) or a
        ``HUMAN_BLOCKED`` verdict (review reached GO but an unresolved human
        review thread remains) applies **neither** label: the PR is not settled,
        so it must be left unlabeled for the "no go/no-go label → re-review" path
        to pick it up next loop (#911 / PR #1069). Labeling it no-go would falsely
        record a converged failure; labeling it go would arm auto-merge on a PR
        that was never reviewed (ERROR) or still has open human threads
        (HUMAN_BLOCKED).
        """
        impl = self.impl
        if last_verdict in ("ERROR", "HUMAN_BLOCKED"):
            reason = (
                "reviewer-infrastructure error"
                if last_verdict == "ERROR"
                else "unresolved human review thread(s)"
            )
            impl._log(
                "warning",
                f"{issue_ref(issue_number)}: implementation review blocked by {reason}; "
                f"leaving {pr_ref(pr_number)} unlabeled for re-review "
                "(no implementation go/no-go label, auto-merge unchanged)",
                thread_id,
            )
            return
        if last_verdict == "GO":
            mark_pr_implementation_go(pr_number)
            if self.options.auto_merge:
                if slot_id is not None:
                    self.status_tracker.update_slot(
                        slot_id, f"{pr_ref(pr_number)}: enabling auto-merge"
                    )
                enable_auto_merge_after_implementation_go(pr_number)
        else:
            mark_pr_implementation_no_go(pr_number)
            impl._log(
                "warning",
                f"{issue_ref(issue_number)}: implementation review did not reach GO; "
                f"auto-merge remains disabled for {pr_ref(pr_number)}",
                thread_id,
            )

    def _review_existing_pr(
        self,
        *,
        issue_number: int,
        existing_pr: int,
        branch_name: str,
        state: ImplementationState,
        slot_id: int | None,
        thread_id: int | None,
    ) -> WorkerResult:
        """Drive an already-open PR through the in-loop review → address cycle.

        Replaces the old "skip existing PRs entirely" shortcut. A pre-existing
        PR is reviewed (and, on NOGO, fixed by the resumed implementer session)
        so it earns the ``state:implementation-go`` label drive-green needs to
        arm auto-merge — without this it would deadlock green-but-unmergeable.

        Idempotency: a PR already carrying ``state:implementation-go`` was
        settled on a prior loop, so it short-circuits without re-reviewing
        (auto-merge arming is drive-green's job). ``state:implementation-no-go``
        is NOT terminal — a NO-GO PR failed review and re-enters the
        review→address→re-review cycle until it earns GO, so it does NOT
        short-circuit. The worktree is prepared on the PR's REAL head branch
        (resolved via ``get_pr_head_branch``), never the assumed
        ``{issue}-auto-impl`` — the PR may have been matched by body ``Closes #N``
        search and live on a differently-named branch. Anti-clobber: the worktree
        is hard-reset to ``origin/<pr-head>`` before the review loop so re-running
        never discards commits that were pushed to the PR head, and the loop runs
        with
        ``session_id=None`` (no agent edit session is started here — the address
        step inside the loop resumes the implementer's own session by
        deterministic id only when there are threads to fix).
        """
        impl = self.impl

        self.status_tracker.update_slot(
            slot_id, f"{pr_ref(existing_pr)}: Checking implementation-review label"
        )
        has_go, has_no_go = pr_has_implementation_state_label(existing_pr)
        if has_go:
            # GO is terminal: the PR passed review on a prior loop. Auto-merge
            # arming is drive-green's job, so re-reviewing here is wasted work.
            impl._log(
                "info",
                f"Issue #{issue_number}: open PR {pr_ref(existing_pr)} already "
                "implementation-review GO — skipping re-review "
                "(settled; auto-merge handled by drive-green)",
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
        if has_no_go:
            # NO-GO is NOT terminal: the PR failed review and must be
            # re-implemented + re-reviewed until it earns GO. Fall through into
            # the review→address→re-review cycle below.
            impl._log(
                "info",
                f"Issue #{issue_number}: open PR {pr_ref(existing_pr)} is "
                "implementation-review NO-GO — re-running implement + review loop "
                "to drive it toward GO",
                thread_id,
            )

        # Resolve the PR's REAL head branch — never assume ``{issue}-auto-impl``.
        # ``find_pr_for_issue`` may have matched this PR via PR-body ``Closes #N``
        # search, so its head branch can be named after a different issue (or a
        # bundle). Fetching the assumed name fails with ``git fetch ... exit 128``.
        # Fall back to the passed-in name only if the lookup fails.
        pr_branch = self._impl_module.get_pr_head_branch(existing_pr) or branch_name
        if pr_branch != branch_name:
            impl._log(
                "info",
                f"Issue #{issue_number}: {pr_ref(existing_pr)} head branch is "
                f"{pr_branch!r} (not the assumed {branch_name!r}); using the real branch",
                thread_id,
            )

        # Reuse-or-create the worktree, then hard-reset it to the PR head so the
        # reviewer sees the real PR state and any in-loop fix lands on top of it.
        self.status_tracker.update_slot(
            slot_id, f"{issue_ref(issue_number)}: Preparing worktree for existing PR"
        )
        worktree_path = self.worktree_manager.create_worktree(issue_number, pr_branch)
        # ``create_worktree`` may have REUSED a worktree another issue already
        # had checked out for ``pr_branch`` (git forbids one branch in two
        # worktrees). A reused worktree can carry uncommitted changes from that
        # other session; the ``reset --hard`` inside sync_worktree_to_remote_branch
        # would silently discard them. Only when dirty, let an agent decide
        # whether to commit (the work belongs to this branch) or stash it
        # (unrelated/uncertain) before we sync to the PR head.
        salvage_sha: str | None = None
        if not is_clean_working_tree(worktree_path):
            salvage_sha = self._resolve_dirty_reused_worktree(
                issue_number=issue_number,
                worktree_path=worktree_path,
                branch_name=pr_branch,
                thread_id=thread_id,
            )
        sync_worktree_to_remote_branch(worktree_path, pr_branch)
        if salvage_sha:
            self._restore_dirty_reused_worktree_commit_after_sync(
                issue_number=issue_number,
                worktree_path=worktree_path,
                branch_name=pr_branch,
                commit_sha=salvage_sha,
                thread_id=thread_id,
            )
            self._push_branch(pr_branch, worktree_path)

        with self.state_lock:
            state.worktree_path = str(worktree_path)
            state.branch_name = pr_branch
            state.pr_number = existing_pr
            state.phase = ImplementationPhase.REVIEWING
        impl._save_state(state)

        issue = self._impl_module.fetch_issue_info(issue_number)

        # Advise-first (#30): same two-turn pattern as the fresh-implementation path.
        # For Claude: advise as turn 1 of AGENT_IMPLEMENTER (cwd=worktree_path).
        # For Codex: run advise separately and inject the findings into the
        # review-loop context.
        implementation_advise_findings = ""
        if self.options.enable_advise and not is_codex(self.options.agent):
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Advising")
            implementation_advise_findings = impl._run_advise_as_implementer_turn(
                issue_number, issue.title, issue.body, worktree_path
            )
        elif self.options.enable_advise:
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Advising")
            implementation_advise_findings = impl._run_advise(issue_number, issue.title, issue.body)

        iterations, last_verdict, last_grade = impl._run_impl_review_loop(
            issue_number=issue_number,
            worktree_path=worktree_path,
            branch_name=pr_branch,
            issue_title=issue.title,
            issue_body=issue.body,
            session_id=None,
            slot_id=slot_id,
            thread_id=thread_id,
            state=state,
            pr_number=existing_pr,
            advise_findings=implementation_advise_findings,
        )
        with self.state_lock:
            state.review_iterations = iterations
            state.last_review_verdict = last_verdict
            state.last_review_grade = last_grade
        impl._save_state(state)

        self._apply_impl_review_verdict(
            issue_number=issue_number,
            pr_number=existing_pr,
            last_verdict=last_verdict,
            slot_id=slot_id,
            thread_id=thread_id,
        )
        impl._run_post_pr_followup(issue_number, worktree_path, state, slot_id)

        impl._log(
            "info",
            f"Issue #{issue_number}: existing PR {pr_ref(existing_pr)} review complete "
            f"(verdict={last_verdict or '?'})",
            thread_id,
        )
        return WorkerResult(
            issue_number=issue_number,
            success=True,
            pr_number=existing_pr,
            branch_name=pr_branch,
            worktree_path=str(worktree_path),
            already_has_pr=True,
        )

    def _resolve_dirty_reused_worktree(
        self,
        *,
        issue_number: int,
        worktree_path: Path,
        branch_name: str,
        thread_id: int | None,
    ) -> str | None:
        """Decide commit-vs-stash for a REUSED worktree's uncommitted changes.

        ``create_worktree`` can reuse a worktree another issue already had checked
        out for ``branch_name``; that worktree may carry uncommitted work the
        upcoming ``reset --hard`` would discard. Rather than guess, a bounded agent
        turn inspects the diff and decides:

        - **commit** — the changes belong to ``branch_name`` (same feature/PR), so
          commit them onto the branch so the reset preserves them as history.
        - **stash** — the changes are unrelated or their ownership is unclear; stash
          them so they survive the reset without polluting the PR.

        Any decision failure falls back to ``git stash`` (the safe default —
        preserves the work without committing it to the wrong branch). A failed
        stash raises so the caller never reaches the destructive reset.

        Returns:
            The SHA of a salvage commit to replay after remote sync, or ``None``
            when changes were stashed.

        """
        status = run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            check=False,
        )
        diff = run(
            ["git", "diff", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            check=False,
        )

        decision: DirtyWorktreeDecision = "stash"
        try:
            prompt = get_dirty_reused_worktree_decision_prompt(
                branch_name=branch_name,
                status_text=status.stdout or "",
                diff_text=diff.stdout or "",
            )
            if is_codex(self.options.agent):
                result = run_codex_text(
                    prompt,
                    cwd=worktree_path,
                    timeout=advise_claude_timeout(),
                    sandbox="read-only",
                )
                output = result.stdout or ""
            else:
                _impl_mod = self._impl_module
                repo_slug = _impl_mod.get_repo_slug(self.repo_root)
                output, _ = _impl_mod.invoke_claude_with_session(
                    repo=repo_slug,
                    issue=issue_number,
                    agent=_impl_mod.AGENT_ADVISE,
                    prompt=prompt,
                    model=advise_model(),
                    cwd=worktree_path,
                    timeout=advise_claude_timeout(),
                    output_format="text",
                )
            decision = _parse_dirty_reused_worktree_decision(output)
        except Exception as e:
            self.impl._log(
                "warning",
                f"Issue #{issue_number}: dirty-worktree decision failed ({e}); defaulting to stash",
                thread_id,
            )

        if decision == "commit":
            try:
                return self._commit_dirty_reused_worktree(
                    issue_number=issue_number,
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                    thread_id=thread_id,
                )
            except Exception as e:
                self.impl._log(
                    "warning",
                    f"Issue #{issue_number}: dirty-worktree COMMIT preservation failed ({e}); "
                    "defaulting to stash before reset",
                    thread_id,
                )

        self._stash_dirty_reused_worktree(
            issue_number=issue_number,
            worktree_path=worktree_path,
            thread_id=thread_id,
        )
        return None

    def _commit_dirty_reused_worktree(
        self,
        *,
        issue_number: int,
        worktree_path: Path,
        branch_name: str,
        thread_id: int | None,
    ) -> str:
        """Commit dirty reused-worktree changes and return the salvage SHA."""
        self.impl._log(
            "info",
            f"Issue #{issue_number}: committing reused-worktree changes on "
            f"{branch_name} before sync",
            thread_id,
        )
        run(["git", "add", "-A"], cwd=worktree_path, check=True)
        run(
            [
                "git",
                "commit",
                "-S",
                "-m",
                f"chore: preserve reused worktree changes on {branch_name}",
            ],
            cwd=worktree_path,
            check=True,
        )
        result = run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            check=True,
        )
        commit_sha = (result.stdout or "").strip()
        if not commit_sha:
            raise RuntimeError("git rev-parse HEAD returned an empty commit SHA")
        return commit_sha

    def _stash_dirty_reused_worktree(
        self,
        *,
        issue_number: int,
        worktree_path: Path,
        thread_id: int | None,
    ) -> None:
        """Stash dirty reused-worktree changes before a destructive sync."""
        self.impl._log(
            "info",
            f"Issue #{issue_number}: stashing reused-worktree changes before sync",
            thread_id,
        )
        try:
            run(
                ["git", "stash", "push", "-u", "-m", f"reused-worktree-{issue_number}"],
                cwd=worktree_path,
                check=True,
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to stash dirty reused worktree for issue #{issue_number}; "
                "refusing to reset"
            ) from e

    def _restore_dirty_reused_worktree_commit_after_sync(
        self,
        *,
        issue_number: int,
        worktree_path: Path,
        branch_name: str,
        commit_sha: str,
        thread_id: int | None,
    ) -> None:
        """Replay a salvage commit onto the freshly synced PR branch."""
        self.impl._log(
            "info",
            f"Issue #{issue_number}: restoring preserved commit {commit_sha} onto "
            f"{branch_name} after sync",
            thread_id,
        )
        run(
            ["git", "cherry-pick", "-S", commit_sha],
            cwd=worktree_path,
            check=True,
        )

    def _validate_prior_threads(
        self,
        *,
        issue_number: int,
        pr_number: int | None,
        branch_name: str,
        worktree_path: Path,
        prior_threads: list[dict[str, Any]],
        iteration: int,
        thread_id: int | None,
    ) -> list[str]:
        """Re-open prior review comments the current diff does not address.

        Runs the read-only validation sub-agent
        (:func:`review_validator.validate_prior_comments_addressed`) against the
        previous iteration's threads. Returns the IDs of any threads it
        re-opened (empty when there is no PR, no prior threads, on the first
        iteration, in dry-run, or when everything was addressed).
        """
        if pr_number is None or not prior_threads or self.options.dry_run:
            return []
        diff_text = self.impl._collect_diff(worktree_path, branch_name)
        reopened, is_clean = validate_prior_comments_addressed(
            pr_number=pr_number,
            issue_number=issue_number,
            worktree_path=worktree_path,
            prior_threads=prior_threads,
            diff_text=diff_text,
            agent=self.options.agent,
            iteration=iteration,
            state_dir=self.state_dir,
            dry_run=False,
        )
        if not is_clean:
            self.impl._log(
                "warning",
                f"{issue_ref(issue_number)} R{iteration}: validator re-opened "
                f"{len(reopened)} prior review comment(s) the diff did not address",
                thread_id,
            )
        return reopened

    # ------------------------------------------------------------------
    # Strict review loop for implementer sessions
    # ------------------------------------------------------------------

    def _run_impl_review_loop(  # noqa: C901  # validate + review + address has several outcome paths
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
        pr_number: int | None = None,
        advise_findings: str = "",
    ) -> tuple[int, str | None, str | None]:
        """Run the bounded in-loop review + address cycle for an implementation.

        Stage 2 (#28): each iteration runs a FRESH reviewer session
        (``reviewer_agent(AGENT_PR_REVIEWER, i)``) that posts INLINE PR review
        threads and returns a verdict; if the verdict is not GO and blocking
        threads were posted, Session 2 (``AGENT_IMPLEMENTER``) is resumed to
        address those threads (fix → commit → push → resolve), then the next
        iteration re-reviews. The loop terminates on GO, on an iteration that
        posts no blocking threads, or after :data:`MAX_REVIEW_ITERATIONS`.

        When no ``pr_number`` is available (e.g. dry-run or the agent failed to
        open a PR), the in-loop posting/addressing cannot run; the loop falls
        back to the diff-only reviewer (no PR writes) so the verdict is still
        surfaced.
        """
        impl = self.impl
        last_verdict: str | None = None
        last_grade: str | None = None
        prior_review: str | None = None
        iterations_run = 0
        # Threads the previous iteration's address step was asked to fix, kept so
        # the next iteration can independently validate they were actually
        # addressed (and re-open the ones that weren't).
        prior_addressed_threads: list[dict[str, Any]] = []

        for iteration in range(MAX_REVIEW_ITERATIONS):
            # Step 1 (#1152): before the fresh review, verify (via the read-only
            # sub-agent) that every PRIOR review comment was truly addressed by
            # the current diff — resolving the ones confirmed fixed and re-opening
            # the ones that aren't. On iteration 0 of the existing-PR path there
            # is no prior-address snapshot, so seed it with the PR's currently
            # unresolved threads; otherwise pre-existing threads would never be
            # verified and the GO gate below would (wrongly) ignore them. From
            # iteration 1 on, ``prior_addressed_threads`` is the snapshot the
            # previous address step was asked to fix.
            #
            # The seed only applies to the existing-PR review path
            # (``session_id is None`` — see ``_review_existing_pr``): that PR
            # arrives with threads from earlier loops that must be re-verified
            # before a GO. The fresh-implementation path (``session_id`` set) has
            # no prior threads at iteration 0 — its threads are posted by R0's own
            # review — so seeding there would wrongly validate not-yet-addressed
            # comments against an empty diff.
            threads_to_validate = prior_addressed_threads
            if (
                not threads_to_validate
                and session_id is None
                and pr_number is not None
                and not self.options.dry_run
            ):
                with contextlib.suppress(Exception):
                    threads_to_validate = gh_pr_list_unresolved_threads(pr_number, dry_run=False)
            reopened = self._validate_prior_threads(
                issue_number=issue_number,
                pr_number=pr_number,
                branch_name=branch_name,
                worktree_path=worktree_path,
                prior_threads=threads_to_validate,
                iteration=iteration,
                thread_id=thread_id,
            )

            # Review step: a fresh reviewer session posts inline PR threads and
            # returns its verdict text. ``prior_review`` carries the previous
            # iteration's critique forward as reviewer context.
            if slot_id is not None:
                review_ref = pr_ref(pr_number) if pr_number is not None else issue_ref(issue_number)
                self.status_tracker.update_slot(
                    slot_id, f"{review_ref}: reviewing impl [R{iteration}]"
                )
            review_text, posted_thread_ids = impl._run_impl_review_step(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                branch_name=branch_name,
                worktree_path=worktree_path,
                pr_number=pr_number,
                iteration=iteration,
                prior_review=prior_review,
                advise_findings=advise_findings,
            )
            impl._save_review_log(issue_number, iteration, review_text)
            iterations_run = iteration + 1

            verdict = parse_review_verdict(review_text)
            last_verdict = verdict.verdict
            last_grade = verdict.grade
            impl._log(
                "info",
                f"{issue_ref(issue_number)} R{iteration}: Verdict={verdict.verdict} "
                f"Grade={verdict.grade or '?'} threads={len(posted_thread_ids)} "
                f"reopened={len(reopened)}",
                thread_id,
            )

            # A2-005: Persist review iteration progress so --resume can skip
            # already-completed iterations.  Persist BEFORE breaking out so
            # the final iteration's data is always on disk.
            impl._save_review_iteration_state(issue_number, iterations_run, review_text)

            # A GO (or empty) review cannot terminate the loop while the
            # validator just re-opened prior comments — those unresolved
            # re-opened threads must still be addressed. Treat the iteration as
            # NOGO so the address step below runs against them.
            go_blocked_by_automation = False
            if reopened:
                last_verdict = "NOGO"
            elif verdict.is_go:
                # GO converges ONLY when the PR has ZERO unresolved review threads
                # (#1152). The reviewer's verdict alone is not enough: a reviewer
                # can emit GO while the SAME pass posts new findings, or while
                # prior automation threads remain open. The GO label must be
                # earned by a clean pass where every comment has been addressed
                # (by the implementer) and verified resolved (by the prior-thread
                # validator that ran at the top of this iteration). This gate
                # RESOLVES NOTHING — it only counts what is still open.
                automation_unresolved = 0
                human_unresolved = 0
                if pr_number is not None and not self.options.dry_run:
                    (
                        automation_unresolved,
                        human_unresolved,
                    ) = self._count_unresolved_threads_blocking_go(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        thread_id=thread_id,
                    )
                if human_unresolved and pr_number is not None:
                    # A GO cannot stand while a HUMAN review thread is open.
                    # Automation must NOT resolve it and cannot fix it — a human
                    # has to. Break with a distinct terminal state (no spin to
                    # exhaustion, no state:skip) so the PR stays unlabeled,
                    # awaiting the human; the loop re-runs next pass via the
                    # "no go/no-go label → re-review" path once threads resolve.
                    last_verdict = "HUMAN_BLOCKED"
                    impl._log(
                        "info",
                        f"{pr_ref(pr_number)}: reviewer said GO but "
                        f"{human_unresolved} unresolved human review thread(s) remain "
                        f"— not accepting GO; awaiting human resolution, "
                        f"leaving PR unlabeled",
                        thread_id,
                    )
                    break
                if automation_unresolved and pr_number is not None:
                    # GO + open automation thread(s): the work is NOT actually
                    # done. Downgrade to NOGO so the address step (below) fixes
                    # and resolves them, and the next iteration re-reviews to
                    # confirm. ``go_blocked_by_automation`` forces the address
                    # step to run even though this GO pass may have posted no new
                    # threads (otherwise the zero-thread guard would skip it and
                    # the loop would spin GO→downgrade to exhaustion).
                    last_verdict = "NOGO"
                    go_blocked_by_automation = True
                    impl._log(
                        "info",
                        f"{pr_ref(pr_number)}: reviewer said GO but "
                        f"{automation_unresolved} unresolved automation review thread(s) "
                        "remain — addressing and re-reviewing before GO can stand",
                        thread_id,
                    )
                else:
                    impl._log(
                        "info",
                        f"{pr_ref(pr_number) if pr_number is not None else issue_ref(issue_number)}"
                        f": GO on iteration {iteration} — all review threads resolved, "
                        "review loop terminated",
                        thread_id,
                    )
                    break

            # Converge ONLY on an explicit Verdict: GO (handled above). A non-GO
            # pass with NO posted threads (and nothing re-opened) must NOT end the
            # loop here — a single garbage/AMBIGUOUS review would otherwise strand
            # a fixable PR after R0. There is nothing to address (no threads), so
            # skip the address step and RE-REVIEW on the next iteration; the loop
            # is bounded by MAX_REVIEW_ITERATIONS and applies ``state:skip`` only
            # on TRUE exhaustion (post-loop block). This is the "zero threads !=
            # GO" rule from the pr-review-loop skill (verified-ci): don't converge
            # on a zero-thread AMBIGUOUS/NO-GO pass.
            if (
                pr_number is not None
                and not posted_thread_ids
                and not reopened
                and not go_blocked_by_automation
            ):
                prior_review = review_text
                continue

            # Save this review for next iteration's context.
            prior_review = review_text

            # On the final iteration there is no subsequent review to verify a
            # fix, so addressing would be a wasted Session 2 resume + push.
            # Stop here and let the warning below flag the non-GO outcome.
            if iteration == MAX_REVIEW_ITERATIONS - 1:
                break

            # Address step: resume Session 2 to fix the posted threads, commit,
            # push, and resolve the threads it actually addressed. Skipped only
            # when there is no PR (no inline threads to address). ``session_id``
            # is informational — the address step resumes ``AGENT_IMPLEMENTER``
            # by its deterministic per-(repo,issue,agent) id (or starts a fresh
            # implementer session when no transcript exists), so the existing-PR
            # path (which has no initial session_id) can still fix review
            # threads rather than dead-ending here.
            if pr_number is None:
                continue
            # Snapshot the unresolved threads the address step is about to fix so
            # the NEXT iteration's validator can check they were truly addressed.
            prior_addressed_threads = gh_pr_list_unresolved_threads(pr_number, dry_run=False)
            if slot_id is not None:
                self.status_tracker.update_slot(
                    slot_id, f"{pr_ref(pr_number)}: addressing review [R{iteration}]"
                )
            addressed = impl._run_address_review_step(
                issue_number=issue_number,
                pr_number=pr_number,
                branch_name=branch_name,
                worktree_path=worktree_path,
                iteration=iteration,
                # When the loop started without an initial implementer session
                # (existing-PR review path), the address session may run fresh
                # with no transcript to resume — give it the task + task-review
                # + diff so it can read the work and continue. The fresh-impl
                # path already carries this in its long-lived transcript.
                include_bootstrap_context=session_id is None,
                issue_title=issue_title,
                issue_body=issue_body,
            )
            if not addressed:
                ref = issue_ref(issue_number)
                impl._log(
                    "info",
                    f"{ref}: address step resolved no threads on iteration {iteration}; "
                    "stopping review loop",
                    thread_id,
                )
                break

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

        # #1083 Bug 2 / #1085 C3: the loop must reach an explicit GO to be
        # considered converged. Apply ``state:skip`` only on TRUE iteration
        # exhaustion — ran all MAX_REVIEW_ITERATIONS without a GO — NOT on a
        # single non-GO (including AMBIGUOUS) outcome. Per the pr-review-loop
        # skill (verified-ci), a zero-thread AMBIGUOUS/NO-GO pass no longer ends
        # the loop early (it re-reviews above), so a transient garbage review
        # (e.g. a malformed verdict) gets MAX_REVIEW_ITERATIONS chances instead
        # of stranding a fixable PR after R0. ``last_verdict`` is None only when
        # there was no PR to review (dry-run / no-PR path).
        #
        # ERROR (reviewer-infrastructure failure — API 400, timeout, crash) is
        # NOT a real verdict: the reviewer never actually judged the code, so an
        # exhausted run that ended in ERROR must NOT be skipped. Skipping there
        # strands a never-reviewed PR (#911 / PR #1069). Leave the issue/PR
        # unlabeled so the "no go/no-go label → re-review" path re-runs the loop
        # next time the reviewer infra is healthy.
        # Neither ERROR (reviewer-infra failure) nor HUMAN_BLOCKED (GO blocked by
        # an open human thread) is a converged failure, so neither applies
        # state:skip — both leave the PR unlabeled for re-review / human action.
        exhausted = iterations_run >= MAX_REVIEW_ITERATIONS and last_verdict not in (
            "GO",
            "ERROR",
            "HUMAN_BLOCKED",
        )
        if (
            pr_number is not None
            and last_verdict is not None
            and exhausted
            and not self.options.dry_run
        ):
            logger.info(
                "#%d: review loop ended without GO (verdict=%r, iterations=%d) — applying %s",
                issue_number,
                last_verdict,
                iterations_run,
                STATE_SKIP,
            )
            with contextlib.suppress(Exception):
                gh_issue_add_labels(issue_number, [STATE_SKIP])
        elif last_verdict == "ERROR":
            logger.warning(
                "#%d: review loop ended in reviewer-infrastructure ERROR after %d "
                "iteration(s) — leaving PR unlabeled for re-review (no %s)",
                issue_number,
                iterations_run,
                STATE_SKIP,
            )
        elif last_verdict == "HUMAN_BLOCKED":
            logger.warning(
                "#%d: review reached GO but is blocked by unresolved human review "
                "thread(s) — leaving PR unlabeled for human resolution (no %s)",
                issue_number,
                STATE_SKIP,
            )

        return iterations_run, last_verdict, last_grade

    def _count_unresolved_threads_blocking_go(
        self,
        *,
        issue_number: int,
        pr_number: int,
        thread_id: int | None,
    ) -> tuple[int, int]:
        """Count unresolved review threads that block a GO, by ownership.

        A GO verdict may NOT stand while ANY review thread is unresolved — not
        even an automation-owned one. The earlier implementation bulk-resolved
        automation threads here so a GO could converge immediately; that was
        wrong (#1152). A reviewer can emit ``Verdict: GO`` in the SAME pass that
        posts new inline findings, and force-resolving those "automation-owned"
        threads accepted the GO without the implementer ever addressing them or
        a subsequent review verifying the fix. The GO label must only be earned
        once every comment has been genuinely addressed AND a clean re-review
        confirms zero unresolved threads.

        This method therefore RESOLVES NOTHING. It returns
        ``(automation_unresolved, human_unresolved)``:

        * ``human_unresolved > 0`` — automation cannot fix human threads, so the
          caller breaks with a distinct ``HUMAN_BLOCKED`` terminal state.
        * ``automation_unresolved > 0`` — the caller downgrades GO to NOGO so the
          address step fixes (and resolves only what it actually addressed) and a
          re-review re-evaluates. Convergence requires a GO pass that leaves zero
          unresolved threads.

        Returns ``(0, 0)`` when the thread list can't be fetched (fail-open:
        don't strand a GO on a transient API blip — a genuinely unresolved
        thread re-surfaces on the next loop's existing-PR re-review).
        """
        impl = self.impl
        try:
            threads = gh_pr_list_unresolved_threads(pr_number, dry_run=False)
        except Exception as exc:
            impl._log(
                "warning",
                f"{issue_ref(issue_number)}: could not list unresolved threads "
                f"after GO for {pr_ref(pr_number)}: {exc}",
                thread_id,
            )
            return (0, 0)
        if not threads:
            return (0, 0)

        current_login = gh_current_login()
        automation_unresolved = 0
        human_unresolved = 0
        for thread in threads:
            if _is_automation_owned_thread(thread, current_login):
                automation_unresolved += 1
            else:
                human_unresolved += 1
        if automation_unresolved or human_unresolved:
            impl._log(
                "info",
                f"{issue_number}: GO pass left {automation_unresolved} automation + "
                f"{human_unresolved} human unresolved thread(s) on {pr_ref(pr_number)} "
                "— GO cannot stand until all are addressed and a clean re-review confirms",
                thread_id,
            )
        return (automation_unresolved, human_unresolved)

    def _run_impl_review_step(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        branch_name: str,
        worktree_path: Path,
        pr_number: int | None,
        iteration: int,
        prior_review: str | None,
        advise_findings: str = "",
    ) -> tuple[str, list[str]]:
        """Run one in-loop review and return ``(review_text, posted_thread_ids)``.

        With a ``pr_number`` this folds in ``pr_reviewer``'s core
        (:func:`review_pr_inline`): a fresh per-iteration reviewer session posts
        INLINE PR review threads and returns its summary text (carrying the
        ``Grade:`` / ``Verdict:`` line). The reviewer context includes the TASK
        (issue title + body), the PLAN and PLAN_REVIEW comments, and the impl
        diff (#28).

        Without a ``pr_number`` (dry-run / no PR) it falls back to the diff-only
        reviewer (:meth:`_run_impl_review`) which posts nothing.
        """
        impl = self.impl
        if pr_number is None:
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
            return review_text, []

        if self.options.dry_run:
            logger.info("[DRY RUN] Would run in-loop PR review for #%s", pr_number)
            return "Grade: A\nVerdict: GO\n", []

        diff_text = impl._collect_diff(worktree_path, branch_name)
        plan_text, plan_review_text = self._fetch_plan_and_review(issue_number)
        context = gather_impl_review_context(
            pr_number=pr_number,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
            plan_review_text=plan_review_text,
            diff_text=diff_text,
            advise_findings=advise_findings,
            include_nitpicks=self.options.include_nitpicks,
        )
        try:
            return review_pr_inline(
                pr_number=pr_number,
                issue_number=issue_number,
                worktree_path=worktree_path,
                context=context,
                agent=self.options.agent,
                iteration=iteration,
                state_dir=self.state_dir,
                dry_run=False,
            )
        except Exception as e:
            logger.error(
                "#%s R%s: in-loop PR review failed: %s; recording ERROR (re-review next "
                "loop, no skip/label) so an infra failure isn't mistaken for a NOGO",
                issue_number,
                iteration,
                e,
            )
            return (
                f"In-loop reviewer invocation failed at iteration {iteration}: {e}\n\n"
                f"{INFRA_ERROR_REVIEW_TEXT}"
            ), []

    def _run_address_review_step(
        self,
        *,
        issue_number: int,
        pr_number: int,
        branch_name: str,
        worktree_path: Path,
        iteration: int,
        include_bootstrap_context: bool = False,
        issue_title: str = "",
        issue_body: str = "",
    ) -> bool:
        """Address the posted PR threads in-loop, resuming Session 2.

        Folds in ``address_review``'s core: lists the unresolved threads on the
        PR, runs the fix session (resuming ``AGENT_IMPLEMENTER`` via
        :func:`run_address_fix_session`, which fans out one sub-agent per COMMENT
        at the model tier matching its classified difficulty, #1083), then
        commits + pushes the fixes. Thread RESOLUTION is no longer done here — it
        moved to the evidence-based validator (#1083); this step only counts as
        progress when a real commit was produced (#1085).

        Returns:
            ``True`` if at least one thread was addressed (so the loop should
            re-review); ``False`` when nothing was addressable (the loop stops).

        """
        threads = gh_pr_list_unresolved_threads(pr_number, dry_run=False)
        if not threads:
            logger.info(
                "#%s R%s: no unresolved threads to address on PR %s",
                issue_number,
                iteration,
                pr_ref(pr_number),
            )
            return False

        # On the existing-PR path the address session may run fresh (no
        # implementer transcript to resume), so it has no memory of the task or
        # the implementation. Bootstrap it with the task, the plan-review, and
        # the current diff. The fresh-impl path already carries this in its
        # long-lived transcript, so the blocks stay empty there (no extra cost).
        task_block = ""
        task_review_block = ""
        diff_text = ""
        if include_bootstrap_context:
            task_block = f"#{issue_number} {issue_title}\n\n{issue_body}".strip()
            _, task_review_block = self._fetch_plan_and_review(issue_number)
            diff_text = self.impl._collect_diff(worktree_path, branch_name)

        log_file = self.state_dir / f"address-review-{issue_number}-r{iteration}.log"
        fix_result = run_address_fix_session(
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            threads=threads,
            agent=self.options.agent,
            repo_root=self.repo_root,
            parse_fn=lambda text: self._parse_address_result(text, issue_number, iteration),
            log_file=log_file,
            dry_run=False,
            task_block=task_block,
            task_review_block=task_review_block,
            diff_text=diff_text,
        )
        addressed: list[str] = fix_result.get("addressed", [])

        # #1083 Bug 1: gate "addressed" on a REAL commit. The fix session's
        # self-reported ``addressed`` list is not trusted on its own — a session
        # can claim it resolved a thread while leaving the worktree clean (e.g.
        # replying "documented as a limitation" with no code change). Only when
        # _commit_if_changes actually produced a commit do we push and report
        # progress, so a no-op fix can never advance the loop.
        committed = self._commit_if_changes(issue_number, worktree_path)
        if not (addressed and committed):
            logger.info(
                "#%s R%s: address step produced no committed change "
                "(addressed=%s, committed=%s) — not counted as progress",
                issue_number,
                iteration,
                bool(addressed),
                committed,
            )
            return False
        self._push_branch(branch_name, worktree_path)

        # #1083 Bug 1/Move-to-reviewer: resolution is NO LONGER done here. The
        # validator (review_validator.validate_prior_comments_addressed) resolves
        # each prior thread only after a fresh read-only sub-agent confirms the
        # current diff actually addresses it. This closes the "resolved without
        # implementing" hole — a self-reported fix with no diff change stays open.
        return True

    def _parse_address_result(self, text: str, issue_number: int, iteration: int) -> dict[str, Any]:
        """Parse the address-session JSON block, tracing parse failures.

        Wraps :func:`address_review._parse_addressed_block` but writes a
        diagnostic trace file when the block is missing/malformed, so an empty
        ``addressed`` list is distinguishable from "the model reviewed and
        chose no fixes" (mirrors the standalone phase's behavior).
        """
        from .address_review import _parse_addressed_block

        matches = _parse_addressed_block(text)
        if not matches.get("addressed") and "```json" not in text:
            with contextlib.suppress(Exception):
                trace_path = self.state_dir / f"address-{issue_number}-r{iteration}.parse-error.log"
                trace_path.write_text(
                    f"reason: no fenced ```json block found in response\n\n"
                    f"=== full response ===\n{text}"
                )
        return matches

    def _fetch_plan_and_review(self, issue_number: int) -> tuple[str, str]:
        """Return ``(plan_text, plan_review_text)`` for the reviewer context.

        The PLAN comment is identified the same way
        :func:`planner_state._comments_contain_plan` does — prefix-anchored on
        :data:`PLAN_COMMENT_MARKER`, skipping comments whose body starts with
        :data:`review_state.PLAN_REVIEW_PREFIX`. The PLAN_REVIEW comment is
        the one whose body starts with ``review_state.PLAN_REVIEW_PREFIX``.
        Best-effort: any fetch failure yields empty strings (looked up via
        ``_impl_module`` so tests can patch
        ``hephaestus.automation.implementer.review_state``).
        """
        plan_text = ""
        plan_review_text = ""
        try:
            review_state = self._impl_module.review_state
            comments = review_state._fetch_issue_comments_graphql(issue_number)
            for comment in comments:
                body = comment.get("body", "")
                stripped = body.lstrip()
                if stripped.startswith(review_state.PLAN_REVIEW_PREFIX):
                    plan_review_text = body
                elif stripped.startswith(PLAN_COMMENT_MARKER):
                    plan_text = body
        except Exception as e:
            logger.warning("#%s: failed to fetch PLAN/PLAN_REVIEW context: %s", issue_number, e)
        return plan_text, plan_review_text

    def _commit_if_changes(self, issue_number: int, worktree_path: Path) -> bool:
        """Commit any pending changes from the in-loop address step.

        Silently skips when the worktree is clean. Mirrors
        ``AddressReviewer._commit_if_changes``.

        Returns:
            ``True`` iff a commit was actually created. ``False`` when the
            worktree was clean (nothing to commit) or the commit failed. The
            caller (#1083) uses this to gate progress/resolution on a real
            change rather than the model's self-report.

        """
        result = run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
        )
        if not result.stdout.strip():
            logger.info("No changes to commit for issue #%s", issue_number)
            return False
        try:
            commit_changes(issue_number, worktree_path, self.options.agent)
            logger.info("Committed in-loop address changes for issue #%s", issue_number)
            return True
        except RuntimeError as e:
            logger.warning("Commit skipped for issue #%s: %s", issue_number, e)
            return False

    def _push_branch(self, branch_name: str, worktree_path: Path) -> None:
        """Push *branch_name* to origin after an in-loop address step.

        Mirrors ``AddressReviewer._push_branch``.

        Raises:
            RuntimeError: If the push fails.

        """
        try:
            run(["git", "push", "origin", branch_name], cwd=worktree_path)
            logger.info("Pushed branch %s to origin", branch_name)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to push branch {branch_name}: {e}") from e

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
        repo_slug = _impl_mod.get_repo_slug(self.repo_root)
        try:
            _impl_mod.invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=_impl_mod.AGENT_IMPLEMENTER,
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
                "#%s R%s: impl reviewer call failed: %s; recording ERROR (re-review next "
                "loop, no skip/label) so an infra failure isn't mistaken for a NOGO",
                issue_number,
                iteration,
                e,
            )
            return (
                f"Reviewer invocation failed at iteration {iteration}: {e}\n\n"
                f"{INFRA_ERROR_REVIEW_TEXT}"
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
        repo_slug = _impl_mod.get_repo_slug(self.repo_root)

        try:
            stdout, _ = _impl_mod.invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=_impl_mod.AGENT_IMPLEMENTER,
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
            self.options.agent,
        )
