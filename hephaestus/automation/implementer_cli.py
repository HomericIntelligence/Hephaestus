"""Argument parsing and logging setup for the bulk issue implementer CLI.

This module holds the argument-parsing and logging-setup helpers that were
previously defined inline in :mod:`.implementer`. They were extracted to
separate the CLI plumbing from the
:class:`~hephaestus.automation.implementer.IssueImplementer` orchestration
class (SRP — see #468).

The ``main()`` entry point lives in :mod:`.implementer` (not here): keeping it
beside ``IssueImplementer`` lets it resolve its collaborators
(``gh_list_open_issues``, ``get_repo_root``, ``IssueImplementer``) through that
module's own namespace with no deferred import, which breaks the import cycle
this module's ``main`` previously required (#714). Tests still patch those
collaborators at ``hephaestus.automation.implementer.<name>`` and call
``implementer.main()`` unchanged.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from hephaestus.agents.runtime import add_agent_argument
from hephaestus.automation._review_utils import add_max_workers_arg
from hephaestus.cli.utils import (
    add_dry_run_arg,
    add_json_arg,
    add_version_arg,
)


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


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the implementer CLI."""
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
    add_max_workers_arg(parser)
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
    add_dry_run_arg(
        parser,
        prefix="Suppress GitHub mutations and git pushes (no PR creation, no commits).",
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
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the implementer CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.epic and args.issues:
        parser.error("Cannot specify both --epic and --issues")

    return args
