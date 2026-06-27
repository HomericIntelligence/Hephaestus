r"""Bulk issue implementation using the selected coding agent in parallel worktrees.

Provides:
- Dependency-aware parallel implementation
- Git worktree isolation
- State persistence and resume
- CI fix automation

Test-Patch Contract
-------------------
This module owns a **minimal** patch surface.  After the #714 extraction, most
patchable collaborators moved to :mod:`.implementer_phase_runner` (see its
top-level comment block for the full list).  Only two patch paths still target
``hephaestus.automation.implementer.<name>`` in the test suite:

  Symbol            Mechanism                Notes
  ----------------- ------------------------ ----------------------------------------
  get_repo_root     direct import + ``as``   Used by ``IssueImplementer.__init__``
                    alias (mypy re-export)   and ``main``; patched in every test that
                                             constructs an ``IssueImplementer``.
  subprocess.run    stdlib top-level         Patched at the dotted path
                    ``import subprocess``    ``…implementer.subprocess.run`` via
                                             Python's standard attribute-traversal
                                             during ``patch()``.

Keep-in-sync command (run when adding a new patch surface here):

    grep -rn 'patch.*hephaestus\\.automation\\.implementer\\.' tests/ \\
      | grep -v 'implementer_phase_runner\\|implementer_cli\\|implementer_state'

When adding a new patchable dependency to *this* module:

  1. Import it here using ``from .module import name as name`` (mypy
     ``implicit_reexport=false``) so the re-export is explicit.
  2. Add a row to the table above.
  3. If the dependency is also called from :mod:`.implementer_phase_runner`,
     add it there with a top-level import instead — do NOT bridge it back
     through this module (that recreates the #714 cycle).

For the full list of patchable symbols in the phase-runner (``fetch_issue_info``,
``find_pr_for_issue``, ``is_plan_review_go``, ``invoke_claude_with_session``,
``get_repo_slug``, ``AGENT_IMPLEMENTER``, ``AGENT_ADVISE``,
``current_trunk_githash``, ``review_state``, …) see the comment block in
:mod:`.implementer_phase_runner` above its ``from .session_naming import`` line.

Why not constructor-injected collaborators: this module is patched by 16+
existing test call sites across ``test_implementer.py`` and
``test_implementer_loop.py``.  Converting to DI would require editing every
one.  See issue #710's tradeoff analysis and the team's
``python-module-decomposition-and-refactor-patterns`` skill, Phase 11
(Reverse-Delegation), validated by PR #674.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from hephaestus.agents.runtime import (
    agent_cli_name,
    agent_display_name,
)

from ._review_utils import ensure_state_dir

# Imports for the Test-Patch Contract — see module docstring for the full table.
# Each symbol here is either a real call site in IssueImplementer/main or an
# explicit re-export required by tests or the public API.
from .curses_ui import CursesUI, ThreadLogManager
from .dependency_resolver import CyclicDependencyError, DependencyResolver

# Patched at ``hephaestus.automation.implementer.get_repo_root`` by every test
# that constructs an IssueImplementer.  Explicit ``as`` alias satisfies mypy
# ``implicit_reexport=false`` and makes the re-export intentional.
from .git_utils import (
    get_repo_root as get_repo_root,
    run,
)
from .github_api import (
    fetch_issue_info,
    gh_list_open_issues as gh_list_open_issues,
)

# _parse_args / _setup_logging live in implementer_cli (SRP extraction #468).
# Re-exported with explicit ``as`` aliases so tests calling
# ``implementer._parse_args()`` continue to work unchanged.
from .implementer_cli import (
    _parse_args as _parse_args,
    _setup_logging as _setup_logging,
)
from .implementer_phase_runner import (
    MAX_REVIEW_ITERATIONS,
    MAX_REVIEW_ITERATIONS_HARD_CAP,
    ImplementationPhaseRunner,
)
from .implementer_state import ImplementationStateManager
from .implementer_summary import ImplementationSummaryPrinter
from .models import (
    ImplementationState,
    ImplementerOptions,
    WorkerResult,
)
from .pr_manager import commit_changes, create_pr
from .state_labels import is_skipped
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

# Public API of this module. `_CLAUDE_IMPL_TIMEOUT` keeps its leading underscore
# (it is an internal default, not for general use) but is exported because
# tests assert on it as the documented default.
__all__ = [
    "MAX_REVIEW_ITERATIONS",
    "MAX_REVIEW_ITERATIONS_HARD_CAP",
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

    _PHASE_RUNNER_DELEGATES: ClassVar[frozenset[str]] = frozenset(
        {
            "_can_resume_state_session",
            "_ensure_pr_created",
            "_learn_needs_rerun",
            "_load_review_iteration_state",
            "_parse_follow_up_items",
            "_rerun_failed_learns",
            "_run_advise_as_implementer_turn",
            "_run_claude_impl_session",
            "_run_codex_code",
            "_run_follow_up_issues",
            "_run_learn",
            "_run_tests_in_worktree",
            "_save_review_iteration_state",
            "_save_review_log",
        }
    )

    if TYPE_CHECKING:
        _parse_follow_up_items: Callable[..., list[dict[str, Any]]]
        _can_resume_state_session: Callable[..., bool]
        _run_follow_up_issues: Callable[..., None]
        _learn_needs_rerun: Callable[..., bool]
        _rerun_failed_learns: Callable[[], dict[int, bool]]
        _run_learn: Callable[..., bool]
        _run_advise_as_implementer_turn: Callable[..., str]
        _save_review_log: Callable[..., None]
        _save_review_iteration_state: Callable[..., None]
        _load_review_iteration_state: Callable[..., tuple[int, str | None]]
        _run_tests_in_worktree: Callable[..., bool]
        _run_claude_impl_session: Callable[..., str | None]
        _run_codex_code: Callable[..., str | None]
        _ensure_pr_created: Callable[..., int]

    def __init__(self, options: ImplementerOptions):
        """Initialize issue implementer.

        Args:
            options: Implementer configuration options

        """
        self.options = options
        self.repo_root = get_repo_root()
        self.state_dir = ensure_state_dir(self.repo_root)

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

    def __getattr__(self, name: str) -> Any:
        """Resolve audited low-risk phase-runner compatibility delegates."""
        if name in type(self)._PHASE_RUNNER_DELEGATES:
            try:
                phase_runner = object.__getattribute__(self, "phase_runner")
            except AttributeError as exc:
                raise AttributeError(
                    f"{type(self).__name__!r} object has no attribute {name!r}"
                ) from exc
            return getattr(phase_runner, name)

        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

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
                #
                # ``state:skip`` is operator-only and ABSOLUTE (#1576): the
                # automation never removes it and never auto-recovers a skipped
                # issue — it is the operator's responsibility to remove the label
                # between runs. ``issue.labels`` comes from the live
                # ``fetch_issue_info`` (``gh issue view``) call above, never a
                # cache, so the decision always reflects current GitHub state.
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
            from hephaestus.github.client import gh_call

            gh_call(["--version"], check=True, retry_on_rate_limit=False, max_retries=1)
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
        agent_binary = agent_cli_name(self.options.agent)
        agent_name = agent_display_name(self.options.agent)
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

    def _implement_all(self) -> dict[int, WorkerResult]:  # noqa: C901  # orchestration: many retry/outcome paths
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
    # Explicit phase-runner shims.
    #
    # Keep these as real methods because they are central workflow/review
    # checkpoints or heavily patched in tests. Lower-risk support shims are
    # resolved by __getattr__ through _PHASE_RUNNER_DELEGATES.
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

    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Run the advise-first step before implementing (delegates to runner)."""
        return self.phase_runner._run_advise(issue_number, issue_title, issue_body)

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
        unaddressed_findings: list[dict[str, Any]] | None = None,
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

    def _run_claude_code(
        self, issue_number: int, worktree_path: Path, prompt: str, slot_id: int | None = None
    ) -> str | None:
        """Run the selected implementation agent in a worktree."""
        return self.phase_runner._run_claude_code(issue_number, worktree_path, prompt, slot_id)

    def _commit_changes(self, issue_number: int, worktree_path: Path) -> None:
        """Commit changes in worktree."""
        commit_changes(issue_number, worktree_path, self.options.agent)

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


def main() -> int:
    """Execute the issue implementation workflow.

    Relocated from :mod:`.implementer_cli` to break the deferred-import cycle
    (#714): ``main`` resolves ``gh_list_open_issues``, ``get_repo_root``, and
    ``IssueImplementer`` directly through this module's namespace instead of
    importing ``implementer`` lazily from ``implementer_cli``. The console-script
    entry point ``hephaestus.automation.implementer:main`` (declared in
    pyproject.toml) continues to resolve unchanged, and tests patching
    ``implementer.<dep>`` still intercept these lookups.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt

    """
    from hephaestus.agents.runtime import resolve_agent
    from hephaestus.cli.utils import configure_github_throttle_from_args, emit_json_status
    from hephaestus.utils.terminal import terminal_guard

    args = _parse_args()
    configure_github_throttle_from_args(args)
    agent = resolve_agent(args.agent)

    state_dir = ensure_state_dir(get_repo_root())
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
        agent=agent,
        analyze_only=args.analyze,
        health_check=args.health_check,
        resume=args.resume,
        max_workers=args.max_workers,
        skip_closed=not args.no_skip_closed,
        auto_merge=not args.no_auto_merge,
        dry_run=args.dry_run,
        enable_advise=not args.no_advise,
        enable_learn=not args.no_learn,
        enable_follow_up=not args.no_follow_up,
        enable_ui=not args.no_ui and not args.json,
        include_nitpicks=args.nitpick,
    )

    if args.health_check:
        log.info("Running health check")
    elif args.issues:
        log.info("Starting implementation of issues: %s", args.issues)
    else:
        log.info("Starting implementation of epic #%s", args.epic)

    with terminal_guard():
        try:
            implementer = IssueImplementer(options)
            results = implementer.run()

            if not args.health_check and not args.analyze:
                failed = [num for num, result in results.items() if not result.success]
                if failed:
                    log.error("Failed to implement %s issue(s): %s", len(failed), failed)
                    if args.json:
                        emit_json_status(1, issues=args.issues or [], failed=failed)
                    return 1

            log.info("Complete")
            if args.json:
                emit_json_status(0, issues=args.issues or [], epic=args.epic or 0)
            return 0
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            if args.json:
                emit_json_status(130, message="interrupted")
            return 130


if __name__ == "__main__":
    sys.exit(main())
