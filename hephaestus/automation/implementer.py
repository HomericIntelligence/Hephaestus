r"""``hephaestus-implement-issues`` CLI — a thin wrapper over the queue-based pipeline.

Epic #1809 made the queue-based pipeline
(:mod:`hephaestus.automation.pipeline.coordinator`) the single implementation
of the plan → implement → review → CI → merge flow. This module is now the
console-script entry point only: :func:`main` parses the historical implementer
argument surface (``--issues``, ``--epic``, ``--max-workers``, ``--dry-run``,
the ``--no-*`` toggles, timeout + GitHub-throttle flags), builds a
:class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig` trimmed to
the ``(implementation, pr_review)`` stage scope via
:class:`~hephaestus.automation.pipeline.routing.PipelineScope`, seeds the
requested (or discovered) issues, and dispatches to
:func:`~hephaestus.automation.pipeline.coordinator.run_pipeline`.

The seeding classifier enforces the plan-go gate: an issue that is not yet at
``state:plan-go`` classifies to PLANNING, which is out of the implementation
scope and is therefore clamped to FINISHED(pass) by the coordinator — only an
at-or-past ``state:plan-go`` issue seeds into the IMPLEMENTATION queue. The
per-issue implementation / review sequencing that the legacy phase runner used
to own now lives entirely in ``pipeline/stages/implementation.py`` and
``pipeline/stages/pr_review.py``.

:class:`IssueImplementer` is retained as a slim session-setup helper (dependency
resolution + worktree / state / status bookkeeping) — the per-issue phase runner
and end-of-run summary printer were removed with the pipeline conversion.

Usage:
    hephaestus-implement-issues [--issues N ...] [--epic N] [--dry-run] \
        [--max-workers N] [--no-advise] [--no-learn] [--no-follow-up]
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
from pathlib import Path

from hephaestus.agents.runtime import (
    agent_cli_name,
    agent_display_name,
    resolve_agent,
)
from hephaestus.cli.utils import (
    add_advise_timeout_arg,
    add_agent_timeout_arg,
    add_follow_up_timeout_arg,
    add_git_message_timeout_arg,
    add_learn_timeout_arg,
)
from hephaestus.config.paths import resolve_projects_dir
from hephaestus.constants import AUTOMATION_LOG_FORMAT, LOG_DATEFMT
from hephaestus.logging.utils import setup_logging

from ._review_utils import build_automation_parser, ensure_state_dir
from .agent_config import AGENT_IMPL_TIMEOUT
from .dependency_resolver import CyclicDependencyError, DependencyResolver

# Patched at ``hephaestus.automation.implementer.get_repo_root`` by every test
# that constructs an IssueImplementer.  Explicit ``as`` alias satisfies mypy
# ``implicit_reexport=false`` and makes the re-export intentional.
from .git_utils import (
    get_repo_root as get_repo_root,
    run,
)
from .github_api import (
    GitHubRateLimitError,
    fetch_issue_info,
    gh_list_open_issues as gh_list_open_issues,
)
from .implementer_state import ImplementationStateManager
from .models import (
    ImplementationState,
    ImplementerOptions,
    WorkerResult,
)
from .state_labels import is_skipped
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

# Public API of this module. ``_CLAUDE_IMPL_TIMEOUT`` keeps its leading
# underscore (it is an internal default, not for general use) but is exported
# because tests assert on it as the documented default.
__all__ = [
    "_CLAUDE_IMPL_TIMEOUT",
    "IssueImplementer",
    "main",
]

# Default implementation timeout in seconds. Actual runtime value comes from
# ``options.agent_timeout`` (set via ``--agent-timeout`` CLI flag or the
# ``ImplementerOptions.agent_timeout`` default, which defaults to
# ``AGENT_IMPL_TIMEOUT``). This constant serves as the documented default and
# can be used in tests.
_CLAUDE_IMPL_TIMEOUT: int = AGENT_IMPL_TIMEOUT


logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool = False, log_dir: Path | None = None) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging.
        log_dir: Optional directory to write a ``run.log`` file into.

    """
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(
        level=logging.DEBUG if verbose else logging.INFO,
        log_file=str(log_dir / "run.log") if log_dir else None,
        format_string=AUTOMATION_LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        primary_stream="stderr",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the implementer CLI.

    Extracted so tests can inspect the flag surface without invoking
    ``parse_args``. Preserves the historical ``hephaestus-implement-issues``
    flag surface (``--issues``, ``--epic``, ``--max-workers``, ``--dry-run``,
    the ``--no-*`` toggles, timeout + GitHub-throttle flags) so pinned callers
    and the loop runner's child-phase argv keep working.
    """
    parser = build_automation_parser(
        description="Bulk implement GitHub issues using Claude Code or Codex",
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
        add_github_throttle=True,
        dry_run_prefix="Suppress GitHub mutations and git pushes (no PR creation, no commits).",
        add_no_ui=True,
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
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="(Deprecated, ignored) kept for CLI compatibility; analysis lives in the pipeline",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Run health check of dependencies and environment",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="(Deprecated, ignored) kept for CLI compatibility; the pipeline resumes from state",
    )
    parser.add_argument(
        "--no-skip-closed",
        action="store_true",
        help="Implement closed issues (default: skip closed issues)",
    )
    parser.add_argument(
        "--no-auto-merge",
        action="store_true",
        help="(Deprecated, ignored) merge arming is owned by merge_wait",
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
        "--no-advise",
        action="store_true",
        help="Skip the advise step before implementation",
    )
    parser.add_argument(
        "--nitpick",
        action="store_true",
        help="Let the reviewer emit nitpick-severity comments (suppressed by default)",
    )
    add_agent_timeout_arg(parser)
    add_advise_timeout_arg(parser)
    add_git_message_timeout_arg(parser)
    add_learn_timeout_arg(parser)
    add_follow_up_timeout_arg(parser)
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the implementer CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.epic and args.issues:
        parser.error("Cannot specify both --epic and --issues")

    return args


class IssueImplementer:
    """Slim session-setup helper for issue implementation.

    Since the epic #1809 pipeline conversion the per-issue implementation /
    review sequencing lives in the pipeline stages
    (``pipeline/stages/implementation.py`` + ``pr_review.py``), not here. What
    remains is the session bootstrap the pipeline and its tests reuse:
    dependency resolution, worktree isolation, and state/status bookkeeping.

    Features:
    - Dependency resolution and topological ordering
    - Isolated git worktree management
    - State persistence for resume capability
    """

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

        self.state_mgr = ImplementationStateManager(self.state_dir)

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

    @property
    def state_manager(self) -> ImplementationStateManager:
        """Return the component that owns implementation state persistence."""
        return self.state_mgr

    def _log(self, level: str, msg: str, thread_id: int | None = None) -> None:
        """Log to the standard logger.

        Args:
            level: Log level ("error", "warning", or "info")
            msg: Message to log
            thread_id: Unused; retained for signature compatibility.

        """
        getattr(logger, level)(msg)

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


def _resolve_repo() -> tuple[str, str]:
    """Resolve ``(org, repo)`` for the current checkout.

    Returns:
        The GitHub ``owner`` and ``repo`` name derived from the local repo
        slug (``owner/repo``).

    """
    from .git_utils import get_repo_slug

    slug = get_repo_slug()
    org, _, repo = slug.partition("/")
    return org, repo


def main() -> int:
    """Execute the issue implementation workflow via the pipeline.

    Parses the historical implementer argument surface, builds a
    :class:`PipelineConfig` scoped to ``(implementation, pr_review)``, seeds the
    requested (or discovered) issues into the implementation queue (the plan-go
    gate is enforced by the seeding classifier), and runs the coordinator.

    ``--health-check`` short-circuits to the standalone environment probe and
    never dispatches to the pipeline.

    Returns:
        Exit code: the coordinator's exit code (0 clean, non-zero on
        fail/skip/blocked), 0 on a clean rate-limited skip or a health check,
        130 on keyboard interrupt.

    """
    from hephaestus.cli.utils import configure_github_throttle_from_args, emit_json_status

    # Imported here (not at module top) so ``import hephaestus.automation.implementer``
    # — and the ``from hephaestus.automation.implementer import main`` import-cycle
    # smoke test — stays free of the coordinator's heavier import surface until
    # the CLI actually runs.
    from hephaestus.utils.terminal import install_sigtstp_only

    from .pipeline.coordinator import PipelineConfig, run_pipeline
    from .pipeline.routing import PipelineScope, StageName

    install_sigtstp_only()
    args = _parse_args()
    configure_github_throttle_from_args(args)
    agent = resolve_agent(args.agent)

    state_dir = ensure_state_dir(get_repo_root())
    _setup_logging(args.verbose, log_dir=state_dir)

    log = logging.getLogger(__name__)

    # ``--health-check`` is a standalone environment probe; it never touches
    # the pipeline. Mirrors the legacy behavior (best-effort probes, always
    # exit 0).
    if args.health_check:
        log.info("Running health check")
        options = ImplementerOptions(
            issues=[],
            agent=agent,
            health_check=True,
            max_workers=args.max_workers,
        )
        IssueImplementer(options)._health_check()
        if args.json:
            emit_json_status(0, message="health-check")
        return 0

    log.info("Starting issue implementer (pipeline, implementation scope)")

    org, repo = _resolve_repo()

    issues = list(args.issues) if args.issues else []
    if not issues and not args.epic:
        try:
            issues = gh_list_open_issues()
        except GitHubRateLimitError as e:
            # Don't smear a traceback across the driver's loop output when the
            # only problem is that the GraphQL hourly budget is gone. Exit
            # cleanly so the outer loop moves on to the next repo.
            log.error(
                "GitHub API rate-limited; cannot discover issues this run "
                "(reset at epoch %s). Skipping cleanly.",
                e.reset_epoch,
            )
            if args.json:
                emit_json_status(0, message="rate-limited; skipped", reset_epoch=e.reset_epoch)
            return 0
        log.info("No --issues/--epic given; discovered %s open issues: %s", len(issues), issues)

    # Dedupe while preserving first-seen order (dict.fromkeys is the canonical
    # "ordered set" trick) so ``--issues 123 123`` never queues the same issue
    # twice.
    issues = list(dict.fromkeys(issues))
    log.info("Issues to implement: %s", issues)

    config = PipelineConfig(
        org=org,
        repos=[repo],
        issues=issues,
        # A single loop pass: the review/address cycle is bounded in-stage
        # (pr_review_iter / pr_review_hard budgets), so the implementer CLI does
        # not need multi-loop convergence.
        loops=1,
        # --max-workers maps to the pipeline worker-pool size.
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        agent=agent,
        no_advise=args.no_advise,
        nitpick=args.nitpick,
        projects_dir=resolve_projects_dir(None, prefer_cwd_parent=True),
        json_out=args.json,
        scope=PipelineScope(
            frozenset({StageName.IMPLEMENTATION, StageName.PR_REVIEW, StageName.MERGE_WAIT})
        ),
    )

    rc = run_pipeline(config)
    log.info("Implementation complete (rc=%d)", rc)
    if args.json:
        emit_json_status(rc, issues=issues, epic=args.epic or 0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
