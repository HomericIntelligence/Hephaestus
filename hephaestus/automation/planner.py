"""Bulk issue planning using the selected coding agent.

Provides:
- Parallel issue planning
- Duplicate plan detection
- Rate limit handling
- Plan posting to GitHub issues
"""

from __future__ import annotations

import argparse
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    resolve_agent,
    uses_direct_agent_runner,
)
from hephaestus.cli.utils import (
    add_advise_timeout_arg,
    add_agent_timeout_arg,
    add_git_message_timeout_arg,
    configure_github_throttle_from_args,
    emit_json_status,
)

from ._review_utils import (
    build_automation_parser,
    close_issue_as_covered,
    find_merged_closing_pr,
    find_pr_for_issue,
)
from .advise_runner import advise_skipped, ensure_mnemosyne, run_advise
from .claude_models import advise_model, codex_advise_model
from .claude_timeouts import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    planner_claude_timeout,
)
from .git_utils import issue_ref
from .github_api import (
    GitHubRateLimitError,
    gh_issue_upsert_comment,
    gh_list_open_issues,
)
from .models import PLAN_COMMENT_MARKER, PlannerOptions, PlanResult
from .planner_claude import PlannerClaudeRunner
from .planner_review_loop import MAX_REVIEW_ITERATIONS, PlanReviewLoop
from .planner_state import PlannerStateManager
from .prompts import get_advise_prompt_builder
from .review_state import is_plan_review_go
from .session_naming import AGENT_ADVISE
from .status_tracker import StatusTracker
from .work_report import work_report_context

__all__ = ["MAX_REVIEW_ITERATIONS", "Planner", "main"]

logger = logging.getLogger(__name__)


class Planner:
    """Plans GitHub issues using Claude Code or Codex.

    Supports parallel planning with rate limit handling and
    duplicate detection.
    """

    def __init__(self, options: PlannerOptions):
        """Initialize planner.

        Args:
            options: Planner configuration options

        """
        self.options = options
        self.status_tracker = StatusTracker(options.parallel)
        self.results: dict[int, PlanResult] = {}
        self.lock = threading.Lock()
        self.state_mgr = PlannerStateManager(options)
        self.claude_runner = PlannerClaudeRunner(options)
        self.review_loop = PlanReviewLoop(self)

    def run(self) -> dict[int, PlanResult]:
        """Run the planner on all issues.

        Returns:
            Dictionary mapping issue number to PlanResult

        """
        logger.info(
            "Planning %s issues with %s parallel workers",
            len(self.options.issues),
            self.options.parallel,
        )

        # Filter closed issues if requested
        issues_to_plan = self._filter_issues()

        if not issues_to_plan:
            logger.warning("No issues to plan")
            return {}

        # Plan issues in parallel
        with ThreadPoolExecutor(max_workers=self.options.parallel) as executor:
            futures: dict[Future[Any], int] = {}

            for issue_num in issues_to_plan:
                future = executor.submit(self._plan_issue, issue_num)
                futures[future] = issue_num

            # Collect results
            for future in as_completed(futures):
                issue_num = futures[future]
                try:
                    result = future.result()
                    with self.lock:
                        self.results[issue_num] = result
                except Exception as e:
                    logger.error("Failed to plan %s: %s", issue_ref(issue_num), e)
                    with self.lock:
                        self.results[issue_num] = PlanResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

        self._print_summary()
        return self.results

    def _filter_issues(self) -> list[int]:
        """Filter issues based on options (delegates to state manager)."""
        return self.state_mgr.filter()

    def _has_existing_plan(self, issue_number: int) -> bool:
        """Skip the planner when the issue is already in ``state:plan-go``.

        Labels-first gate (#704). An issue counts as "already planned" iff
        ``is_plan_review_go`` returns True, which is now keyed on the
        ``state:plan-go`` label (with a one-time comment-scan backfill for
        issues that converged before the labels rollout). Issues in
        ``state:plan-no-go`` or with no state label at all are re-planned so
        the loop drives them toward GO without churning on GO'd ones.

        Reuses the labels batch-fetched in :meth:`PlannerStateManager.filter`
        (passed as ``issue_labels=``) so this check costs no extra round-trip
        for issues whose labels are already cached.
        """
        cached_labels = self.state_mgr.get_cached_labels(issue_number)
        return is_plan_review_go(issue_number, issue_labels=cached_labels)

    def _pr_coverage_skip(self, issue_number: int, slot_id: int) -> PlanResult | None:
        """Skip planning when a PR already covers the issue (FM1 idempotency guard).

        Two gaps closed here, both observed in the 2026-06-15 automation-loop run:

        1. **Open PR exists** — an open PR already closes this issue, so planning
           it again wastes an agent call and risks a duplicate/zombie PR. Mirrors
           the implementer's existing open-PR skip via the shared
           :func:`find_pr_for_issue` (branch-name → review-state → ``Closes #N``
           body search with the exact ``^Closes #N`` post-filter).
        2. **Merged closing PR, issue still OPEN** — a closing PR merged with a
           valid ``Closes #N`` line yet the issue never auto-closed. Close the
           issue (idempotently) and skip rather than re-implement landed work.

        The merged-PR gate is checked first: a merged PR is the stronger signal
        (work has landed), and closing the issue prevents the loop from churning
        on it next run.

        Args:
            issue_number: Issue under consideration.
            slot_id: Worker slot for status-tracker updates.

        Returns:
            A ``PlanResult`` (success, ``plan_already_exists=True``) when the
            issue should be skipped, or ``None`` when planning should proceed.

        """
        # Gate A: a merged PR already closed (or should have closed) this issue.
        merged_pr = find_merged_closing_pr(issue_number)
        if merged_pr is not None:
            logger.info(
                "Issue #%s: merged PR #%s already closes it — closing issue and skipping plan",
                issue_number,
                merged_pr,
            )
            close_issue_as_covered(issue_number, merged_pr)
            self.status_tracker.update_slot(
                slot_id,
                f"{issue_ref(issue_number)}: merged PR #{merged_pr} covers it, closed + skipped",
            )
            return PlanResult(
                issue_number=issue_number,
                success=True,
                plan_already_exists=True,
            )

        # Gate B: an open PR already covers this issue.
        open_pr = find_pr_for_issue(issue_number)
        if open_pr is not None:
            logger.info(
                "Issue #%s: open PR #%s already covers it — skipping plan",
                issue_number,
                open_pr,
            )
            self.status_tracker.update_slot(
                slot_id,
                f"{issue_ref(issue_number)}: open PR #{open_pr} covers it, skipped",
            )
            return PlanResult(
                issue_number=issue_number,
                success=True,
                plan_already_exists=True,
            )

        return None

    def _plan_issue(self, issue_number: int) -> PlanResult:
        """Plan a single issue.

        Args:
            issue_number: Issue number to plan

        Returns:
            PlanResult

        """
        slot_id = self.status_tracker.acquire_slot()
        if slot_id is None:
            return PlanResult(
                issue_number=issue_number,
                success=False,
                error="Failed to acquire worker slot",
            )

        try:
            # Idempotency guard (FM1): never (re-)plan an issue whose work is
            # already covered by a PR. Runs BEFORE the existing-plan/force gates
            # because an open/merged closing PR makes planning pure churn — the
            # 2026-06-15 loop burned ~5.5h re-planning #1357/#1289/#1179 while
            # their PRs were open (and again after they merged). This mirrors the
            # implementer phase's "Skipped (open PR already exists)" semantics so
            # plan and implement agree on what to skip. Localized to this guard
            # block (no _call_claude changes) for FM3 retry-logic coordination.
            covered = self._pr_coverage_skip(issue_number, slot_id)
            if covered is not None:
                return covered

            # Skip-if-already-planned moved here from _filter_issues (#548) so
            # the check runs inside the thread pool (parallel, overlapped with
            # actual planning work) instead of as a serial pre-pass that
            # blocked all workers behind N ``gh issue view --comments`` calls.
            if not self.options.force:
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: checking existing plan"
                )
                if self._has_existing_plan(issue_number):
                    logger.info("Issue #%s already has a plan, skipping", issue_number)
                    self.status_tracker.update_slot(
                        slot_id, f"{issue_ref(issue_number)}: plan exists, skipped"
                    )
                    return PlanResult(
                        issue_number=issue_number,
                        success=True,
                        plan_already_exists=True,
                    )

            self.status_tracker.update_slot(slot_id, f"Planning {issue_ref(issue_number)}")

            if self.options.dry_run:
                logger.info("[DRY RUN] Would plan %s", issue_ref(issue_number))
                return PlanResult(issue_number=issue_number, success=True)

            # Run the strict review loop: advise → loop[plan → learn → review]
            # → post final plan with last review attached. Loop terminates on
            # the first unambiguous GO or after MAX_REVIEW_ITERATIONS.
            plan, final_review, iterations, verdict_is_go = self._run_plan_review_loop(
                issue_number, slot_id
            )

            # Post final plan + review to issue regardless of verdict so
            # operators can see what was produced (NOGO banner is appended
            # inside _post_plan when verdict_is_go is False).
            self._post_plan(
                issue_number, plan, final_review=final_review, verdict_is_go=verdict_is_go
            )

            self.status_tracker.update_slot(
                slot_id, f"Completed {issue_ref(issue_number)} ({iterations} iter)"
            )

            if not verdict_is_go:
                return PlanResult(
                    issue_number=issue_number,
                    success=False,
                    error=(
                        "review loop exhausted all iterations without a GO verdict (NOGO-exhausted)"
                    ),
                )

            return PlanResult(issue_number=issue_number, success=True)

        except Exception as e:
            logger.error("Failed to plan %s: %s", issue_ref(issue_number), e)
            return PlanResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )
        finally:
            self.status_tracker.release_slot(slot_id)

    def _call_claude(
        self,
        prompt: str,
        *,
        model: str,
        agent: str,
        issue_number: int | str,
        max_retries: int = 3,
        timeout: int | None = None,
        extra_args: list[str] | None = None,
    ) -> str:
        """Call Claude (delegates to claude_runner)."""
        return self.claude_runner.call_claude(
            prompt,
            model=model,
            agent=agent,
            issue_number=issue_number,
            max_retries=max_retries,
            timeout=planner_claude_timeout() if timeout is None else timeout,
            extra_args=extra_args,
        )

    def _ensure_mnemosyne(self, mnemosyne_root: Path) -> bool:
        """Clone or refresh ProjectMnemosyne (delegates to the shared runner).

        Thin wrapper around :func:`advise_runner.ensure_mnemosyne` kept on the
        Planner so the existing ``patch.object(planner, "_ensure_mnemosyne")``
        test seam still intercepts.

        Args:
            mnemosyne_root: Expected local path for ProjectMnemosyne.

        Returns:
            True if the directory exists (or was cloned successfully), else False.

        """
        return ensure_mnemosyne(mnemosyne_root)

    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Search team knowledge base for relevant prior learnings.

        Delegates the Mnemosyne setup + prompt construction to the shared
        :mod:`advise_runner`, supplying the planner's own ``_call_claude`` (under
        ``AGENT_ADVISE``, the cheap read-only advise session) as the invoker.
        Kept as a method so the ``patch.object(planner, "_run_advise")`` test
        seam — and the review loop's ``self.planner._run_advise(...)`` call —
        keep working.

        Args:
            issue_number: Issue number.
            issue_title: Issue title.
            issue_body: Issue body/description.

        Returns:
            Advise findings text, or an ``advise_skipped`` marker on failure.

        """

        def _invoke(prompt: str) -> str:
            if uses_direct_agent_runner(self.options.agent):
                return self.claude_runner.call_direct_agent(
                    prompt,
                    model=direct_agent_model(
                        self.options.agent,
                        "HEPH_ADVISE_MODEL",
                        codex_default=codex_advise_model(),
                    ),
                    timeout=self.options.advise_timeout,
                    sandbox="read-only",
                )
            # Advise is light search work, so it runs on the cheap model with a
            # short timeout under its own AGENT_ADVISE session.
            return self._call_claude(
                prompt,
                model=advise_model(),
                agent=AGENT_ADVISE,
                issue_number=issue_number,
                timeout=self.options.advise_timeout,
            )

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=_invoke,
            build_prompt=get_advise_prompt_builder(self.options.agent),
        )

    @staticmethod
    def _advise_skipped(reason: str) -> str:
        """Return the advise skip marker (delegates to the shared runner)."""
        return advise_skipped(reason)

    def _generate_plan(
        self,
        issue_number: int,
        max_retries: int = 3,
        *,
        prior_review: str | None = None,
        cached_advise: str | None = None,
        cached_issue_data: dict[str, Any] | None = None,
    ) -> str:
        """Generate implementation plan (delegates to review loop)."""
        return self.review_loop.generate_plan(
            issue_number,
            max_retries=max_retries,
            prior_review=prior_review,
            cached_advise=cached_advise,
            cached_issue_data=cached_issue_data,
        )

    def _post_plan(
        self,
        issue_number: int,
        plan: str,
        *,
        final_review: str | None = None,
        verdict_is_go: bool = True,
    ) -> None:
        """Upsert the single PLAN comment on the issue.

        Updates the issue's one ``# Implementation Plan`` comment in place
        (via :func:`gh_issue_upsert_comment`) rather than appending a new one.
        The review loop already upserts an in-progress plan comment every
        iteration; this final upsert overwrites it with the canonical body
        (NOGO banner + plan + final-review collapsible + footer), so after the
        planner finishes the issue holds exactly one comment starting with
        ``# Implementation Plan`` reflecting the final iteration. The separate
        ``## 🔍 Plan Review`` comment is owned by the loop.

        Args:
            issue_number: Issue number
            plan: Plan text
            final_review: When set, the last reviewer output (Grade + Verdict +
                rationale) is appended in a collapsible section so the human
                reviewer can see why the loop terminated.
            verdict_is_go: When ``False`` a visible NOGO banner is prepended to
                the comment so operators can tell at a glance that the review loop
                exhausted all iterations without approval (#369).

        """
        nogo_banner = ""
        if not verdict_is_go:
            nogo_banner = (
                "> [!WARNING]\n"
                "> **NOGO-EXHAUSTED** — The strict review loop ran all "
                f"{MAX_REVIEW_ITERATIONS} iterations without an unambiguous GO verdict. "
                "This plan was posted for operator review but **should not be implemented** "
                "until a human approves it.\n\n"
            )

        comment_body = f"""# Implementation Plan

{nogo_banner}{plan}
"""

        if final_review:
            comment_body += f"""
---

<details>
<summary>Final review verdict (from strict review loop)</summary>

{final_review}

</details>
"""

        comment_body += """
---
*Generated by Claude Code Planner (strict review loop)*
"""

        gh_issue_upsert_comment(issue_number, PLAN_COMMENT_MARKER, comment_body)
        logger.info("Posted plan to %s", issue_ref(issue_number))

    # ------------------------------------------------------------------
    # Strict review loop — delegations to PlanReviewLoop
    # ------------------------------------------------------------------

    def _run_plan_review_loop(
        self, issue_number: int, slot_id: int
    ) -> tuple[str, str | None, int, bool]:
        """Run the bounded review loop (delegates to review_loop)."""
        return self.review_loop.run(issue_number, slot_id)

    def _capture_planner_learnings(self, issue_number: int, plan: str) -> str:
        """Capture planner learnings (delegates to review_loop)."""
        return self.review_loop.capture_planner_learnings(issue_number, plan)

    def _run_plan_review(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        plan_text: str,
        learnings: str,
        iteration: int,
        prior_review: str | None,
        advise_findings: str = "",
    ) -> str:
        """Run reviewer pass (delegates to review_loop)."""
        return self.review_loop.run_plan_review(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
            learnings=learnings,
            iteration=iteration,
            prior_review=prior_review,
            advise_findings=advise_findings,
        )

    def _print_summary(self) -> None:
        """Print summary of planning results."""
        total = len(self.results)
        successful = sum(1 for r in self.results.values() if r.success)
        already_planned = sum(1 for r in self.results.values() if r.plan_already_exists)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("Planning Summary")
        logger.info("=" * 60)
        logger.info("Total issues: %s", total)
        logger.info("Successfully planned: %s", successful - already_planned)
        logger.info("Already planned: %s", already_planned)
        logger.info("Failed: %s", failed)

        if failed > 0:
            logger.info("\nFailed issues:")
            for issue_num, result in self.results.items():
                if not result.success:
                    logger.info("  #%s: %s", issue_num, result.error)


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging

    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the planner CLI.

    Extracted so tests can inspect help text without invoking parse_args.
    """
    from pathlib import Path

    parser = build_automation_parser(
        description="Bulk plan GitHub issues using Claude Code or Codex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Plan all open issues (no arguments needed)
  %(prog)s

  # Plan specific issues
  %(prog)s --issues 123 456 789

  # Force re-plan even if plan exists
  %(prog)s --issues 123 --force

  # Dry run (no actual planning)
  %(prog)s --issues 123 --dry-run

  # Use custom system prompt
  %(prog)s --issues 123 --system-prompt .claude/agents/planner.md

  # Plan with more parallelism
  %(prog)s --issues 123 456 789 --parallel 5
        """,
        add_max_workers=False,
        add_parallel=True,
        parallel_help="Number of parallel workers, 1-32 (default: 3)",
        add_github_throttle=True,
        dry_run_prefix="Suppress GitHub mutations and agent calls (no issue comments posted).",
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
        help="Force re-planning even if plan already exists",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        help="Path to system prompt file for Claude Code",
    )
    parser.add_argument(
        "--no-skip-closed",
        action="store_true",
        help="Plan closed issues (default: skip closed issues)",
    )
    parser.add_argument(
        "--no-advise",
        action="store_true",
        help="Skip the advise step (don't search team knowledge base before planning)",
    )
    add_agent_timeout_arg(parser)
    add_advise_timeout_arg(parser)
    add_git_message_timeout_arg(parser)
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the planner CLI."""
    return _build_parser().parse_args(argv)


def main() -> int:
    """Execute the issue planning workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt

    """
    args = _parse_args()
    configure_github_throttle_from_args(args)
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)
    log.info("Starting issue planner")
    agent = resolve_agent(args.agent)

    # Capture explicitness before auto-discovery overwrites ``args.issues``.
    issues_explicit = bool(args.issues)

    work_units = 0
    with work_report_context(lambda: work_units):
        if not args.issues:
            try:
                discovered = gh_list_open_issues()
            except GitHubRateLimitError as e:
                # Don't smear a 100-line traceback across the driver's loop output
                # when the only problem is that the GraphQL hourly budget is gone.
                # Exit cleanly so run_automation_loop.sh moves on to the next repo.
                log.error(
                    "GitHub API rate-limited; cannot discover issues this run "
                    "(reset at epoch %s). Skipping cleanly.",
                    e.reset_epoch,
                )
                if args.json:
                    emit_json_status(0, message="rate-limited; skipped", reset_epoch=e.reset_epoch)
                return 0
            log.info(
                "No --issues given; discovered %s open issues: %s", len(discovered), discovered
            )
            args.issues = discovered

        # Dedupe while preserving first-seen order. dict.fromkeys is the
        # canonical "ordered set" trick. Without this, ``--issues 123 123``
        # would race two workers on the same issue and produce double-posts.
        args.issues = list(dict.fromkeys(args.issues))

        log.info("Issues to plan: %s", args.issues)

        try:
            options = PlannerOptions(
                issues=args.issues,
                issues_explicit=issues_explicit,
                agent=agent,
                dry_run=args.dry_run,
                force=args.force,
                parallel=args.parallel,
                system_prompt_file=args.system_prompt,
                skip_closed=not args.no_skip_closed,
                enable_advise=not args.no_advise,
                agent_timeout=(
                    args.agent_timeout if args.agent_timeout is not None else DEFAULT_AGENT_TIMEOUT
                ),
                advise_timeout=(
                    args.advise_timeout
                    if args.advise_timeout is not None
                    else DEFAULT_AGENT_TIMEOUT
                ),
                git_message_timeout=(
                    args.git_message_timeout
                    if args.git_message_timeout is not None
                    else DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT
                ),
            )

            planner = Planner(options)
            results = planner.run()

            # Compute work units for loop convergence (#613): new plans
            successful = sum(1 for r in results.values() if r.success)
            already_planned = sum(1 for r in results.values() if r.plan_already_exists)
            work_units = max(0, successful - already_planned)

            failed = [num for num, result in results.items() if not result.success]
            if failed:
                log.error("Failed to plan %s issue(s): %s", len(failed), failed)
                if args.json:
                    emit_json_status(1, issues=args.issues, failed=failed)
                return 1

            log.info("Planning complete")
            if args.json:
                emit_json_status(0, issues=args.issues, failed=[])
            return 0
        except KeyboardInterrupt:
            logging.getLogger(__name__).warning("Interrupted by user")
            if args.json:
                emit_json_status(130, message="interrupted")
            return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
