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
    add_logging_args,
    configure_cli_logging,
    configure_github_throttle_from_args,
    create_parser,
    emit_json_status,
)
from hephaestus.github.client import positive_timeout
from hephaestus.github.fleet_sync.config import resolve_fleet_config
from hephaestus.github.fleet_sync.models import (
    ASCII_SYMBOLS,
    DEFAULT_FLEET_TIMEOUTS,
    UNICODE_SYMBOLS,
    FleetTimeouts,
)
from hephaestus.github.fleet_sync.sync_coordinator import process_repo
from hephaestus.prompts import add_prompt_dir_argument

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for hephaestus-fleet-sync."""
    parser = create_parser(
        prog_name="hephaestus-fleet-sync",
        description="Sync all PRs across a configurable GitHub organization's fleet",
        epilog=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without executing GitHub, Git, or agent mutations",
    )
    parser.add_argument(
        "--org",
        metavar="ORG",
        default=None,
        help="GitHub organization (overrides .fleet.yml)",
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        metavar="REPO",
        default=None,
        help="Restrict to specific repos (overrides .fleet.yml)",
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
    parser.add_argument(
        "--resign-email",
        default=None,
        help="explicit email used when re-signing rewritten commits",
    )
    parser.add_argument(
        "--skip-email-key-check",
        action="store_true",
        help="explicitly bypass matching --resign-email to the configured signing key",
    )
    add_agent_argument(parser)
    add_prompt_dir_argument(parser)
    parser.add_argument(
        "--gh-timeout",
        type=positive_timeout,
        default=DEFAULT_FLEET_TIMEOUTS.gh,
        metavar="SECONDS",
        help="per-call GitHub CLI timeout (default: 120)",
    )
    parser.add_argument(
        "--metadata-timeout",
        type=positive_timeout,
        default=DEFAULT_FLEET_TIMEOUTS.metadata,
        metavar="SECONDS",
        help="local metadata probe timeout (default: 10)",
    )
    parser.add_argument(
        "--network-timeout",
        type=positive_timeout,
        default=DEFAULT_FLEET_TIMEOUTS.network,
        metavar="SECONDS",
        help="Git network operation timeout (default: 120)",
    )
    parser.add_argument(
        "--clone-timeout",
        type=positive_timeout,
        default=DEFAULT_FLEET_TIMEOUTS.clone,
        metavar="SECONDS",
        help="Git clone timeout (default: 120)",
    )
    parser.add_argument(
        "--rebase-timeout",
        type=positive_timeout,
        default=DEFAULT_FLEET_TIMEOUTS.rebase,
        metavar="SECONDS",
        help="Git/agent rebase timeout (default: 2400)",
    )
    add_logging_args(parser)
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
    args.timeouts = FleetTimeouts(
        gh=args.gh_timeout,
        metadata=args.metadata_timeout,
        network=args.network_timeout,
        clone=args.clone_timeout,
        rebase=args.rebase_timeout,
    )
    configure_github_throttle_from_args(args)
    # Dry-run performs no discovery or agent work, so an offline preview must
    # not require provider authentication.
    args.agent = (
        args.agent
        if args.dry_run
        else resolve_agent(
            args.agent,
            disable_pi_automation=args.disable_pi_automation,
            auth_status_timeout=args.auth_status_timeout,
            pi_isolation_adapter=args.pi_isolation_adapter,
            pi_dir=args.pi_dir,
        )
    )
    if args.agent is None:
        args.agent = "codex"

    configure_cli_logging(verbose=args.verbose, log_format=args.log_format)

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
