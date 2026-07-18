"""``hephaestus-drive-prs-green`` CLI — a thin wrapper over the queue-based pipeline.

Epic #1809 made the queue-based pipeline
(:mod:`hephaestus.automation.pipeline.coordinator`) the single implementation
of the drive-green (``strict_review`` → ``merge_wait``) flow. This module is
the console-script entry point: :func:`main` parses its scope and worker
arguments, builds a
:class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig` trimmed to
the ``(strict_review, merge_wait)`` stage scope via
:class:`~hephaestus.automation.pipeline.routing.PipelineScope`, seeds the
requested issues / PRs (and, in no-scope discovery mode, the repo-wide open-PR
sweep via ``drive_green_all``), and dispatches to
:func:`~hephaestus.automation.pipeline.coordinator.run_pipeline`.

The former CI repair/rebase/poll stage was deliberately removed: CI/CD remains
independent branch protection and never supplies automation-loop input. The
remaining stages live in ``pipeline/stages/strict_review.py`` and
``pipeline/stages/merge_wait.py``. :class:`CIDriver` is retained as an
importable placeholder for the package's public API surface
(:mod:`hephaestus.automation`); it no longer carries orchestration.

Usage:
    hephaestus-drive-prs-green [--issues N ...] [--prs N ...] [--dry-run]
        [--max-workers N] [--all] [-v] [--json]
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from hephaestus.agents.runtime import resolve_agent
from hephaestus.cli.utils import (
    add_advise_timeout_arg,
    add_agent_timeout_arg,
    add_learn_timeout_arg,
    configure_cli_logging,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.config.paths import resolve_projects_dir

from ._review_utils import build_automation_parser
from .git_utils import get_repo_slug
from .pipeline.routing import PipelineScope, StageName
from .strict_review_guard import StrictReviewGuard

logger = logging.getLogger(__name__)

#: Contiguous stage subset the historical drive-green CLI runs: in-loop
#: `$athena:pr-review`, then the sole conditional merge-wait arm.
_CI_DRIVER_SCOPE_STAGES: frozenset[StageName] = frozenset(
    {StageName.STRICT_REVIEW, StageName.MERGE_WAIT}
)


def _pr_needs_loop_review(pr: dict[str, Any]) -> bool:
    """Return whether an open non-draft PR is eligible for loop review.

    This intentionally does not read a check, workflow, status, or merge
    state. The loop's own review and approval label are its entire input.
    """
    return not bool(pr.get("isDraft")) and pr.get("state", "OPEN") == "OPEN"


class CIDriver:
    """Importable placeholder for the drive-green public API surface.

    Since the epic #1809 pipeline conversion the per-issue drive-green
    orchestration lives entirely in the pipeline stages
    (``pipeline/stages/strict_review.py`` + ``pipeline/stages/merge_wait.py``), driven by
    :func:`~hephaestus.automation.pipeline.coordinator.run_pipeline` and reached
    from :func:`main`. Nothing instantiates this class at runtime; it is kept
    only so the package's documented public export
    (:mod:`hephaestus.automation`) stays importable.
    """


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging.

    """
    configure_cli_logging(verbose=verbose)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the historical drive-green CLI.

    Extracted so tests can inspect the flag surface without invoking
    ``parse_args``. The supported flags cover issue/PR scope, author and bot
    toggles, worker and agent timeouts, and GitHub throttling.
    """
    parser = build_automation_parser(
        description="Run loop-owned PR review and conditional auto-merge arming",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Discover every open non-draft PR (issue-driven + bot-PR union, #848)
  %(prog)s

  # Scope to specific issues' PRs
  %(prog)s --issues 814 815

  # Drive specific PRs directly
  %(prog)s --prs 661 662 664 666

  # Dry run (no GitHub writes or git pushes)
  %(prog)s --issues 123 --dry-run

  # More parallel workers
  %(prog)s --issues 123 456 --max-workers 5

  # Verbose
  %(prog)s -v

  # Drive every open PR, including teammates' and bots' (default is @me only)
  %(prog)s --all
        """,
        add_github_throttle=True,
        dry_run_prefix=(
            "Suppress GitHub writes and git pushes (no comments, no merges, no pushes)."
        ),
        add_no_ui=True,
        add_version=False,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        default=[],
        help=(
            "Scope to these issue numbers' PRs. Requires at least one issue "
            "number when given. Omit the flag entirely to drive every open "
            "PR discovered via gh (issue-linked PRs plus bot-authored PRs)."
        ),
    )
    parser.add_argument(
        "--prs",
        type=int,
        nargs="*",
        default=[],
        metavar="PR",
        help=(
            "PR numbers to drive directly, bypassing issue-to-PR discovery (#918). "
            "Use when the PR body uses 'Refs #N' or the PR is otherwise not "
            "reachable via the strict Closes-link lookup. May be combined with "
            "--issues; duplicate PRs are deduped."
        ),
    )
    parser.add_argument(
        "--no-advise",
        action="store_true",
        help="Skip the advise step before loop review",
    )
    parser.add_argument(
        "--no-include-bot-prs",
        dest="include_bot_prs",
        action="store_false",
        default=True,
        help=(
            "Exclude open bot-authored PRs (Dependabot, github-actions, etc.) "
            "from the no-scope discovery sweep. Bot-authored PRs are included "
            "by default so they are not architecturally invisible (#848)."
        ),
    )
    parser.add_argument(
        "--all",
        dest="include_all_authors",
        action="store_true",
        default=False,
        help=(
            "Include PRs opened by other actors (teammates and bots). Without "
            "this flag, no-scope discovery drives only PRs authored by the "
            "authenticated viewer (`gh api user`) (#821). Explicit --issues "
            "and --prs scopes are processed regardless of author."
        ),
    )
    add_agent_timeout_arg(parser)
    add_advise_timeout_arg(parser)
    add_learn_timeout_arg(parser)
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the historical drive-green CLI."""
    return _build_parser().parse_args(argv)


def _resolve_repo() -> tuple[str, str]:
    """Resolve ``(org, repo)`` for the current checkout.

    Returns:
        The GitHub ``owner`` and ``repo`` name derived from the local repo
        slug (``owner/repo``).

    """
    slug = get_repo_slug()
    org, _, repo = slug.partition("/")
    return org, repo


def main() -> int:
    """Execute the drive-green workflow via strict_review → merge_wait.

    Parses the historical drive-green argument surface, builds a
    :class:`PipelineConfig` scoped to ``(strict_review, merge_wait)``, and
    dispatches to the coordinator. Seeding is coordinator-owned and uses only
    open-PR state and loop-owned labels; it does not inspect CI/CD.

    Returns:
        Exit code: the coordinator's exit code (0 clean, non-zero on
        fail/blocked/needs-action), 130 on keyboard interrupt.

    """
    # Imported here (not at module top) so ``import hephaestus.automation.ci_driver``
    # — and the ``from hephaestus.automation.ci_driver import main`` import-cycle
    # smoke test — stays free of the coordinator's heavier import surface until
    # the CLI actually runs.
    from hephaestus.utils.terminal import install_sigtstp_only

    from .pipeline.coordinator import PipelineConfig, run_pipeline

    install_sigtstp_only()
    args = _parse_args()
    configure_github_throttle_from_args(args)
    _setup_logging(args.verbose)
    agent = resolve_agent(args.agent)

    log = logging.getLogger(__name__)
    log.info(
        "Starting loop review driver (strict_review→merge_wait) for issues: %s, direct PRs: %s",
        args.issues or "<discovery mode>",
        args.prs,
    )

    try:
        org, repo = _resolve_repo()

        # Dedupe while preserving first-seen order (dict.fromkeys is the
        # canonical "ordered set" trick) so ``--issues 123 123`` / ``--prs 5 5``
        # never queue the same work item twice.
        issues = list(dict.fromkeys(args.issues))
        prs = list(dict.fromkeys(args.prs))

        # No-scope discovery mode: with neither --issues nor --prs, the
        # coordinator's repo-discovery seed unions every open non-draft PR on the
        # repo (the legacy bot-PR sweep, #819 / #848) — enabled via
        # drive_green_all. A scoped run (issues or PRs given) stays narrow (POLA).
        drive_green_all = not issues and not prs

        config = PipelineConfig(
            org=org,
            repos=[repo],
            issues=issues,
            prs=prs,
            # A single loop pass is sufficient: strict review either approves
            # or routes back; merge_wait arms only its direct review handoff.
            loops=1,
            # --max-workers maps to the pipeline worker-pool size.
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            agent=agent,
            no_advise=args.no_advise,
            drive_green_all=drive_green_all,
            include_bot_prs=args.include_bot_prs,
            include_all_authors=args.include_all_authors,
            projects_dir=resolve_projects_dir(None, prefer_cwd_parent=True),
            json_out=args.json,
            strict_review_guard=StrictReviewGuard(),
            scope=PipelineScope(_CI_DRIVER_SCOPE_STAGES),
        )

        rc = run_pipeline(config)
        log.info("Loop review drive complete (rc=%d)", rc)
        if args.json:
            emit_json_status(rc, issues=issues, prs=prs)
        return rc

    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        if args.json:
            emit_json_status(130, message="interrupted")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
