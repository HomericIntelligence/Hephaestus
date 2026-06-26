"""Per-issue pipeline coordinator for :class:`IssueImplementer`.

Originally extracted from :mod:`hephaestus.automation.implementer` (#597), the
runner owned the entire ``_implement_issue`` body plus every phase helper in
one ~2600-line class. The #712 decomposition splits that god-class into five
single-responsibility phase collaborators —
:class:`~hephaestus.automation._plan_phase.PlanPhase`,
:class:`~hephaestus.automation._implement_phase.ImplementPhase`,
:class:`~hephaestus.automation._review_phase.ReviewPhase`,
:class:`~hephaestus.automation._pr_create_phase.PRCreatePhase`, and
:class:`~hephaestus.automation._followup_phase.FollowUpPhase` — each built
around a single :class:`~hephaestus.automation._stage_context.StageContext`.

:class:`ImplementationPhaseRunner` is now a thin pipeline coordinator: it owns
the top-level per-issue orchestration (``_implement_issue`` /
``_review_existing_pr`` and the dirty-reused-worktree salvage helpers) and
delegates each phase's work to the matching collaborator.

The runner still keeps a back-reference to the parent ``IssueImplementer`` and
re-exposes the phase methods under their original names. That preserves the
test-patch contract: ``patch.object(impl, "_has_plan", ...)`` still intercepts
every callsite — ``IssueImplementer`` forwards ``impl._has_plan`` to the runner,
the runner forwards to ``PlanPhase``, and the phases dispatch cross-phase work
back through ``impl._method`` so the patch wins.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_text,
    uses_direct_agent_runner,
)

from . import review_state  # noqa: F401  # patch surface — see #714 cycle-break note
from ._followup_phase import FollowUpPhase
from ._implement_phase import ImplementPhase, _prepend_advise
from ._plan_phase import PlanPhase
from ._pr_create_phase import PRCreatePhase
from ._review_phase import (
    MAX_REVIEW_ITERATIONS,
    MAX_REVIEW_ITERATIONS_HARD_CAP,
    ReviewPhase,
)
from ._review_utils import find_pr_for_issue, get_pr_head_branch
from ._stage_context import StageContext
from .claude_invoke import invoke_claude_with_session
from .claude_models import advise_model
from .claude_timeouts import advise_claude_timeout
from .git_utils import (
    commit_if_changes,
    get_repo_slug,
    is_clean_working_tree,
    issue_ref,
    pr_ref,
    push_branch,
    run,
    sync_worktree_to_remote_branch,
)
from .github_api import fetch_issue_info, gh_issue_add_labels
from .models import (
    ImplementationPhase,
    ImplementationState,
    WorkerResult,
)
from .pr_manager import (
    ensure_pr_auto_merge_deferred,
    pr_has_implementation_state_label,
)
from .prompts import get_dirty_reused_worktree_decision_prompt, get_implementation_prompt
from .review_state import is_plan_review_go

# Test-Patch Contract — patchable symbols owned by this module.
# All symbols below are imported top-level from their true source modules so
# that tests patch them at ``hephaestus.automation.implementer_phase_runner.<X>``.
# This is the pattern established by #714 (which removed the old deferred
# per-runner ``from . import implementer`` back-pointer that caused an import
# cycle with :mod:`.implementer`).
#
#   Symbol                    Patched by
#   ------------------------- ----------------------------------------------------
#   find_pr_for_issue         test_implementer.py (TestReviewExistingPR)
#   get_pr_head_branch        test_implementer.py (TestReviewExistingPR)
#   is_plan_review_go         test_implementer.py / test_implementer_loop.py
#   fetch_issue_info          test_implementer.py / test_implementer_loop.py
#   invoke_claude_with_session test_implementer.py / test_implementer_loop.py
#   get_repo_slug             test_implementer.py
#   AGENT_IMPLEMENTER         test_implementer.py / test_implementer_loop.py
#   AGENT_ADVISE              test_implementer.py / test_implementer_loop.py
#   current_trunk_githash     test_implementer.py (patch-only; not used at runtime)
#   review_state (submodule)  test_implementer.py (via ``from . import review_state``)
#   run                       test_implementer_loop.py (health-check)
#   sync_worktree_to_remote_branch  test_implementer_loop.py
#   ensure_pr_auto_merge_deferred   test_implementer.py
#
# Keep in sync: grep -rn 'patch.*hephaestus\.automation\.implementer_phase_runner\.' tests/
from .session_naming import (  # noqa: F401
    AGENT_ADVISE,
    AGENT_IMPLEMENTER,
    current_trunk_githash,
)
from .state_labels import STATE_SKIP

if TYPE_CHECKING:
    from .implementer import IssueImplementer

logger = logging.getLogger(__name__)

# Re-exported for back-compat: ``implementer`` imports ``MAX_REVIEW_ITERATIONS``
# from this module. The canonical value now lives in ``_review_phase``.
__all__ = [
    "MAX_REVIEW_ITERATIONS",
    "MAX_REVIEW_ITERATIONS_HARD_CAP",
    "ImplementationPhaseRunner",
]

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


class ImplementationPhaseRunner:
    """Coordinate the per-issue implementation pipeline for one ``IssueImplementer``.

    The runner is constructed by :class:`IssueImplementer` and keeps a
    back-reference to it so cross-phase dispatch (``_has_plan``,
    ``_save_state``, ``_run_claude_code``, …) can flow back through the
    coordinator. It owns the top-level orchestration and delegates the
    plan / implement / review / PR-create / follow-up work to dedicated phase
    collaborators built on a shared :class:`StageContext`.
    """

    def __init__(self, impl: IssueImplementer) -> None:
        """Initialize the runner and its phase collaborators.

        Args:
            impl: Parent ``IssueImplementer``. Held by reference; the
                runner and its phases read ``impl.options``, ``impl.state_dir``,
                ``impl.repo_root``, ``impl.worktree_manager``,
                ``impl.status_tracker``, ``impl.state_mgr``, and the
                ``_log`` / ``_get_state`` / ``_get_or_create_state`` /
                ``_save_state`` helper methods from it.

        """
        self.impl = impl
        self.ctx = StageContext(impl=impl, runner=self)
        self.plan_phase = PlanPhase(self.ctx)
        self.implement_phase = ImplementPhase(self.ctx)
        self.review_phase = ReviewPhase(self.ctx)
        self.pr_create_phase = PRCreatePhase(self.ctx)
        self.followup_phase = FollowUpPhase(self.ctx)

    # ------------------------------------------------------------------
    # Convenience accessors — keep orchestration bodies readable.
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

    # ------------------------------------------------------------------
    # Top-level per-issue pipeline
    # ------------------------------------------------------------------

    def _dispatch_issue_work(
        self,
        *,
        issue_number: int,
        slot_id: int | None,
        thread_id: int | None,
    ) -> WorkerResult:
        """Execute the implementation pipeline body for one issue (no slot/error management).

        Called inside the try block of :meth:`_implement_issue`; exception
        handling and slot lifecycle live in the caller. Handles dry-run early
        exit, existing-PR fast-path, and the fresh-implementation happy path.
        """
        impl = self.impl
        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Starting")
        impl._log("info", f"Starting issue {issue_ref(issue_number)}", thread_id)

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
                issue_number=issue_number, success=True, branch_name=branch_name, worktree_path=None
            )

        # When an open PR already exists for this issue, skip the
        # plan/implement steps (the PR is already opened) but STILL drive it
        # through the in-loop review → address cycle so a green PR earns the
        # ``state:implementation-go`` label that drive-green requires to arm
        # auto-merge. Imported top-level so tests patch
        # ``hephaestus.automation.implementer_phase_runner.find_pr_for_issue``.
        self.status_tracker.update_slot(
            slot_id, f"{issue_ref(issue_number)}: Checking for existing PR"
        )
        existing_pr = find_pr_for_issue(issue_number)
        if existing_pr is not None:
            return self._review_existing_pr(
                issue_number=issue_number,
                existing_pr=existing_pr,
                branch_name=branch_name,
                state=state,
                slot_id=slot_id,
                thread_id=thread_id,
            )

        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Creating worktree")
        # refresh_base=True: issue-major loop (#1560) — cut this issue's branch
        # from the freshly-merged trunk so it never conflicts with a sibling
        # issue's just-merged PR.
        worktree_path = self.worktree_manager.create_worktree(
            issue_number, branch_name, refresh_base=True
        )
        with self.state_lock:
            state.worktree_path = str(worktree_path)
            state.branch_name = branch_name
        impl._save_state(state)

        deferred = self._ensure_plan_ready(
            issue_number=issue_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            state=state,
            slot_id=slot_id,
            thread_id=thread_id,
        )
        if deferred is not None:
            return deferred

        return self._run_implementation_and_review(
            issue_number=issue_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            state=state,
            slot_id=slot_id,
            thread_id=thread_id,
        )

    def _implement_issue(self, issue_number: int) -> WorkerResult:
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
                issue_number=issue_number, success=False, error="Failed to acquire worker slot"
            )

        thread_id = threading.get_ident()

        try:
            return self._dispatch_issue_work(
                issue_number=issue_number,
                slot_id=slot_id,
                thread_id=thread_id,
            )

        except subprocess.TimeoutExpired as e:
            error_msg = f"Timeout: {' '.join(e.cmd[:3])} exceeded {e.timeout}s"
            impl._log("error", error_msg, thread_id)
            return self._record_issue_failure(
                issue_number, slot_id, thread_id, error_msg, persist_error=error_msg
            )

        except subprocess.CalledProcessError as e:
            error_msg = f"Command failed (exit {e.returncode}): {' '.join(e.cmd[:3])}"
            impl._log("error", error_msg, thread_id)
            if e.stderr:
                impl._log("error", f"stderr: {e.stderr[:300]}", thread_id)
            return self._record_issue_failure(
                issue_number, slot_id, thread_id, error_msg, persist_error=str(e)
            )

        except RuntimeError as e:
            return self._handle_runtime_error(issue_number, slot_id, thread_id, e)

        except Exception as e:  # broad catch: top-level worker boundary, must not crash thread pool
            impl._log("error", f"Unexpected {type(e).__name__}: {e}", thread_id)
            return self._record_issue_failure(
                issue_number, slot_id, thread_id, str(e)[:80], persist_error=str(e)
            )

        finally:
            self.status_tracker.release_slot(slot_id)

    def _handle_runtime_error(
        self,
        issue_number: int,
        slot_id: int | None,
        thread_id: int | None,
        e: RuntimeError,
    ) -> WorkerResult:
        """Map a ``RuntimeError`` from the pipeline to a WorkerResult.

        "No changes produced" / "no commits vs" means the branch has 0 commits
        vs main — the implementation already landed via a prior merged PR. Treat
        that as success and apply ``state:skip`` so future loops don't re-attempt
        the issue. Any other RuntimeError is a genuine failure.
        """
        impl = self.impl
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
        return self._record_issue_failure(
            issue_number, slot_id, thread_id, str(e)[:80], persist_error=str(e)
        )

    def _record_issue_failure(
        self,
        issue_number: int,
        slot_id: int | None,
        thread_id: int | None,
        ui_error: str,
        *,
        persist_error: str,
    ) -> WorkerResult:
        """Surface a failure in the UI, persist FAILED state, and return a result.

        Shared by every ``_implement_issue`` exception handler so the
        "show-in-UI → mark state FAILED + bump attempts → return failure
        WorkerResult" tail cannot drift between handlers.

        Args:
            issue_number: The issue that failed.
            slot_id: UI slot to update (``None`` skips the UI update).
            thread_id: Worker thread id for log correlation.
            ui_error: Short error string shown in the status slot.
            persist_error: Full error string recorded on the issue state and
                returned in the :class:`WorkerResult`.

        """
        impl = self.impl
        self.status_tracker.update_slot(
            slot_id, f"{issue_ref(issue_number)}: FAILED - {ui_error[:50]}"
        )

        err_state = impl._get_state(issue_number)
        if err_state:
            with self.state_lock:
                err_state.phase = ImplementationPhase.FAILED
                err_state.error = persist_error
                err_state.attempts += 1
            impl._save_state(err_state)

        return WorkerResult(
            issue_number=issue_number,
            success=False,
            error=persist_error,
        )

    def _ensure_plan_ready(
        self,
        *,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
        thread_id: int | None,
    ) -> WorkerResult | None:
        """Ensure the issue has a plan and a GO plan-review before implementing.

        Generates a plan if none exists, then gates on the plan-review verdict
        (#551). ``_has_plan`` only verifies a plan comment EXISTS; it does not
        look at the plan-reviewer's verdict, so a NOGO plan (or a NOGO-exhausted
        plan that still starts with "# Implementation Plan") used to be
        implemented just like a GO one. When the latest plan-review is anything
        other than GO, the issue is deferred so the planner can re-plan and the
        reviewer re-evaluate.

        Returns a deferral :class:`WorkerResult` (``plan_review_not_go=True``)
        when the gate fails, or ``None`` when the plan is GO and implementation
        may proceed.
        """
        impl = self.impl
        # Check for existing plan
        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Checking plan")
        if not impl._has_plan(issue_number):
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Generating plan")
            impl._log("info", f"Issue #{issue_number} has no plan, generating...", thread_id)
            with self.state_lock:
                state.phase = ImplementationPhase.PLANNING
            impl._save_state(state)
            impl._generate_plan(issue_number)

        self.status_tracker.update_slot(
            slot_id, f"{issue_ref(issue_number)}: Checking plan-review verdict"
        )
        if not is_plan_review_go(issue_number):
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
        return None

    def _run_advise_and_implement(
        self,
        *,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
        issue: Any,
    ) -> tuple[str | None, str]:
        """Run advise-first + implementation agent; persist session to state.

        Returns ``(session_id, advise_findings)``.
        """
        impl = self.impl
        # Advise-first (#30): Claude and Codex both use the shared selected-skill
        # lookup and explicitly prepend its returned context to the task prompt.
        implementation_advise_findings = ""
        if self.options.enable_advise:
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Advising")
            implementation_advise_findings = impl._run_advise(issue_number, issue.title, issue.body)

        self.status_tracker.update_slot(
            slot_id, f"{issue_ref(issue_number)}: Running {self.options.agent}"
        )
        session_id = impl._run_claude_code(
            issue_number,
            worktree_path,
            _prepend_advise(
                implementation_advise_findings,
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
        return session_id, implementation_advise_findings

    def _run_review_loop_with_verdict(
        self,
        *,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
        thread_id: int | None,
        session_id: str | None,
        pr_number: int,
        issue: Any,
        advise_findings: str,
    ) -> None:
        """Create PR, run review loop, save state, and apply verdict label.

        Called from both the fresh-implementation and existing-PR paths.
        ``pr_number`` is the already-created PR; no PR creation happens here.
        """
        impl = self.impl
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
            advise_findings=advise_findings,
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

    def _run_implementation_and_review(
        self,
        *,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
        thread_id: int | None,
    ) -> WorkerResult:
        """Run the fresh-implementation happy path: implement → PR → review → follow-up.

        Reached only after the plan-review GO gate in :meth:`_implement_issue`
        passes. Fetches issue context, runs advise + the selected agent, opens
        the PR up-front, drives the strict review loop, labels the verdict, then
        runs post-PR /learn + follow-up filing.
        """
        impl = self.impl
        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Fetching issue")
        with self.state_lock:
            state.phase = ImplementationPhase.IMPLEMENTING
        impl._save_state(state)
        issue = fetch_issue_info(issue_number)

        session_id, advise_findings = self._run_advise_and_implement(
            issue_number=issue_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            state=state,
            slot_id=slot_id,
            issue=issue,
        )

        # Create the PR up-front so the in-loop reviewer (Stage 2, #28) has a
        # concrete PR to post INLINE review threads against. ``_finalize_pr`` is
        # idempotent — ``ensure_pr_created`` no-ops if the agent already opened it.
        pr_number = impl._finalize_pr(issue_number, branch_name, worktree_path, state, slot_id)
        ensure_pr_auto_merge_deferred(pr_number)

        self._run_review_loop_with_verdict(
            issue_number=issue_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            state=state,
            slot_id=slot_id,
            thread_id=thread_id,
            session_id=session_id,
            pr_number=pr_number,
            issue=issue,
            advise_findings=advise_findings,
        )

        impl._run_post_pr_followup(issue_number, worktree_path, state, slot_id)
        impl._log("info", f"Issue #{issue_number} completed: PR {pr_ref(pr_number)}", thread_id)

        return WorkerResult(
            issue_number=issue_number,
            success=True,
            pr_number=pr_number,
            branch_name=branch_name,
            worktree_path=str(worktree_path),
        )

    def _prepare_worktree_for_existing_pr(
        self,
        *,
        issue_number: int,
        existing_pr: int,
        branch_name: str,
        state: ImplementationState,
        slot_id: int | None,
        thread_id: int | None,
    ) -> tuple[Path, str]:
        """Resolve branch, sync worktree to PR head, update state.

        Returns ``(worktree_path, pr_branch)``. The worktree is hard-reset to
        ``origin/<pr-head>`` so re-running never discards pushed commits.
        Dirty reused worktrees are salvaged (commit or stash) before the reset.
        """
        impl = self.impl
        # Resolve the PR's REAL head branch — never assume ``{issue}-auto-impl``.
        # ``find_pr_for_issue`` may have matched via PR-body ``Closes #N`` so the
        # head branch can be named after a different issue (or a bundle).
        pr_branch = get_pr_head_branch(existing_pr) or branch_name
        if pr_branch != branch_name:
            impl._log(
                "info",
                f"Issue #{issue_number}: {pr_ref(existing_pr)} head branch is "
                f"{pr_branch!r} (not the assumed {branch_name!r}); using the real branch",
                thread_id,
            )

        self.status_tracker.update_slot(
            slot_id, f"{issue_ref(issue_number)}: Preparing worktree for existing PR"
        )
        worktree_path = self.worktree_manager.create_worktree(issue_number, pr_branch)
        # ``create_worktree`` may REUSE a worktree with uncommitted changes from
        # another session; ``reset --hard`` would silently discard them. Only when
        # dirty, let an agent decide (commit=branch-belongs, stash=unrelated).
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
        return worktree_path, pr_branch

    def _run_advise_and_review_for_existing_pr(
        self,
        *,
        issue_number: int,
        existing_pr: int,
        pr_branch: str,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
        thread_id: int | None,
    ) -> str | None:
        """Fetch issue, run advise, drive review loop, save state, apply verdict.

        Returns the last verdict string (or ``None`` if the loop didn't run).
        """
        impl = self.impl
        issue = fetch_issue_info(issue_number)

        # Advise-first (#30): same selected-skill context path as fresh implementation.
        implementation_advise_findings = ""
        if self.options.enable_advise:
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Advising")
            implementation_advise_findings = impl._run_advise(issue_number, issue.title, issue.body)

        self._run_review_loop_with_verdict(
            issue_number=issue_number,
            branch_name=pr_branch,
            worktree_path=worktree_path,
            state=state,
            slot_id=slot_id,
            thread_id=thread_id,
            session_id=None,
            pr_number=existing_pr,
            issue=issue,
            advise_findings=implementation_advise_findings,
        )
        return state.last_review_verdict

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

        GO-labeled PRs short-circuit (already settled on a prior loop). NO-GO
        PRs re-enter the review→address cycle until they earn GO. The worktree
        is prepared on the PR's REAL head branch (anti-clobber: hard-reset to
        ``origin/<pr-head>`` before the loop). ``session_id=None`` — the address
        step resumes the implementer session by deterministic id on demand.
        """
        impl = self.impl

        self.status_tracker.update_slot(
            slot_id, f"{pr_ref(existing_pr)}: Checking implementation-review label"
        )
        has_go, has_no_go = pr_has_implementation_state_label(existing_pr)
        if has_go:
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
            impl._log(
                "info",
                f"Issue #{issue_number}: open PR {pr_ref(existing_pr)} is "
                "implementation-review NO-GO — re-running implement + review loop "
                "to drive it toward GO",
                thread_id,
            )

        worktree_path, pr_branch = self._prepare_worktree_for_existing_pr(
            issue_number=issue_number,
            existing_pr=existing_pr,
            branch_name=branch_name,
            state=state,
            slot_id=slot_id,
            thread_id=thread_id,
        )
        last_verdict = self._run_advise_and_review_for_existing_pr(
            issue_number=issue_number,
            existing_pr=existing_pr,
            pr_branch=pr_branch,
            worktree_path=worktree_path,
            state=state,
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

    # ------------------------------------------------------------------
    # Dirty reused-worktree salvage (orchestration helpers)
    # ------------------------------------------------------------------

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
            if uses_direct_agent_runner(self.options.agent):
                result = run_agent_text(
                    agent=self.options.agent,
                    prompt=prompt,
                    cwd=worktree_path,
                    timeout=advise_claude_timeout(),
                    model=direct_agent_model(self.options.agent, "HEPH_ADVISE_MODEL"),
                    sandbox="read-only",
                )
                output = result.stdout or ""
            else:
                repo_slug = get_repo_slug(self.repo_root)
                output, _ = invoke_claude_with_session(
                    repo=repo_slug,
                    issue=issue_number,
                    agent=AGENT_ADVISE,
                    prompt=prompt,
                    model=advise_model(),
                    cwd=worktree_path,
                    timeout=advise_claude_timeout(),
                    output_format="text",
                    allowed_tools="Read,Glob,Grep,Bash",
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
        # Use ``git add -u`` (tracked files only) instead of ``git add -A``
        # so unrelated untracked leftover state in the reused worktree is NOT
        # swept into the salvage commit.  Untracked leftovers from a prior
        # session are excluded by design; only tracked modifications are
        # preserved.  This prevents the cherry-pick from conflicting on
        # unrelated files after the subsequent reset --hard.
        run(["git", "add", "-u"], cwd=worktree_path, check=True)
        run(
            [
                "git",
                "commit",
                "-S",
                "-s",
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
        """Replay a salvage commit onto the freshly synced PR branch.

        Cherry-pick conflicts are treated as non-fatal: the in-progress work is
        recoverable because the automation agent will regenerate it on the next
        implementation turn.  Aborting on conflict avoids leaving the worktree
        in an unresolvable merge state that would block the whole issue.
        """
        self.impl._log(
            "info",
            f"Issue #{issue_number}: restoring preserved commit {commit_sha} onto "
            f"{branch_name} after sync",
            thread_id,
        )
        try:
            run(
                ["git", "cherry-pick", "-S", "-s", commit_sha],
                cwd=worktree_path,
                check=True,
            )
        except Exception as exc:
            # Cherry-pick failed (e.g. conflict with the freshly synced remote
            # state).  Abort the failed pick to leave the worktree clean, then
            # log a warning and continue — the agent will regenerate the work on
            # the next implementation turn rather than blocking the entire issue.
            self.impl._log(
                "warning",
                f"Issue #{issue_number}: cherry-pick of salvage commit {commit_sha} "
                f"onto {branch_name} failed ({exc}); aborting pick — preserved "
                "changes will be regenerated by the agent on the next turn",
                thread_id,
            )
            run(
                ["git", "cherry-pick", "--abort"],
                cwd=worktree_path,
                check=False,
            )

    # ------------------------------------------------------------------
    # Phase delegators — preserve the original public method names so the
    # ``patch.object(impl, "_method", ...)`` test contract keeps intercepting.
    # ``IssueImplementer`` forwards ``impl._method`` to the runner; the runner
    # forwards to the owning phase.
    # ------------------------------------------------------------------

    # PlanPhase
    def _has_plan(self, issue_number: int) -> bool:
        """Delegate to :meth:`PlanPhase._has_plan`."""
        return self.plan_phase._has_plan(issue_number)

    def _generate_plan(self, issue_number: int) -> None:
        """Delegate to :meth:`PlanPhase._generate`."""
        self.plan_phase._generate(issue_number)

    # ImplementPhase
    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Delegate to :meth:`ImplementPhase._run_advise`."""
        return self.implement_phase._run_advise(issue_number, issue_title, issue_body)

    def _run_advise_as_implementer_turn(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        worktree_path: Path,
    ) -> str:
        """Delegate to :meth:`ImplementPhase._run_advise_as_implementer_turn`."""
        return self.implement_phase._run_advise_as_implementer_turn(
            issue_number, issue_title, issue_body, worktree_path
        )

    def _compact_implementer_session(self, issue_number: int, worktree_path: Path) -> None:
        """Delegate to :meth:`ImplementPhase._compact_implementer_session`."""
        self.implement_phase._compact_implementer_session(issue_number, worktree_path)

    def _run_claude_code(
        self, issue_number: int, worktree_path: Path, prompt: str, slot_id: int | None = None
    ) -> str | None:
        """Delegate to :meth:`ImplementPhase._run_claude_code`."""
        return self.implement_phase._run_claude_code(issue_number, worktree_path, prompt, slot_id)

    def _run_claude_impl_session(
        self, issue_number: int, worktree_path: Path, prompt: str
    ) -> str | None:
        """Delegate to :meth:`ImplementPhase._run_claude_impl_session`."""
        return self.implement_phase._run_claude_impl_session(issue_number, worktree_path, prompt)

    def _run_codex_code(self, issue_number: int, worktree_path: Path, prompt: str) -> str | None:
        """Delegate to :meth:`ImplementPhase._run_codex_code`."""
        return self.implement_phase._run_codex_code(issue_number, worktree_path, prompt)

    # PRCreatePhase
    def _finalize_pr(
        self,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
    ) -> int:
        """Delegate to :meth:`PRCreatePhase._finalize_pr`."""
        return self.pr_create_phase._finalize_pr(
            issue_number, branch_name, worktree_path, state, slot_id
        )

    def _run_tests_in_worktree(self, worktree_path: Path, issue_number: int) -> bool:
        """Delegate to :meth:`PRCreatePhase._run_tests_in_worktree`."""
        return self.pr_create_phase._run_tests_in_worktree(worktree_path, issue_number)

    def _ensure_pr_created(
        self,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        slot_id: int | None = None,
    ) -> int:
        """Delegate to :meth:`PRCreatePhase._ensure_pr_created`."""
        return self.pr_create_phase._ensure_pr_created(
            issue_number, branch_name, worktree_path, slot_id
        )

    # ReviewPhase
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
        pr_number: int | None = None,
        advise_findings: str = "",
    ) -> tuple[int, str | None, str | None]:
        """Delegate to :meth:`ReviewPhase._run_impl_review_loop`."""
        return self.review_phase._run_impl_review_loop(
            issue_number=issue_number,
            worktree_path=worktree_path,
            branch_name=branch_name,
            issue_title=issue_title,
            issue_body=issue_body,
            session_id=session_id,
            slot_id=slot_id,
            thread_id=thread_id,
            state=state,
            pr_number=pr_number,
            advise_findings=advise_findings,
        )

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
        """Delegate to :meth:`ReviewPhase._run_impl_review_step`."""
        return self.review_phase._run_impl_review_step(
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
        unaddressed_findings: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Delegate to :meth:`ReviewPhase._run_address_review_step`."""
        return self.review_phase._run_address_review_step(
            issue_number=issue_number,
            pr_number=pr_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            iteration=iteration,
            include_bootstrap_context=include_bootstrap_context,
            issue_title=issue_title,
            issue_body=issue_body,
            unaddressed_findings=unaddressed_findings,
        )

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
        """Delegate to :meth:`ReviewPhase._resume_impl_with_feedback`."""
        return self.review_phase._resume_impl_with_feedback(
            session_id=session_id,
            worktree_path=worktree_path,
            issue_number=issue_number,
            review_text=review_text,
            prev_iteration=prev_iteration,
            verdict=verdict,
            state=state,
        )

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
        """Delegate to :meth:`ReviewPhase._run_impl_review`."""
        return self.review_phase._run_impl_review(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            diff_text=diff_text,
            files_changed=files_changed,
            iteration=iteration,
            prior_review=prior_review,
        )

    def _collect_diff(self, worktree_path: Path, branch_name: str) -> str:
        """Delegate to :meth:`ReviewPhase._collect_diff`."""
        return self.review_phase._collect_diff(worktree_path, branch_name)

    def _collect_changed_files(self, worktree_path: Path, branch_name: str) -> str:
        """Delegate to :meth:`ReviewPhase._collect_changed_files`."""
        return self.review_phase._collect_changed_files(worktree_path, branch_name)

    def _save_review_log(self, issue_number: int, iteration: int, review_text: str) -> None:
        """Delegate to :meth:`ReviewPhase._save_review_log`."""
        self.review_phase._save_review_log(issue_number, iteration, review_text)

    def _save_review_iteration_state(
        self, issue_number: int, iterations_run: int, prior_review: str
    ) -> None:
        """Delegate to :meth:`ReviewPhase._save_review_iteration_state`."""
        self.review_phase._save_review_iteration_state(issue_number, iterations_run, prior_review)

    def _load_review_iteration_state(self, issue_number: int) -> tuple[int, str | None]:
        """Delegate to :meth:`ReviewPhase._load_review_iteration_state`."""
        return self.review_phase._load_review_iteration_state(issue_number)

    def _apply_impl_review_verdict(
        self,
        *,
        issue_number: int,
        pr_number: int,
        last_verdict: str | None,
        slot_id: int | None,
        thread_id: int | None,
    ) -> None:
        """Delegate to :meth:`ReviewPhase._apply_impl_review_verdict`."""
        self.review_phase._apply_impl_review_verdict(
            issue_number=issue_number,
            pr_number=pr_number,
            last_verdict=last_verdict,
            slot_id=slot_id,
            thread_id=thread_id,
        )

    def _fetch_plan_and_review(self, issue_number: int) -> tuple[str, str]:
        """Delegate to :meth:`ReviewPhase._fetch_plan_and_review`."""
        return self.review_phase._fetch_plan_and_review(issue_number)

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
        prior_reopened_keys: set[str] | None = None,
    ) -> tuple[list[str], bool, set[str]]:
        """Delegate to :meth:`ReviewPhase._validate_prior_threads`."""
        return self.review_phase._validate_prior_threads(
            issue_number=issue_number,
            pr_number=pr_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            prior_threads=prior_threads,
            iteration=iteration,
            thread_id=thread_id,
            prior_reopened_keys=prior_reopened_keys or set(),
        )

    def _count_unresolved_threads_blocking_go(
        self,
        *,
        issue_number: int,
        pr_number: int,
        thread_id: int | None,
    ) -> tuple[int, int]:
        """Delegate to :meth:`ReviewPhase._count_unresolved_threads_blocking_go`."""
        return self.review_phase._count_unresolved_threads_blocking_go(
            issue_number=issue_number,
            pr_number=pr_number,
            thread_id=thread_id,
        )

    def _parse_address_result(self, text: str, issue_number: int, iteration: int) -> dict[str, Any]:
        """Delegate to :meth:`ReviewPhase._parse_address_result`."""
        return self.review_phase._parse_address_result(text, issue_number, iteration)

    def _commit_if_changes(self, issue_number: int, worktree_path: Path) -> bool:
        """Commit pending changes from the in-loop address step."""
        return commit_if_changes(
            issue_number,
            worktree_path,
            self.options.agent,
            committed_log_message="Committed in-loop address changes for issue #%s",
        )

    def _push_branch(self, branch_name: str, worktree_path: Path) -> None:
        """Push *branch_name* to origin."""
        push_branch(branch_name, worktree_path)

    # FollowUpPhase
    def _run_post_pr_followup(
        self,
        issue_number: int,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
    ) -> None:
        """Delegate to :meth:`FollowUpPhase._run_post_pr_followup`."""
        self.followup_phase._run_post_pr_followup(issue_number, worktree_path, state, slot_id)

    def _parse_follow_up_items(self, text: str) -> list[dict[str, Any]]:
        """Delegate to :meth:`FollowUpPhase._parse_follow_up_items`."""
        return self.followup_phase._parse_follow_up_items(text)

    def _can_resume_state_session(self, state: ImplementationState) -> bool:
        """Delegate to :meth:`FollowUpPhase._can_resume_state_session`."""
        return self.followup_phase._can_resume_state_session(state)

    def _run_follow_up_issues(
        self,
        session_id: str,
        worktree_path: Path,
        issue_number: int,
        slot_id: int | None = None,
        *,
        session_agent: str | None = None,
    ) -> None:
        """Delegate to :meth:`FollowUpPhase._run_follow_up_issues`."""
        self.followup_phase._run_follow_up_issues(
            session_id, worktree_path, issue_number, slot_id, session_agent=session_agent
        )

    def _learn_needs_rerun(self, issue_number: int) -> bool:
        """Delegate to :meth:`FollowUpPhase._learn_needs_rerun`."""
        return self.followup_phase._learn_needs_rerun(issue_number)

    def _rerun_failed_learns(self) -> dict[int, bool]:
        """Delegate to :meth:`FollowUpPhase._rerun_failed_learns`."""
        return self.followup_phase._rerun_failed_learns()

    def _run_learn(
        self,
        session_id: str,
        worktree_path: Path,
        issue_number: int,
        slot_id: int | None = None,
        *,
        session_agent: str | None = None,
    ) -> bool:
        """Delegate to :meth:`FollowUpPhase._run_learn`."""
        return self.followup_phase._run_learn(
            session_id, worktree_path, issue_number, slot_id, session_agent=session_agent
        )
