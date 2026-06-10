"""Bulk issue implementation using the selected coding agent in parallel worktrees.

Provides:
- Dependency-aware parallel implementation
- Git worktree isolation
- State persistence and resume
- CI fix automation
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    is_codex,
)

# ---------------------------------------------------------------------------
# Test-patch contract (load-bearing — DO NOT remove a shim without checking
# the call site AND every test it intercepts; see #710 for the refactor plan
# that will eventually replace this with dependency injection).
#
# Why these re-exports exist:
#   - ``implementer_phase_runner`` (the runtime call site) deliberately does
#     NOT import these symbols directly. Instead, it dynamically looks them
#     up via ``ImplementationPhaseRunner._impl_module`` (see
#     ``implementer_phase_runner.py:188-199``), which returns *this* module.
#   - That indirection means a test calling
#     ``patch("hephaestus.automation.implementer.X", ...)`` intercepts the
#     runtime call inside the phase runner too — without the runner needing
#     a constructor-injected collaborator.
#   - When the shim is removed or renamed, the patch silently no-ops and
#     tests that depended on it will start exercising real network / disk.
#
# Maintenance rule: each shim below lists (1) the runtime call site that
# reaches it via ``_impl_module``, and (2) the tests that patch it. Update
# both columns when you add, remove, or rename a shim. Line citations are
# correct as of the commit that introduced them; if they have drifted,
# re-run ``grep -rn 'patch.*implementer\.<symbol>' tests/``.
# ---------------------------------------------------------------------------
# NOTE: ``review_state`` is accessed as a *module reference*, not patched via
# ``patch("implementer.review_state")``.  Re-exporting it here ensures the
# runtime lookup at ``implementer_phase_runner.py:1199``
# (``_impl_module.review_state``) resolves to the real module object.
# Tests control ``review_state`` behaviour by patching its internal functions
# directly (not by replacing the module reference):
#   monkeypatch.setattr(review_state_mod, "_fetch_issue_comments_graphql", …)
#   → test_implementer_loop.py:{865,879}
from . import (  # noqa: F401  # test-patch shim — see contract above
    review_state,
)

# Both re-exported as test-patch shims, reached at runtime via ``_impl_module``:
#   - ``find_pr_for_issue``: locate the open PR for an issue.
#       Patched by: tests/unit/automation/test_implementer.py:{274,354,390,435,460};
#                   tests/unit/automation/test_implementer_loop.py:559
#   - ``get_pr_head_branch``: resolve a PR's REAL head branch so
#       ``_review_existing_pr`` never assumes ``{issue}-auto-impl`` (the assumption
#       makes ``git fetch`` fail with exit 128 when the PR lives on another branch).
# Runtime call site: ``implementer_phase_runner.py`` (via ``_impl_module``).
from ._review_utils import (
    find_pr_for_issue,  # noqa: F401  # test-patch shim
    get_pr_head_branch,  # noqa: F401  # test-patch shim
)

# Patched by: tests/unit/automation/test_implementer_loop.py:{324,346,366,388}
# Runtime call site: ``implementer_phase_runner.py:{855,1305,1580}`` (via ``_impl_module``)
from .claude_invoke import invoke_claude_with_session  # noqa: F401  # test-patch shim
from .curses_ui import CursesUI, ThreadLogManager
from .dependency_resolver import CyclicDependencyError, DependencyResolver

# ``get_repo_root`` is re-exported with an explicit ``as`` alias so that
# ``implementer_cli.main`` (which resolves it via this module) and tests
# patching ``implementer.get_repo_root`` share one lookup site.
#
# Patched by: tests/unit/automation/test_implementer.py:{91,161,197,250,427,518,539,563};
#             tests/unit/automation/test_implementer_loop.py:30
# Runtime call site: ``implementer.py:134`` (``IssueImplementer.__init__``)
#                    + ``implementer_cli.main``
from .git_utils import (
    get_repo_root as get_repo_root,
)

# Patched by: tests/unit/automation/test_implementer_loop.py:316
# Runtime call site: ``implementer_phase_runner.py:{854,1303,1577}`` (via ``_impl_module``)
# ``run`` is a real call site (not just a shim) for ``IssueImplementer._health_check``
# (see ``implementer.py:294,301``); it does double duty as a patch surface.
from .git_utils import (
    get_repo_slug,  # noqa: F401  # test-patch shim
    run,
)

# ``fetch_issue_info`` is a real call site (``IssueImplementer._load_issues``
# at ``implementer.py:270``), not a shim.
from .github_api import fetch_issue_info

# ``gh_list_open_issues`` is re-exported with an explicit ``as`` alias so
# ``implementer_cli.main`` (which looks it up here) and tests patching
# ``implementer.gh_list_open_issues`` share one lookup site.
#
# Patched by: indirectly via ``implementer_cli.main``'s auto-discovery path
# Runtime call site: ``implementer_cli.main`` (lazy lookup through this module)
from .github_api import (
    gh_list_open_issues as gh_list_open_issues,
)

# ``MAX_REVIEW_ITERATIONS`` is re-exported so tests that import it via
# ``hephaestus.automation.implementer`` see the same value the runtime loop
# in :class:`ImplementationPhaseRunner` uses. Single source of truth lives
# in ``implementer_phase_runner``.
#
# The CLI entry point (argument parsing, logging setup, and ``main``) lives
# in ``implementer_cli`` (extracted for SRP — see #468). Re-exported here
# with explicit ``as`` aliases (required by mypy) for two reasons:
#   1. Console script: ``hephaestus-implement-issues`` is wired to
#      ``hephaestus.automation.implementer:main`` in ``pyproject.toml``
#      (verified at tests/integration/test_orchestration_smoke.py:37).
#   2. Test compatibility: existing tests call ``implementer.main()`` /
#      ``implementer._parse_args()`` and patch dependencies at
#      ``implementer.<dep>`` paths.
# ``main`` imports this module lazily (inside its body) so this top-level
# import is cycle-safe.
# Patched by: tests/unit/automation/test_implementer.py (various)
# Runtime call site: console script entry point ``hephaestus-implement-issues``
from .implementer_cli import (
    _parse_args as _parse_args,
)
from .implementer_cli import (
    _setup_logging as _setup_logging,
)
from .implementer_cli import (
    main as main,
)
from .implementer_phase_runner import MAX_REVIEW_ITERATIONS, ImplementationPhaseRunner
from .implementer_state import ImplementationStateManager
from .implementer_summary import ImplementationSummaryPrinter
from .models import (
    ImplementationState,
    ImplementerOptions,
    WorkerResult,
)
from .pr_manager import commit_changes, create_pr

# Patched by: tests/unit/automation/test_implementer.py:{278,358,394,467};
#             tests/unit/automation/test_implementer_loop.py:560
# Runtime call site: ``implementer_phase_runner.py:314`` (via ``_impl_module``)
from .review_state import is_plan_review_go  # noqa: F401  # test-patch shim

# Patched by: tests/unit/automation/test_implementer_loop.py:{316,318};
#             see comment at tests/unit/automation/test_phase_agent_wiring.py:51
# Runtime call site: phase runner agent dispatch + session naming, both via
# ``_impl_module``
from .session_naming import AGENT_ADVISE, AGENT_IMPLEMENTER, current_trunk_githash  # noqa: F401
from .state_labels import is_skipped
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

# Public API of this module. `_CLAUDE_IMPL_TIMEOUT` keeps its leading underscore
# (it is an internal default, not for general use) but is exported because
# tests assert on it as the documented default.
__all__ = [
    "MAX_REVIEW_ITERATIONS",
    "_CLAUDE_IMPL_TIMEOUT",
    "IssueImplementer",
    "main",
]

# Default implementation timeout in seconds. Actual runtime value is read from
# ``HEPH_IMPLEMENTER_AGENT_TIMEOUT`` by
# :func:`.claude_timeouts.implementer_claude_timeout`; this constant serves as
# the documented default and can be used in tests.
_CLAUDE_IMPL_TIMEOUT: int = 1800


logger = logging.getLogger(__name__)


class IssueImplementer:
    """Implements GitHub issues in parallel using the selected coding agent.

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
        self.state_dir = self.repo_root / "build" / ".issue_implementer"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.resolver = DependencyResolver(skip_closed=options.skip_closed)
        self.worktree_manager = WorktreeManager()
        self.status_tracker = StatusTracker(options.max_workers)
        self.log_manager = ThreadLogManager()

        self.state_mgr = ImplementationStateManager(self.state_dir)
        self.phase_runner = ImplementationPhaseRunner(self)

        self.ui: CursesUI | None = None

    # ------------------------------------------------------------------
    # Compatibility shims: callers that pre-date the #597 state-manager
    # extraction reach into ``self.states`` / ``self.state_lock`` directly.
    # Expose them as read-only views onto the manager so behavior is
    # identical.
    # ------------------------------------------------------------------

    @property
    def states(self) -> dict[int, ImplementationState]:
        """Return the in-memory state dict owned by :attr:`state_mgr`."""
        return self.state_mgr.states

    @property
    def state_lock(self) -> threading.Lock:
        """Return the lock guarding :attr:`states`."""
        return self.state_mgr.lock

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

    def run(self) -> dict[int, WorkerResult]:  # noqa: C901  # branchy orchestration body is intentionally linear
        """Run the implementer.

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        # Health check mode
        if self.options.health_check:
            return self._health_check()

        # Short-circuit when there's nothing to implement. The CLI's auto-
        # discovery branch (implementer.main → gh_list_open_issues) sets
        # ``args.issues = []`` for repos with zero open issues, then defaults
        # ``epic_number=0``. Without this guard we fall through to
        # ``load_epic(0)`` and pay 5 retries × exponential backoff against
        # ``gh issue view 0`` before crashing — wasting ~12s per empty repo
        # and dominating wall-clock for the parallel-repos loop. Mirrors
        # ``planner.py:107-108`` which warns and returns ``{}`` on the same
        # condition. See #574.
        if not self.options.issues and not self.options.epic_number:
            logger.warning("No issues to implement (repo has no open issues / nothing discovered)")
            return {}

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
        from .github_api import prefetch_issue_states

        # Prefetch states for efficiency
        cached_states = prefetch_issue_states(issue_numbers)

        for issue_num in issue_numbers:
            issue_state = cached_states.get(issue_num)
            if self.options.skip_closed and issue_state is not None and issue_state.is_done:
                logger.info("Skipping closed issue #%s", issue_num)
                self.resolver.completed.add(issue_num)
                continue

            try:
                issue = fetch_issue_info(issue_num)

                # Manual override (#1083): a ``state:skip`` label removes the
                # issue from all phases. Treat it as completed so dependents are
                # not blocked, and never add it to the work graph.
                if is_skipped(issue.labels):
                    logger.info("Skipping #%s (state:skip)", issue_num)
                    self.resolver.completed.add(issue_num)
                    continue

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

                        if result.success and result.plan_review_not_go:
                            # Deferred: plan exists but latest review is not GO.
                            # Do NOT mark completed — dependents must still wait,
                            # and the issue will be retried on the next
                            # automation loop after re-review. See #551.
                            logger.info(
                                "Issue #%s deferred: waiting for GO plan-review",
                                issue_num,
                            )
                        elif result.success:
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

    # ------------------------------------------------------------------
    # Per-issue phase runner — bodies live in implementer_phase_runner.py.
    # Each shim here forwards to the runner; the runner dispatches all
    # cross-method calls back through ``self.impl._xxx`` so test idioms
    # like ``patch.object(impl, "_has_plan", ...)`` keep intercepting
    # every call site, including the ones inside ``_implement_issue``.
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
        return self.phase_runner._finalize_pr(
            issue_number, branch_name, worktree_path, state, slot_id
        )

    def _run_post_pr_followup(
        self,
        issue_number: int,
        worktree_path: Path,
        state: ImplementationState,
        slot_id: int | None,
    ) -> None:
        """Run /learn and file follow-up issues after the PR is created."""
        self.phase_runner._run_post_pr_followup(issue_number, worktree_path, state, slot_id)

    def _implement_issue(self, issue_number: int) -> WorkerResult:
        """Implement a single issue."""
        return self.phase_runner._implement_issue(issue_number)

    def _has_plan(self, issue_number: int) -> bool:
        """Check if issue has an implementation plan."""
        return self.phase_runner._has_plan(issue_number)

    def _generate_plan(self, issue_number: int) -> None:
        """Generate plan for an issue using hephaestus-plan-issues."""
        self.phase_runner._generate_plan(issue_number)

    def _parse_follow_up_items(self, text: str) -> list[dict[str, Any]]:
        """Parse follow-up items from Claude's JSON response."""
        return self.phase_runner._parse_follow_up_items(text)

    def _can_resume_state_session(self, state: ImplementationState) -> bool:
        """Return True when the saved session can be resumed by the selected agent."""
        return self.phase_runner._can_resume_state_session(state)

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
        self.phase_runner._run_follow_up_issues(
            session_id,
            worktree_path,
            issue_number,
            slot_id,
            session_agent=session_agent,
        )

    def _learn_needs_rerun(self, issue_number: int) -> bool:
        """Check if learn log indicates failure."""
        return self.phase_runner._learn_needs_rerun(issue_number)

    def _rerun_failed_learns(self) -> dict[int, bool]:
        """Re-run failed learns for completed issues."""
        return self.phase_runner._rerun_failed_learns()

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
        return self.phase_runner._run_learn(
            session_id,
            worktree_path,
            issue_number,
            slot_id,
            session_agent=session_agent,
        )

    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Run the advise-first step before implementing (delegates to runner)."""
        return self.phase_runner._run_advise(issue_number, issue_title, issue_body)

    def _run_advise_as_implementer_turn(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        worktree_path: Path,
    ) -> str:
        """Advise turn 1 of the implementer session (delegates to runner)."""
        return self.phase_runner._run_advise_as_implementer_turn(
            issue_number, issue_title, issue_body, worktree_path
        )

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
        """Run the bounded review loop for an implementation."""
        return self.phase_runner._run_impl_review_loop(
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
        """Run one in-loop review (posts inline PR threads) and return its verdict."""
        return self.phase_runner._run_impl_review_step(
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
    ) -> bool:
        """Address the posted PR threads in-loop, resuming Session 2."""
        return self.phase_runner._run_address_review_step(
            issue_number=issue_number,
            pr_number=pr_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            iteration=iteration,
            include_bootstrap_context=include_bootstrap_context,
            issue_title=issue_title,
            issue_body=issue_body,
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
        """Resume the impl session and feed reviewer feedback as the next prompt."""
        return self.phase_runner._resume_impl_with_feedback(
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
        """Run a fresh-session reviewer against the current impl diff."""
        return self.phase_runner._run_impl_review(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            diff_text=diff_text,
            files_changed=files_changed,
            iteration=iteration,
            prior_review=prior_review,
        )

    def _collect_diff(self, worktree_path: Path, branch_name: str) -> str:
        """Return the cumulative diff of *branch_name* against ``origin/main``."""
        return self.phase_runner._collect_diff(worktree_path, branch_name)

    def _collect_changed_files(self, worktree_path: Path, branch_name: str) -> str:
        """Return a newline-separated list of changed files vs ``origin/main``."""
        return self.phase_runner._collect_changed_files(worktree_path, branch_name)

    def _save_review_log(self, issue_number: int, iteration: int, review_text: str) -> None:
        """Persist a per-iteration review log for later inspection."""
        self.phase_runner._save_review_log(issue_number, iteration, review_text)

    def _save_review_iteration_state(
        self, issue_number: int, iterations_run: int, prior_review: str
    ) -> None:
        """Persist review loop progress for ``--resume`` continuity (A2-005)."""
        self.phase_runner._save_review_iteration_state(issue_number, iterations_run, prior_review)

    def _load_review_iteration_state(self, issue_number: int) -> tuple[int, str | None]:
        """Load persisted review iteration progress for ``--resume`` (A2-005)."""
        return self.phase_runner._load_review_iteration_state(issue_number)

    def _run_tests_in_worktree(self, worktree_path: Path, issue_number: int) -> bool:
        """Run the unit test suite inside the worktree as a pre-PR gate (A2-004)."""
        return self.phase_runner._run_tests_in_worktree(worktree_path, issue_number)

    def _run_claude_code(
        self, issue_number: int, worktree_path: Path, prompt: str, slot_id: int | None = None
    ) -> str | None:
        """Run the selected implementation agent in a worktree."""
        return self.phase_runner._run_claude_code(issue_number, worktree_path, prompt, slot_id)

    def _run_claude_impl_session(
        self, issue_number: int, worktree_path: Path, prompt: str
    ) -> str | None:
        """Run Claude implementation prompt and return its session id."""
        return self.phase_runner._run_claude_impl_session(issue_number, worktree_path, prompt)

    def _run_codex_code(self, issue_number: int, worktree_path: Path, prompt: str) -> str | None:
        """Run Codex implementation prompt in a worktree."""
        return self.phase_runner._run_codex_code(issue_number, worktree_path, prompt)

    def _commit_changes(self, issue_number: int, worktree_path: Path) -> None:
        """Commit changes in worktree."""
        commit_changes(issue_number, worktree_path, self.options.agent)

    def _ensure_pr_created(
        self,
        issue_number: int,
        branch_name: str,
        worktree_path: Path,
        slot_id: int | None = None,
    ) -> int:
        """Ensure commit is pushed and PR is created (fallback if Claude didn't do it)."""
        return self.phase_runner._ensure_pr_created(
            issue_number, branch_name, worktree_path, slot_id
        )

    def _create_pr(self, issue_number: int, branch_name: str) -> int:
        """Create pull request for issue."""
        return create_pr(
            issue_number,
            branch_name,
            auto_merge=False,
            agent=self.options.agent,
        )

    def _get_or_create_state(self, issue_number: int) -> ImplementationState:
        """Get or create implementation state for an issue."""
        return self.state_mgr.get_or_create(issue_number)

    def _get_state(self, issue_number: int) -> ImplementationState | None:
        """Get implementation state for an issue."""
        return self.state_mgr.get(issue_number)

    def _save_state(self, state: ImplementationState) -> None:
        """Save implementation state to disk."""
        self.state_mgr.save(state)

    def _load_state(self) -> None:
        """Load all implementation states from disk."""
        self.state_mgr.load_all()

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print implementation summary."""
        ImplementationSummaryPrinter(self.worktree_manager).print(results)


if __name__ == "__main__":
    sys.exit(main())
