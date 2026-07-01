"""Command-line entry point for fleet sync."""

from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

from hephaestus.agents.runtime import add_agent_argument, resolve_agent
from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    configure_github_throttle_from_args,
    create_parser,
    emit_json_status,
)
from hephaestus.github.fleet_sync.config import resolve_fleet_config
from hephaestus.github.fleet_sync.models import ASCII_SYMBOLS, UNICODE_SYMBOLS
from hephaestus.github.fleet_sync.sync_coordinator import process_repo
from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for hephaestus-fleet-sync."""
    parser = create_parser(
        prog_name="hephaestus-fleet-sync",
        description="Sync all PRs across a configurable GitHub organization's fleet",
        epilog=None,
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument(
        "--org",
        metavar="ORG",
        default=None,
        help="GitHub organization (overrides FLEET_ORG and .fleet.yml)",
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        metavar="REPO",
        default=None,
        help="Restrict to specific repos (overrides FLEET_REPOS and .fleet.yml)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=str,
        default=None,
        help="Path to fleet config YAML (default: ./.fleet.yml then repo-root .fleet.yml)",
    )
    parser.add_argument(
        "--skip-conflict-resolution",
        action="store_true",
        help="Skip agent conflict resolution for conflicted PRs",
    )
    add_agent_argument(parser)
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument(
        "--ascii",
        action="store_true",
        help=(
            "Use ASCII fallbacks (==, *, ->, --) instead of Unicode "
            "box/check/arrow/dash glyphs in log output; use when piping "
            "stdout to ASCII-only consumers."
        ),
    )
    add_github_throttle_args(parser)
    add_json_arg(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for hephaestus-fleet-sync."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_github_throttle_from_args(args)
    args.agent = resolve_agent(args.agent)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        org, repos = resolve_fleet_config(args.org, args.repos, args.config)
    except RuntimeError as e:
        logger.error("%s", e)
        if args.json:
            emit_json_status(2, str(e))
        return 2

    args.org = org
    args.repos = repos
    dry_tag = " [DRY RUN]" if args.dry_run else ""
    symbols = ASCII_SYMBOLS if args.ascii else UNICODE_SYMBOLS
    logger.info("Fleet sync %s org=%s, %d repo(s)%s", symbols.dash, org, len(repos), dry_tag)

    totals: dict[str, int] = {
        "merged": 0,
        "rebased": 0,
        "conflict_resolved": 0,
        "skipped": 0,
        "failed": 0,
    }

    with tempfile.TemporaryDirectory(prefix="hephaestus-fleet-") as tmp:
        clone_dir = Path(tmp)
        for repo in repos:
            counts = process_repo(repo, org, args, clone_dir, symbols=symbols)
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v

    logger.info("\n%s", "=" * 60)
    logger.info("Fleet sync complete")
    logger.info("  Merged:            %d", totals["merged"])
    logger.info("  Rebased+re-signed: %d", totals["rebased"])
    logger.info("  Conflicts resolved:%d", totals["conflict_resolved"])
    logger.info("  Skipped:           %d", totals["skipped"])
    logger.info("  Failed:            %d", totals["failed"])

    exit_code = 0 if totals["failed"] == 0 else 1
    if args.json:
        emit_json_status(exit_code, None, repos=len(repos), totals=totals)
    return exit_code
