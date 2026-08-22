"""``hephaestus-review-prs`` CLI — a thin wrapper over the queue-based pipeline.

Epic #1809 made the queue-based pipeline
(:mod:`hephaestus.automation.pipeline.coordinator`) the single implementation
of the PR-review flow. This module is now the console-script entry point only:
:func:`main` parses the historical reviewer argument surface
(``--issues``, ``--agent``, ``--max-workers``, ``--no-ui``, throttle flags),
builds a :class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig`
trimmed to the ``pr_review`` stage scope via
:class:`~hephaestus.automation.pipeline.routing.PipelineScope`, seeds the
requested issues, and dispatches to
:func:`~hephaestus.automation.pipeline.coordinator.run_pipeline`.

The per-PR review orchestration lives entirely in
``pipeline/stages/pr_review.py``. The pure/parse/context review cores it
shares with the in-loop implementer review step (Stage 2, #28) live in
:mod:`hephaestus.automation.pr_review_core`. This module is only the CLI
entry point; it does not expose a direct review-thread lifecycle.

Usage:
    hephaestus-review-prs --issues N ... [--dry-run] [--max-workers N] [--no-ui]
"""

from __future__ import annotations

import argparse
import logging

from hephaestus.agents.runtime import resolve_agent
from hephaestus.cli.utils import (
    add_agent_timeout_arg,
    add_pipeline_runtime_args,
    add_version_arg,
    configure_cli_logging,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.config.paths import resolve_projects_dir

from ._review_utils import build_review_parser
from .agent_config import fallback_model, reviewer_model
from .git_utils import get_repo_info
from .pipeline.routing import PipelineScope, StageName

logger = logging.getLogger(__name__)

#: Single-stage scope the PR-review CLI runs. Reviewer agents are read-only,
#: but the stage owns the complete two-role lifecycle: it may direct the
#: implementation agent to fix open threads, post its replies after a push,
#: and reconcile the reviewer's fresh decision. Its ADVANCE target is out of
#: scope, so ``PipelineScope`` rewrites it to FINISHED — this CLI never arms
#: auto-merge.
_PR_REVIEWER_SCOPE_STAGES: frozenset[StageName] = frozenset({StageName.PR_REVIEW})


class PRReviewer:
    """Importable placeholder for the PR-review public API surface.

    Since the epic #1809 pipeline conversion the per-PR review orchestration
    lives entirely in the pipeline PR-review stage
    (``pipeline/stages/pr_review.py``), driven by
    :func:`~hephaestus.automation.pipeline.coordinator.run_pipeline` and reached
    from :func:`main`. Nothing instantiates this class at runtime; it is kept
    only so the package's documented public export
    (:mod:`hephaestus.automation`) stays importable.
    """


def _setup_logging(verbose: bool = False, log_format: str = "text") -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging.

    """
    configure_cli_logging(verbose=verbose, log_format=log_format)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the PR reviewer CLI.

    Extracted so tests can inspect the flag surface without invoking
    ``parse_args``. Preserves the historical ``hephaestus-review-prs`` flag
    surface (``--issues``, ``--agent``, ``--max-workers``, ``--no-ui``, the
    ``--agent-timeout`` / GitHub-throttle flags) so pinned callers keep working.
    """
    parser = build_review_parser(
        description=(
            "Analyze open PRs linked to GitHub issues using Claude Code or Codex "
            "and run the PR review/remediation lifecycle; reviewer agents are read-only, "
            "while the coordinator may apply implementation fixes and reconcile threads"
        ),
        epilog="""
Examples:
  # Review PRs for specific issues
  %(prog)s --issues 595 596

  # Review with dry run
  %(prog)s --issues 595 --dry-run

  # Review with more workers
  %(prog)s --issues 595 596 --max-workers 5
        """,
        issues_help="Issue numbers whose linked PRs should be reviewed",
        dry_run_prefix=(
            "Show the review/remediation lifecycle without GitHub mutations or git pushes."
        ),
    )
    add_version_arg(parser)
    add_agent_timeout_arg(parser, default=1200)
    add_pipeline_runtime_args(
        parser,
        role="reviewer",
        timeouts=("network", "gh", "metadata", "diff-collect"),
        plugin_skills=True,
    )
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the reviewer CLI."""
    return _build_parser().parse_args(argv)


def _resolve_repo() -> tuple[str, str]:
    """Resolve ``(org, repo)`` for the current checkout.

    Returns:
        The GitHub ``owner`` and ``repo`` name derived from the local remote.

    """
    return get_repo_info()


def main() -> int:
    """Execute the PR review workflow via the pipeline (pr_review scope).

    Parses the historical reviewer argument surface, builds a
    :class:`PipelineConfig` scoped to ``pr_review``, seeds the requested issues
    into the PR-review queue, and runs the coordinator.

    Returns:
        Exit code: the coordinator's exit code (0 clean, non-zero on
        fail/blocked/needs-action), 130 on keyboard interrupt.

    """
    # Imported here (not at module top) so ``import hephaestus.automation.pr_reviewer``
    # — and the ``from hephaestus.automation.pr_reviewer import main`` import-cycle
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
    log.info("Starting PR review (pipeline, pr_review scope) for issues: %s", args.issues)

    try:
        org, repo = _resolve_repo()

        # Dedupe while preserving first-seen order (dict.fromkeys is the
        # canonical "ordered set" trick) so ``--issues 123 123`` never queues
        # the same issue twice.
        issues = list(dict.fromkeys(args.issues))

        config = PipelineConfig(
            org=org,
            repos=[repo],
            issues=issues,
            # A single loop pass: the review/amend cycle is bounded in-stage
            # (pr_review_iter budget), so the reviewer CLI does not need
            # multi-loop convergence.
            loops=1,
            # --max-workers maps to the pipeline worker-pool size.
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            agent=agent,
            disable_pi_automation=args.disable_pi_automation,
            auth_status_timeout=args.auth_status_timeout,
            model=args.model,
            reviewer_model=reviewer_model(args.reviewer_model or args.model or None),
            fallback_model=fallback_model(args.fallback_model or args.model or None),
            reviewer_timeout=args.agent_timeout,
            projects_dir=resolve_projects_dir(args.projects_dir, prefer_cwd_parent=True),
            rate_guard_enabled=args.rate_guard_enabled,
            rate_guard_threshold=args.rate_guard_threshold,
            plugin_skills_dir=args.plugin_skills_dir,
            network_timeout=args.network_timeout,
            gh_timeout=args.gh_timeout,
            metadata_timeout=args.metadata_timeout,
            diff_collect_timeout=args.diff_collect_timeout,
            json_out=args.json,
            scope=PipelineScope(_PR_REVIEWER_SCOPE_STAGES),
        )

        rc = run_pipeline(config)
        log.info("PR review complete (rc=%d)", rc)
        if args.json:
            emit_json_status(rc, issues=issues)
        return rc

    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        if args.json:
            emit_json_status(130, message="interrupted")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
