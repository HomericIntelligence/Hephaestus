"""``hephaestus-drive-prs-green`` CLI — a thin wrapper over the queue-based pipeline.

Epic #1809 made the queue-based pipeline
(:mod:`hephaestus.automation.pipeline.coordinator`) the single implementation
of the drive-green (``ci`` → ``merge_wait``) flow. This module is now the
console-script entry point only: :func:`main` parses the historical CI-driver
argument surface (``--issues`` / ``--prs``, ``--max-fix-iterations``, the
poll/timeout + GitHub-throttle flags, ``--all`` / bot-PR toggles), builds a
:class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig` trimmed to
the ``(ci, merge_wait)`` stage scope via
:class:`~hephaestus.automation.pipeline.routing.PipelineScope`, seeds the
requested issues / PRs (and, in no-scope discovery mode, the repo-wide failing-PR
sweep via ``drive_green_all``), and dispatches to
:func:`~hephaestus.automation.pipeline.coordinator.run_pipeline`.

The per-issue drive-green orchestration the legacy ``CIDriver`` used to own
(discover → rebase → poll → fix → push → arm → wait-for-merge → post-merge
``/learn``) now lives entirely in ``pipeline/stages/ci.py`` and
``pipeline/stages/merge_wait.py``. The pure classifiers those stages share with
the legacy loop live in ``ci_run_coordinator.py`` (``classify_ci_state`` /
``classify_pr_merge_state``); the PR-discovery semantics live in
``pr_discovery.py``. :class:`CIDriver` is retained as an importable placeholder
for the package's public API surface (:mod:`hephaestus.automation`); it no longer
carries orchestration.

``_pr_is_failing`` is kept as the single canonical "does this open PR need
drive-green attention?" predicate — the loop runner's failing-PR SKIP gate
(``loop_repo_manager._count_failing_prs``) imports it from here so the gate can
never drift from the pipeline's failing-PR sweep.

Usage:
    hephaestus-drive-prs-green [--issues N ...] [--prs N ...] [--dry-run] \
        [--max-fix-iterations N] [--max-workers N] [--all]
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
    add_poll_max_wait_arg,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.config.paths import resolve_projects_dir
from hephaestus.constants import AUTOMATION_LOG_FORMAT, LOG_DATEFMT

from ._review_utils import build_automation_parser
from .ci_check_inspector import (
    FAILING_CHECK_CONCLUSIONS as FAILING_CHECK_CONCLUSIONS,  # re-export
)
from .git_utils import get_repo_slug
from .pipeline.routing import PipelineScope, StageName

logger = logging.getLogger(__name__)

# FAILING_CHECK_CONCLUSIONS lives in ci_check_inspector.py (#1357) and is
# re-exported above for backward compatibility — ``_pr_is_failing`` below and
# ``loop_repo_manager._count_failing_prs`` both consume it from here.

#: Contiguous stage subset the CI-driver CLI runs: the CI drive-green loop
#: (CI) followed by the arm + wait-for-merge + post-merge /learn loop
#: (MERGE_WAIT). CiStage's ADVANCE target (MERGE_WAIT) is in scope, so a
#: green PR flows straight into merge_wait; merge_wait's terminal simply
#: finishes.
_CI_DRIVER_SCOPE_STAGES: frozenset[StageName] = frozenset({StageName.CI, StageName.MERGE_WAIT})


def _pr_is_failing(pr: dict[str, Any]) -> bool:
    """Return True iff this PR row is one drive-green should pick up.

    A PR is "failing" when it is open, non-draft, and either
    mergeStateStatus is BLOCKED or any statusCheckRollup entry's
    conclusion is in FAILING_CHECK_CONCLUSIONS. BLOCKED captures the
    branch-protection/required-review-not-met case; the conclusion check
    captures every CI red. PENDING is intentionally excluded — the driver
    waits for terminal state elsewhere.

    The single canonical failing-PR predicate: the loop runner's SKIP gate
    (``loop_repo_manager._count_failing_prs``) imports this so its "is there
    drive-green work?" check cannot drift from the pipeline's sweep.
    """
    if pr.get("isDraft"):
        return False
    if pr.get("mergeStateStatus") == "BLOCKED":
        return True
    rollup = pr.get("statusCheckRollup") or []
    return any(c.get("conclusion") in FAILING_CHECK_CONCLUSIONS for c in rollup)


class CIDriver:
    """Importable placeholder for the drive-green public API surface.

    Since the epic #1809 pipeline conversion the per-issue drive-green
    orchestration lives entirely in the pipeline stages
    (``pipeline/stages/ci.py`` + ``pipeline/stages/merge_wait.py``), driven by
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
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format=AUTOMATION_LOG_FORMAT,
        datefmt=LOG_DATEFMT,
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the CI-driver CLI.

    Extracted so tests can inspect the flag surface without invoking
    ``parse_args``. Preserves the historical ``hephaestus-drive-prs-green``
    flag surface (``--issues`` / ``--prs``, ``--max-fix-iterations``, the
    ``--all`` / bot-PR toggles, the poll/timeout + GitHub-throttle flags) so
    pinned callers and the loop runner's child-phase argv keep working.
    """
    parser = build_automation_parser(
        description="Drive PRs to green CI: fix failures and enable auto-merge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Discover every failing open PR (issue-driven + bot-PR union, #848)
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
            "number when given. Omit the flag entirely to drive every failing "
            "open PR discovered via gh (issue-linked PRs plus bot-authored PRs)."
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
        help="Skip the advise step before CI fixing",
    )
    parser.add_argument(
        "--no-include-bot-prs",
        dest="include_bot_prs",
        action="store_false",
        default=True,
        help=(
            "Suppress the union of open bot-authored PRs (Dependabot, "
            "github-actions, etc.) into the work set. By default the driver "
            "unions every open is_bot=true PR with the issue-driven list so "
            "Dependabot PRs are not architecturally invisible (#848). Pass "
            "this flag only when you explicitly want issue-driven scope."
        ),
    )
    parser.add_argument(
        "--all",
        dest="include_all_authors",
        action="store_true",
        default=False,
        help=(
            "Include PRs opened by other actors (teammates, bots). Without "
            "this flag, only PRs authored by the authenticated viewer "
            "(`gh api user`) are driven (#821). NOTE: when scoped to issues "
            "(--issues N), the resolved PR is processed regardless of "
            "author — issue-scoped takes precedence."
        ),
    )
    parser.add_argument(
        "--no-mechanical-rebase",
        dest="enable_mechanical_rebase",
        action="store_false",
        default=True,
        help=(
            "Disable the mechanical git rebase that runs before the CI-fix "
            "agent. By default a PR that is behind/conflicting with its base is "
            "rebased and pushed with no agent spend; only PRs whose rebase hits "
            "real conflicts fall through to the agent (#871). Pass this flag to "
            "require the agent for all behind/conflicting PRs."
        ),
    )
    parser.add_argument(
        "--max-fix-iterations",
        type=int,
        default=1,
        help=(
            "Number of CI-fix attempts per failing PR before giving up "
            "(default: 1). The issue-major loop passes its --max-merge-attempts "
            "here so a PR that will not go green is abandoned after N tries."
        ),
    )
    add_agent_timeout_arg(parser)
    add_advise_timeout_arg(parser)
    add_learn_timeout_arg(parser)
    add_poll_max_wait_arg(parser)
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the CI driver CLI."""
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
    """Execute the CI driver workflow via the pipeline (ci → merge_wait scope).

    Parses the historical CI-driver argument surface, builds a
    :class:`PipelineConfig` scoped to ``(ci, merge_wait)``, and dispatches to
    the coordinator. Seeding is coordinator-owned: ``--issues`` route each
    issue's open PR (implementation-go → CI), ``--prs`` route each PR by its
    implementation-go label (``pr_discovery`` semantics), and — in no-scope
    discovery mode (neither ``--issues`` nor ``--prs``) — ``drive_green_all``
    plus repo discovery unions every open failing PR on the repo (the legacy
    failing-PR / bot-PR sweep).

    Returns:
        Exit code: the coordinator's exit code (0 clean, non-zero on
        fail/blocked/needs-action), 130 on keyboard interrupt.

    """
    # Imported here (not at module top) so ``import hephaestus.automation.ci_driver``
    # — and the ``from hephaestus.automation.ci_driver import main`` import-cycle
    # smoke test — stays free of the coordinator's heavier import surface until
    # the CLI actually runs.
    from .pipeline.coordinator import PipelineConfig, run_pipeline

    args = _parse_args()
    configure_github_throttle_from_args(args)
    _setup_logging(args.verbose)
    agent = resolve_agent(args.agent)

    log = logging.getLogger(__name__)
    log.info(
        "Starting CI driver (pipeline, ci→merge_wait scope) for issues: %s, direct PRs: %s",
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
        # coordinator's repo-discovery seed unions every open failing PR on the
        # repo (the legacy failing-PR / bot-PR sweep, #819 / #848) — enabled via
        # drive_green_all. A scoped run (issues or PRs given) stays narrow (POLA).
        drive_green_all = not issues and not prs

        config = PipelineConfig(
            org=org,
            repos=[repo],
            issues=issues,
            prs=prs,
            # A single loop pass: the fix/rebase/address cycles are bounded
            # in-stage (ci_fix / rebase / blocked_address budgets), so the CI
            # driver CLI does not need multi-loop convergence.
            loops=1,
            # --max-workers maps to the pipeline worker-pool size.
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            agent=agent,
            no_advise=args.no_advise,
            drive_green_all=drive_green_all,
            projects_dir=resolve_projects_dir(None, prefer_cwd_parent=True),
            json_out=args.json,
            scope=PipelineScope(_CI_DRIVER_SCOPE_STAGES),
        )

        rc = run_pipeline(config)
        log.info("CI drive complete (rc=%d)", rc)
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
