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

The per-PR review orchestration the legacy ``PRReviewer`` used to own
(discover → worktree → gather-context → analyze → post inline threads → GO/NOGO)
now lives entirely in ``pipeline/stages/pr_review.py``. The pure/parse/context
review cores it shares with the in-loop implementer review step (Stage 2, #28)
live in :mod:`hephaestus.automation.pr_review_core` — this module re-exports
them (``name as name``) so long-pinned patch sites and
``from hephaestus.automation.pr_reviewer import review_pr_inline`` call sites
keep resolving. :class:`PRReviewer` is retained as an importable placeholder for
the package's public API surface (:mod:`hephaestus.automation`); it no longer
carries orchestration.

Usage:
    hephaestus-review-prs --issues N ... [--dry-run] [--max-workers N] [--no-ui]
"""

from __future__ import annotations

import argparse
import logging

from hephaestus.agents.runtime import resolve_agent
from hephaestus.cli.utils import (
    add_agent_timeout_arg,
    add_version_arg,
    configure_cli_logging,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.config.paths import resolve_projects_dir

from ._review_utils import build_review_parser
from .git_utils import get_repo_slug
from .pipeline.routing import PipelineScope, StageName

# The pure/parse/context review cores live in ``pr_review_core`` (unit-covered,
# off the coverage omit-list). They are re-exported here (``name as name``) so
# the long-pinned patch sites — ``hephaestus.automation.pr_reviewer.review_pr_inline``
# / ``.run_pr_review_analysis`` / ``.gather_impl_review_context`` — and the
# ``from hephaestus.automation.pr_reviewer import ...`` call sites keep resolving
# after the #1823 split.
from .pr_review_core import (
    gather_impl_review_context as gather_impl_review_context,
    review_pr_inline as review_pr_inline,
    run_pr_review_analysis as run_pr_review_analysis,
)

logger = logging.getLogger(__name__)

#: Single-stage scope the PR-review CLI runs: the read-only analyze + post-inline
#: + GO/NOGO review loop (PR_REVIEW). PrReviewStage's ADVANCE target
#: (STRICT_REVIEW) is out of scope, so ``PipelineScope`` rewrites it to
#: FINISHED — a GO'd review simply finishes (this CLI does not perform strict
#: review, drive CI, or arm auto-merge).
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


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging.

    """
    configure_cli_logging(verbose=verbose)


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
            "and post inline review comments (read-only — does not fix code)"
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
        dry_run_prefix="Show what would be done without actually posting any review comments.",
    )
    add_version_arg(parser)
    add_agent_timeout_arg(parser)
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the reviewer CLI."""
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
    _setup_logging(args.verbose)
    agent = resolve_agent(args.agent)

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
            projects_dir=resolve_projects_dir(None, prefer_cwd_parent=True),
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
