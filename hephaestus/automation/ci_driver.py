"""``hephaestus-drive-prs-green`` CLI — a thin wrapper over the queue-based pipeline.

Epic #1809 made the queue-based pipeline
(:mod:`hephaestus.automation.pipeline.coordinator`) the single implementation
of the drive-green (``pr_review`` → ``merge_wait``) flow. This module is
the console-script entry point: :func:`main` parses its scope and worker
arguments, builds a
:class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig` trimmed to
the ``(pr_review, merge_wait)`` stage scope via
:class:`~hephaestus.automation.pipeline.routing.PipelineScope`, seeds the
requested issues / PRs (or, in no-scope discovery mode, the repository's
bounded linked-issue source), and dispatches to
:func:`~hephaestus.automation.pipeline.coordinator.run_pipeline`.

It does not enumerate unrelated open PRs. An orphan PR has no issue
requirements and remains out of repository discovery; ``--prs`` can select it
for fail-closed direct evaluation but cannot supply the missing requirements.

The former CI repair/rebase/poll stage was deliberately removed: CI/CD remains
independent branch protection and never supplies automation-loop input. The
remaining stages live in ``pipeline/stages/pr_review.py`` and
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

from hephaestus.agents.runtime import resolve_agent
from hephaestus.cli.utils import (
    add_agent_timeout_arg,
    add_pipeline_runtime_args,
    configure_cli_logging,
    configure_github_throttle_from_args,
    emit_json_status,
    positive_int,
)
from hephaestus.config.paths import resolve_projects_dir

from ._review_utils import build_automation_parser
from .agent_config import fallback_model, reviewer_model
from .git_utils import get_repo_info
from .pipeline.routing import PipelineScope, StageName

logger = logging.getLogger(__name__)

#: Contiguous stage subset the historical drive-green CLI runs. Direct PRs
#: first receive the normal PR review, then merge-wait verifies the proof and
#: conditionally merges only an admitted reviewed head without mutating native
#: auto-merge.
_CI_DRIVER_SCOPE_STAGES: frozenset[StageName] = frozenset(
    {StageName.PR_REVIEW, StageName.MERGE_WAIT}
)


class CIDriver:
    """Importable placeholder for the drive-green public API surface.

    Since the epic #1809 pipeline conversion the per-issue drive-green
    orchestration lives entirely in the pipeline stages
    (``pipeline/stages/pr_review.py`` and ``pipeline/stages/merge_wait.py``), driven by
    :func:`~hephaestus.automation.pipeline.coordinator.run_pipeline` and
    reached from :func:`main`. Nothing instantiates this class at runtime; it
    is kept only so the package's documented public export
    (:mod:`hephaestus.automation`) stays importable.
    """


def _setup_logging(verbose: bool = False, log_format: str = "text") -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging.

    """
    configure_cli_logging(verbose=verbose, log_format=log_format)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the historical drive-green CLI.

    Extracted so tests can inspect the flag surface without invoking
    ``parse_args``. The supported flags cover issue/PR scope, author and bot
    toggles, worker and agent timeouts, and GitHub throttling.
    """
    parser = build_automation_parser(
        description="Run loop-owned PR review and safe merge-wait verification",
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

  # Drive one explicitly selected PR
  %(prog)s --prs 123
        """,
        add_github_throttle=True,
        dry_run_prefix=(
            "Suppress GitHub writes and git pushes (no comments, no merges, no pushes)."
        ),
        add_no_ui=True,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        default=[],
        help=(
            "Scope to these issue numbers' PRs. Requires at least one issue "
            "number when given. Omit the flag to use bounded linked-issue "
            "discovery; unrelated open PRs are not enumerated."
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
            "Each PR must carry the repository-policy 'Closes #N' issue link "
            "so the loop has independent requirements context. May be combined "
            "with --issues; duplicate PRs are deduped."
        ),
    )
    parser.add_argument(
        "--no-advise",
        action="store_true",
        help="Skip the advise step before loop review",
    )
    parser.add_argument(
        "--learning-workers",
        type=positive_int,
        default=1,
        help="Independent auxiliary learning workers (default: 1)",
    )
    parser.add_argument(
        "--learning-queue-capacity",
        type=positive_int,
        default=1,
        help="Bounded auxiliary learning queue capacity (default: 1)",
    )
    parser.add_argument(
        "--no-learn",
        action="store_true",
        help="Do not create or execute auxiliary learning intents",
    )
    parser.add_argument(
        "--no-include-bot-prs",
        dest="include_bot_prs",
        action="store_false",
        default=True,
        help=(
            "Compatibility option retained for the retired open-PR sweep. "
            "No-scope discovery is linked-issue based, so unrelated bot PRs "
            "remain out of scope; use --prs to select a PR explicitly."
        ),
    )
    parser.add_argument(
        "--all",
        dest="include_all_authors",
        action="store_true",
        default=False,
        help=(
            "Compatibility option retained for the retired author-filtered "
            "open-PR sweep. It does not widen linked-issue discovery; explicit "
            "--issues and --prs scopes are processed regardless of author."
        ),
    )
    add_agent_timeout_arg(parser, default=7200)
    add_pipeline_runtime_args(
        parser,
        role="reviewer",
        timeouts=("network", "gh", "metadata", "diff-collect"),
        plugin_skills=True,
    )
    parser.add_argument("--poll-max-wait", type=positive_int, default=1200, metavar="SECONDS")
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the historical drive-green CLI."""
    return _build_parser().parse_args(argv)


def _resolve_repo() -> tuple[str, str]:
    """Resolve ``(org, repo)`` for the current checkout.

    Returns:
        The GitHub ``owner`` and ``repo`` name derived from the local remote.

    """
    return get_repo_info()


def main() -> int:
    """Execute the drive-green workflow via PR review → merge wait.

    Parses the historical drive-green argument surface, builds a
    :class:`PipelineConfig` scoped to ``(pr_review, merge_wait)``, and
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
    _setup_logging(args.verbose, args.log_format)
    agent = resolve_agent(
        args.agent,
        disable_pi_automation=args.disable_pi_automation,
        auth_status_timeout=args.auth_status_timeout,
        pi_isolation_adapter=args.pi_isolation_adapter,
        pi_dir=args.pi_dir,
    )

    log = logging.getLogger(__name__)
    log.info(
        "Starting loop review driver (pr_review→merge_wait) for issues: %s, direct PRs: %s",
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

        # No-scope discovery mode keeps the compatibility flag true, but the
        # coordinator now consumes only the bounded linked-issue source. It
        # never enumerates unrelated open PRs; a scoped run stays narrow (POLA).
        drive_green_all = not issues and not prs

        config = PipelineConfig(
            org=org,
            repos=[repo],
            issues=issues,
            prs=prs,
            # A single loop pass is sufficient: in-loop review either applies
            # the eligibility label or routes back; merge_wait separately
            # consumes the current proof and operator authorization.
            loops=1,
            # --max-workers maps to the pipeline worker-pool size.
            max_workers=args.max_workers,
            learning_workers=args.learning_workers,
            learning_queue_capacity=args.learning_queue_capacity,
            dry_run=args.dry_run,
            agent=agent,
            disable_pi_automation=args.disable_pi_automation,
            auth_status_timeout=args.auth_status_timeout,
            model=args.model,
            reviewer_model=reviewer_model(args.reviewer_model or args.model or None),
            fallback_model=fallback_model(args.fallback_model or args.model or None),
            reviewer_timeout=args.agent_timeout,
            implementer_timeout=args.agent_timeout,
            no_advise=args.no_advise,
            enable_learn=not args.no_learn,
            drive_green_all=drive_green_all,
            include_bot_prs=args.include_bot_prs,
            include_all_authors=args.include_all_authors,
            projects_dir=resolve_projects_dir(args.projects_dir, prefer_cwd_parent=True),
            rate_guard_enabled=args.rate_guard_enabled,
            rate_guard_threshold=args.rate_guard_threshold,
            plugin_skills_dir=args.plugin_skills_dir,
            network_timeout=args.network_timeout,
            gh_timeout=args.gh_timeout,
            metadata_timeout=args.metadata_timeout,
            diff_collect_timeout=args.diff_collect_timeout,
            poll_max_wait=args.poll_max_wait,
            json_out=args.json,
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
