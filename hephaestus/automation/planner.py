"""``hephaestus-plan-issues`` CLI — a thin wrapper over the queue-based pipeline.

Epic #1809 made the queue-based pipeline
(:mod:`hephaestus.automation.pipeline.coordinator`) the single planning
implementation. This module is now ONLY the console-script entry point: it
parses the historical planner argument surface (``--issues``, ``--parallel``,
``--dry-run``, ``--force``, ``--no-advise``, the agent/timeout/throttle flags),
builds a :class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig`
trimmed to the ``(planning, plan_review)`` stage scope via
:class:`~hephaestus.automation.pipeline.routing.PipelineScope`, and dispatches
to :func:`~hephaestus.automation.pipeline.coordinator.run_pipeline`.

The legacy ``Planner`` / ``PlanReviewLoop`` classes were removed; their
plan → review → learn control flow now lives entirely in
``pipeline/stages/planning.py`` and ``pipeline/stages/plan_review.py``.

Usage:
    hephaestus-plan-issues [--issues N ...] [--parallel N] [--dry-run] \
        [--force] [--no-advise]
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
from .agent_config import fallback_model, planner_model, reviewer_model
from .git_utils import get_repo_info
from .github_api import (
    GitHubRateLimitError,
    gh_list_open_issues,
)
from .pipeline.routing import PipelineScope, StageName

logger = logging.getLogger(__name__)

#: Retryable planner deferral, matching the conventional EX_TEMPFAIL value.
RATE_LIMIT_DEFERRED_EXIT_CODE = 75

#: Contiguous stage subset the planner CLI runs: initial plan generation
#: (PLANNING) followed by the plan review/amend/learn loop (PLAN_REVIEW).
#: PlanReviewStage's ADVANCE target (IMPLEMENTATION) is out of scope, so
#: ``PipelineScope`` rewrites it to FINISHED — a GO'd plan simply finishes.
_PLANNER_SCOPE_STAGES: frozenset[StageName] = frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})


def _setup_logging(verbose: bool = False, log_format: str = "text") -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging.

    """
    configure_cli_logging(verbose=verbose, log_format=log_format)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the planner CLI.

    Extracted so tests can inspect help text without invoking parse_args.
    Preserves the historical ``hephaestus-plan-issues`` flag surface
    (``--issues``, ``--parallel``, ``--dry-run``, ``--force``, ``--no-advise``,
    timeout + GitHub-throttle flags) so pinned callers and the loop runner's
    child-phase argv keep working.
    """
    from pathlib import Path

    parser = build_automation_parser(
        prog="hephaestus-plan-issues",
        description="Bulk plan GitHub issues via the queue-based pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Plan all open issues (no arguments needed)
  %(prog)s

  # Plan specific issues
  %(prog)s --issues 123 456 789

  # Force re-plan even if a plan already exists (at-or-past state:plan-go)
  %(prog)s --issues 123 --force

  # Dry run (classify + preview only, no agent calls or GitHub mutations)
  %(prog)s --issues 123 --dry-run

  # Plan with more parallelism
  %(prog)s --issues 123 456 789 --parallel 5
        """,
        add_max_workers=False,
        add_parallel=True,
        parallel_help="Number of parallel workers, 1-32 (maps to the pipeline worker pool)",
        add_github_throttle=True,
        dry_run_prefix="Suppress GitHub mutations and agent calls (classify + preview only).",
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        help="Issue numbers to plan (default: all open issues)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-planning even when the issue is already at-or-past state:plan-go",
    )
    parser.add_argument(
        "--reset-plan-review-session",
        action="store_true",
        help="Explicitly discard reviewer conversation state for the selected --issues",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        help="(Deprecated, ignored) system prompt file path; kept for CLI compatibility",
    )
    parser.add_argument(
        "--no-skip-closed",
        action="store_true",
        help="(Deprecated, ignored) kept for CLI compatibility; closed issues never queue",
    )
    parser.add_argument(
        "--no-advise",
        action="store_true",
        help="Skip the advise step (don't search team knowledge base before planning)",
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
    add_agent_timeout_arg(parser, default=1200)
    parser.add_argument("--reviewer-model", default="", metavar="MODEL")
    parser.add_argument("--reviewer-timeout", type=positive_int, default=1200, metavar="SECONDS")
    add_pipeline_runtime_args(parser, role="planner", timeouts=("gh", "metadata"))
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the planner CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.reset_plan_review_session and not args.issues:
        parser.error("--reset-plan-review-session requires explicit --issues")
    return args


def _resolve_repo() -> tuple[str, str]:
    """Resolve ``(org, repo)`` for the current checkout.

    Returns:
        The GitHub ``owner`` and ``repo`` name derived from the local remote.

    """
    return get_repo_info()


def main() -> int:
    """Execute the issue planning workflow via the pipeline (planning scope).

    Parses the historical planner argument surface, builds a
    :class:`PipelineConfig` scoped to ``(planning, plan_review)``, seeds the
    requested (or discovered) issues into the planning queue, and runs the
    coordinator.

    Returns:
        Exit code: the coordinator's exit code (0 clean, 1 any
        fail/skip/blocked, 130 interrupt), or
        :data:`RATE_LIMIT_DEFERRED_EXIT_CODE` when open-issue discovery is
        deferred by a GitHub rate limit.

    """
    # Imported here (not at module top) so ``import hephaestus.automation.planner``
    # — and the ``from hephaestus.automation.planner import main`` import-cycle
    # smoke test — stays free of the coordinator's heavier import surface until
    # the CLI actually runs.
    from hephaestus.utils.terminal import install_sigtstp_only

    from .pipeline.coordinator import PipelineConfig, run_pipeline

    install_sigtstp_only()
    args = _parse_args()
    configure_github_throttle_from_args(args)
    _setup_logging(args.verbose, args.log_format)

    log = logging.getLogger(__name__)
    log.info("Starting issue planner (pipeline, planning scope)")
    agent = resolve_agent(
        args.agent,
        disable_pi_automation=args.disable_pi_automation,
        auth_status_timeout=args.auth_status_timeout,
        pi_isolation_adapter=args.pi_isolation_adapter,
        pi_dir=args.pi_dir,
    )

    org, repo = _resolve_repo()

    issues = list(args.issues) if args.issues else []
    if not issues:
        try:
            issues = gh_list_open_issues(timeout=args.gh_timeout)
        except GitHubRateLimitError as e:
            reset_epoch = e.reset_epoch if e.reset_epoch > 0 else None
            reset_description = (
                f"at epoch {reset_epoch}" if reset_epoch is not None else "time unknown"
            )
            log.error(
                "GitHub API rate-limited; planning deferred for open issues in "
                "%s/%s (reset %s). Affected issue numbers are unavailable until "
                "discovery succeeds.",
                org,
                repo,
                reset_description,
            )
            if args.json:
                emit_json_status(
                    RATE_LIMIT_DEFERRED_EXIT_CODE,
                    message="planning deferred by GitHub rate limit",
                    deferred=True,
                    retryable=True,
                    reset_epoch=reset_epoch,
                    affected_issues=None,
                    incomplete_issue_scope={
                        "org": org,
                        "repo": repo,
                        "selection": "all-open-issues",
                    },
                )
            return RATE_LIMIT_DEFERRED_EXIT_CODE
        log.info("No --issues given; discovered %s open issues: %s", len(issues), issues)

    # Dedupe while preserving first-seen order (dict.fromkeys is the canonical
    # "ordered set" trick) so ``--issues 123 123`` never queues the same issue
    # twice.
    issues = list(dict.fromkeys(issues))
    log.info("Issues to plan: %s", issues)

    config = PipelineConfig(
        org=org,
        repos=[repo],
        issues=issues,
        # A single loop pass: the review/amend cycle is bounded in-stage
        # (plan_review_iter / plan_cycles budgets), so the planner CLI does not
        # need multi-loop convergence.
        loops=1,
        # --parallel maps to the pipeline worker-pool size.
        max_workers=args.parallel,
        learning_workers=args.learning_workers,
        learning_queue_capacity=args.learning_queue_capacity,
        dry_run=args.dry_run,
        agent=agent,
        disable_pi_automation=args.disable_pi_automation,
        auth_status_timeout=args.auth_status_timeout,
        model=args.model,
        planner_model=planner_model(args.planner_model or args.model or None),
        reviewer_model=reviewer_model(args.reviewer_model or args.model or None),
        fallback_model=fallback_model(args.fallback_model or args.model or None),
        planner_timeout=args.agent_timeout,
        reviewer_timeout=args.reviewer_timeout,
        no_advise=args.no_advise,
        enable_learn=not args.no_learn,
        projects_dir=resolve_projects_dir(args.projects_dir, prefer_cwd_parent=True),
        rate_guard_enabled=args.rate_guard_enabled,
        rate_guard_threshold=args.rate_guard_threshold,
        gh_timeout=args.gh_timeout,
        metadata_timeout=args.metadata_timeout,
        json_out=args.json,
        scope=PipelineScope(_PLANNER_SCOPE_STAGES),
        # --force re-plans issues already at-or-past state:plan-go (seeding
        # override in the coordinator).
        force=args.force,
        reset_plan_review_sessions=(
            frozenset(issues) if args.reset_plan_review_session else frozenset()
        ),
    )

    rc = run_pipeline(config)
    log.info("Planning complete (rc=%d)", rc)
    return rc


if __name__ == "__main__":
    import sys

    sys.exit(main())
