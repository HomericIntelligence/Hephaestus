"""Command-line entry point for the bulk issue implementer.

This module holds the argument-parsing, logging-setup, and ``main()`` entry
point that were previously defined inline in :mod:`.implementer`. They were
extracted to separate the CLI/entry-point concern from the
:class:`~hephaestus.automation.implementer.IssueImplementer` orchestration
class (SRP — see #468).

``main`` deliberately resolves its patchable collaborators
(``gh_list_open_issues``, ``get_repo_root``, ``CursesUI``, ``IssueImplementer``)
through the :mod:`.implementer` module namespace rather than importing them
directly. This preserves the existing test contract, where tests patch those
names via ``patch.object(implementer, "...")`` and call ``implementer.main()``
— the lookup must happen where those tests patch, not where the symbols are
defined.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from hephaestus.agents.runtime import add_agent_argument, resolve_agent
from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status

from .models import ImplementerOptions


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
        help="Don't enable auto-merge after implementation-review GO",
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
        "--no-advise",
        action="store_true",
        help="Skip the advise step before implementation",
    )
    parser.add_argument(
        "--nitpick",
        action="store_true",
        help="Let the reviewer emit nitpick-severity comments (suppressed by default)",
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
    add_json_arg(parser)
    add_version_arg(parser)

    args = parser.parse_args()

    if args.epic and args.issues:
        parser.error("Cannot specify both --epic and --issues")

    return args


def main() -> int:
    """Execute the issue implementation workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt

    """
    # Resolve patchable collaborators through the implementer module so that
    # existing tests patching ``implementer.gh_list_open_issues`` /
    # ``implementer.get_repo_root`` / ``implementer.CursesUI`` /
    # ``implementer.IssueImplementer`` continue to intercept these lookups.
    from . import implementer as _impl

    args = _parse_args()
    agent = resolve_agent(args.agent)

    state_dir = _impl.get_repo_root() / "build" / ".issue_implementer"
    _setup_logging(args.verbose, log_dir=state_dir)

    log = logging.getLogger(__name__)

    # Auto-discover all open issues when neither --issues nor --epic is given
    if not args.health_check and not args.epic and not args.issues:
        discovered = _impl.gh_list_open_issues()
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

    from hephaestus.utils.terminal import terminal_guard

    with terminal_guard():
        try:
            implementer = _impl.IssueImplementer(options)
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
