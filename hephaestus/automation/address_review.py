"""Retired standalone address-review compatibility surface.

This module remains importable for compatibility, but it is retired and all
entry points fail closed. Use ``hephaestus-automation-loop``: it is the sole
workflow that can post implementation replies and have a fresh reviewer
validate or resolve the live GitHub threads.
"""

from __future__ import annotations

import argparse
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    resolve_agent,
)
from hephaestus.cli.utils import (
    add_advise_timeout_arg,
    add_agent_timeout_arg,
    emit_json_status,
)

from ._review_utils import (
    _discover_prs_simple,
    build_review_parser,
    drain_completed_futures,
    find_pr_for_issue,
    instance_log,
    load_impl_session_id,
    print_worker_summary,
    setup_review_logging,
)
from ._reviewer_base import BaseReviewer

# The pure/parse/session address cores live in ``address_review_core``
# (unit-covered, off the coverage omit-list). They are re-exported here so
# historical imports keep resolving after the #1823 split.
# ``run_address_fix_session`` is deliberately fail-closed.
from .address_review_core import (
    _ADDRESS_PARSE_DEFAULT as _ADDRESS_PARSE_DEFAULT,
    _log_address_parse_error as _log_address_parse_error,
    _parse_addressed_block as _parse_addressed_block,
    run_address_fix_session as run_address_fix_session,
)
from .agent_config import DEFAULT_AGENT_TIMEOUT
from .git_utils import (
    issue_auto_impl_branch_name,
    issue_ref,
    pr_ref,
)
from .github_api import (
    gh_pr_list_unresolved_threads,
)
from .models import AddressReviewOptions, ReviewPhase, ReviewState, WorkerResult

logger = logging.getLogger(__name__)

_RETIRED_ERROR = "address_review_retired_use_pipeline"


class AddressReviewer(BaseReviewer):
    """Retired compatibility placeholder; use ``hephaestus-automation-loop``."""

    options: AddressReviewOptions

    def __init__(self, options: AddressReviewOptions, **kwargs: Any) -> None:
        """Initialize address reviewer.

        Args:
            options: Reviewer configuration options
            **kwargs: Forwarded to :class:`BaseReviewer` for dependency
                injection (``get_repo_root``, ``worktree_manager_factory``,
                ``status_tracker_factory``, ``log_manager_factory``).

        """
        super().__init__(options, **kwargs)

    def _log(self, level: str, msg: str, thread_id: int | None = None) -> None:
        """Log to both standard logger and UI thread buffer.

        Overrides :meth:`BaseReviewer._log` so the stdlib log record
        attributes to this module rather than ``_reviewer_base``.
        """
        instance_log(self.log_manager, level, msg, thread_id, caller_logger=logger)

    def run(self) -> dict[int, WorkerResult]:
        """Refuse the retired standalone address workflow before any mutation."""
        logger.error(
            "AddressReviewer is retired; use hephaestus-automation-loop so implementation "
            "replies and reviewer reconciliation stay in one lifecycle"
        )
        return {
            issue_number: WorkerResult(
                issue_number=issue_number,
                success=False,
                error=_RETIRED_ERROR,
            )
            for issue_number in self.options.issues
        }

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:
        """Pre-discover open PRs for all issues.

        Args:
            issue_numbers: Issue numbers to check

        Returns:
            Mapping of issue_number -> pr_number for issues that have an open PR

        """
        return _discover_prs_simple(
            issue_numbers,
            lambda issue_num: find_pr_for_issue(
                issue_num,
                extra_strategies=True,
                _load_review_state_fn=lambda: self._load_review_state(issue_num),
            ),
            on_missing=lambda issue_num: logger.info(
                "Issue #%s: no open PR found, skipping", issue_num
            ),
        )

    def _address_all(self, pr_map: dict[int, int]) -> dict[int, WorkerResult]:
        """Address all issues in parallel.

        Args:
            pr_map: Mapping of issue_number -> pr_number (pre-filtered to issues with PRs)

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}

            for issue_num, pr_num in pr_map.items():
                # Slot acquisition happens inside _address_issue via
                # self.status_tracker.acquire_slot() so workers that exceed
                # max_workers block rather than racing on a pre-assigned index
                # (#A3-005).
                future = executor.submit(self._address_issue, issue_num, pr_num)
                futures[future] = issue_num

            for future in drain_completed_futures(futures):
                issue_num = futures.pop(future)
                try:
                    result = future.result()
                    results[issue_num] = result
                    if result.success:
                        logger.info("Issue #%s address review completed", issue_num)
                    else:
                        logger.error("Issue #%s address review failed: %s", issue_num, result.error)
                except Exception as e:
                    logger.error("Issue #%s raised exception: %s", issue_num, e)
                    results[issue_num] = WorkerResult(
                        issue_number=issue_num,
                        success=False,
                        error=str(e),
                    )

        self._print_summary(results)
        return results

    def _check_threads_for_address(
        self,
        issue_number: int,
        pr_number: int,
        thread_id: int,
    ) -> list[dict[str, Any]] | None:
        """List unresolved threads; return None if none or dry-run (caller returns success).

        Args:
            issue_number: GitHub issue number.
            pr_number: PR number to check.
            thread_id: Current thread id for logging.

        Returns:
            None if no threads or dry-run; list of unresolved thread dicts otherwise.

        """
        threads = gh_pr_list_unresolved_threads(pr_number, dry_run=self.options.dry_run)
        if not threads:
            self._log(
                "info",
                f"No unresolved threads on PR {pr_ref(pr_number)} for issue {issue_ref(issue_number)}",  # noqa: E501
                thread_id,
            )
            return None

        self._log(
            "info",
            f"Found {len(threads)} unresolved thread(s) on PR {pr_ref(pr_number)}",
            thread_id,
        )

        if self.options.dry_run:
            self._log(
                "info",
                f"[DRY RUN] Would address {len(threads)} unresolved thread(s) "
                f"and push for PR {pr_ref(pr_number)}",
                thread_id,
            )
            return None

        return threads

    def _setup_address_state(
        self,
        issue_number: int,
        pr_number: int,
        slot_id: int,
    ) -> tuple[str | None, ReviewState, str, Path]:
        """Load session_id, load/create ReviewState, resolve branch, create worktree.

        Args:
            issue_number: GitHub issue number.
            pr_number: PR number.
            slot_id: Worker slot for status tracking.

        Returns:
            Tuple of (session_id, review_state, branch_name, worktree_path).

        """
        session_id = self._load_impl_session_id(issue_number)
        review_state = self._load_review_state(issue_number)
        if review_state is None:
            branch_name = issue_auto_impl_branch_name(issue_number)
            review_state = ReviewState(
                issue_number=issue_number,
                pr_number=pr_number,
                branch_name=branch_name,
            )
        else:
            review_state.pr_number = pr_number
        branch_name = review_state.branch_name or issue_auto_impl_branch_name(issue_number)

        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Setting up worktree")
        worktree_path = self._get_or_create_worktree(issue_number, branch_name, review_state)

        with self.state_lock:
            review_state.worktree_path = str(worktree_path)
            review_state.branch_name = branch_name
            review_state.phase = ReviewPhase.FIXING
        self._save_review_state(review_state)
        return session_id, review_state, branch_name, worktree_path

    def _commit_push_and_record(
        self,
        *,
        issue_number: int,
        pr_number: int,
        branch_name: str,
        worktree_path: Path,
        addressed: list[str],
        replies: dict[str, str],
        threads: list[dict[str, Any]],
        review_state: ReviewState,
        slot_id: int,
        thread_id: int,
    ) -> None:
        """Reject the retired standalone commit/push lifecycle.

        Args:
            issue_number: GitHub issue number.
            pr_number: PR number.
            branch_name: Git branch name.
            worktree_path: Path to worktree.
            addressed: Thread IDs the agent addressed.
            replies: Mapping returned by the agent's parse contract.
            threads: Threads presented to the fix session; retained for call compatibility.
            review_state: Review state to update.
            slot_id: Worker slot for status tracking.
            thread_id: Current thread id for logging.

        """
        del (
            issue_number,
            pr_number,
            branch_name,
            worktree_path,
            addressed,
            replies,
            threads,
            review_state,
            slot_id,
            thread_id,
        )
        raise RuntimeError(_RETIRED_ERROR)

    def _address_issue(self, issue_number: int, pr_number: int) -> WorkerResult:
        """Refuse direct review-thread remediation outside the queue pipeline."""
        del pr_number
        return WorkerResult(
            issue_number=issue_number,
            success=False,
            error=_RETIRED_ERROR,
        )

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Load the implementer's agent session ID from state file.

        Thin wrapper around :func:`._review_utils.load_impl_session_id`, kept
        as a method so existing patch-by-method test seams hold.

        Args:
            issue_number: GitHub issue number

        Returns:
            Session ID string if found, None otherwise

        """
        return load_impl_session_id(self.state_dir, issue_number, self.options.agent)

    def _load_review_state(self, issue_number: int) -> ReviewState | None:
        """Load review state from disk.

        Thin wrapper around :meth:`BaseReviewer._load_review_state_from_disk`
        kept for backward compatibility with internal callers and tests.

        Args:
            issue_number: GitHub issue number

        Returns:
            ReviewState if state file exists and is valid, None otherwise

        """
        return self._load_review_state_from_disk(issue_number)

    def _save_review_state(self, state: ReviewState) -> None:
        """Save review state to disk.

        Thin wrapper around :meth:`BaseReviewer._save_state` kept for
        backward compatibility with internal callers.

        Args:
            state: ReviewState to persist

        """
        self._save_state(state)

    def _get_or_create_worktree(
        self,
        issue_number: int,
        branch_name: str,
        review_state: ReviewState,
    ) -> Path:
        """Get existing worktree or create a new one for the PR branch.

        Reuses the worktree path from review state if it still exists on disk.
        Otherwise creates a new worktree via WorktreeManager.

        Args:
            issue_number: GitHub issue number
            branch_name: PR branch name
            review_state: Current review state (may contain existing worktree path)

        Returns:
            Path to worktree directory

        """
        # Try to reuse existing worktree from review state
        if review_state.worktree_path:
            existing_path = Path(review_state.worktree_path)
            if existing_path.exists() and (existing_path / ".git").exists():
                logger.info(
                    "Reusing existing worktree at %s for issue #%s", existing_path, issue_number
                )
                # Register with worktree manager so cleanup works
                with self.worktree_manager.lock:
                    self.worktree_manager.worktrees[issue_number] = existing_path
                return existing_path

        # Create new worktree
        logger.info("Creating new worktree for issue #%s on branch %s", issue_number, branch_name)
        return self.worktree_manager.create_worktree(issue_number, branch_name)

    def _run_fix_session(
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        threads: list[dict[str, Any]],
        session_id: str | None,
    ) -> dict[str, Any]:
        """Reject the retired standalone agent-fix lifecycle.

        Delegates to the module-level :func:`run_address_fix_session` so the
        in-loop implementer step (#28) and this standalone phase share one
        invocation core (DRY). The Claude path resumes the implementer's
        deterministic session; ``session_id`` only feeds the Codex
        resume-then-fallback path below.

        Args:
            issue_number: GitHub issue number
            pr_number: GitHub PR number
            worktree_path: Path to git worktree containing PR branch
            threads: List of unresolved thread dicts (id, path, line, body)
            session_id: Previous Codex session ID to resume, or None for fresh session

        Returns:
            Parsed dict with "addressed" and "replies" keys

        """
        del issue_number, pr_number, worktree_path, threads, session_id
        raise RuntimeError(_RETIRED_ERROR)

    def _commit_if_changes(self, issue_number: int, worktree_path: Path) -> None:
        """Reject standalone commits outside the pipeline lifecycle.

        Args:
            issue_number: GitHub issue number (used in commit message)
            worktree_path: Path to git worktree

        """
        del issue_number, worktree_path
        raise RuntimeError(_RETIRED_ERROR)

    def _push_branch(self, branch_name: str, worktree_path: Path) -> None:
        """Reject standalone pushes outside the pipeline lifecycle.

        Args:
            branch_name: Branch name to push
            worktree_path: Path to git worktree

        Raises:
            RuntimeError: If push fails

        """
        del branch_name, worktree_path
        raise RuntimeError(_RETIRED_ERROR)

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print address review summary.

        Args:
            results: Mapping of issue number to WorkerResult

        """
        print_worker_summary(
            "Address Review Summary",
            results,
            failed_header="\nFailed issues:",
        )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for address review CLI."""
    parser = build_review_parser(
        description=(
            "Retired compatibility command. Use hephaestus-automation-loop to address, "
            "reply to, validate, and resolve PR review threads."
        ),
        epilog="Run hephaestus-automation-loop instead.",
        issues_help="Retired; use hephaestus-automation-loop instead",
        dry_run_prefix="Retired; no review-thread work is performed.",
    )
    add_agent_timeout_arg(parser)
    add_advise_timeout_arg(parser)
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the address review CLI."""
    return _build_parser().parse_args(argv)


def main() -> int:
    """Execute the address review workflow.

    Returns:
        Exit code: 0 on success, 1 if any issue failed, 130 on keyboard interrupt

    """
    args = _parse_args()
    from hephaestus.cli.utils import configure_github_throttle_from_args

    configure_github_throttle_from_args(args)
    setup_review_logging(args.verbose)
    agent = resolve_agent(args.agent)

    log = logging.getLogger(__name__)
    log.info("Starting address review for issues: %s", args.issues)

    from hephaestus.utils.terminal import terminal_guard

    options = AddressReviewOptions(
        issues=args.issues,
        agent=agent,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        enable_ui=not args.no_ui and not args.json,
        verbose=args.verbose,
        agent_timeout=(
            args.agent_timeout if args.agent_timeout is not None else DEFAULT_AGENT_TIMEOUT
        ),
        advise_timeout=(
            args.advise_timeout if args.advise_timeout is not None else DEFAULT_AGENT_TIMEOUT
        ),
    )

    with terminal_guard():
        try:
            reviewer = AddressReviewer(options)
            results = reviewer.run()

            failed = [num for num, result in results.items() if not result.success]
            if failed:
                log.error("Failed to address review for %s issue(s): %s", len(failed), failed)
                if args.json:
                    emit_json_status(1, issues=args.issues, failed=failed)
                return 1

            log.info("Address review complete")
            if args.json:
                emit_json_status(0, issues=args.issues, failed=[])
            return 0
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            if args.json:
                emit_json_status(130, message="interrupted")
            return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
