"""Retired compatibility surface for drive-green review-thread handling.

The queue pipeline owns the only supported two-role protocol: implementation
posts a reply after a pushed fix, and a fresh reviewer resolves or returns the
thread. This module deliberately refuses to run the former code-only handoff.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import CIDriverOptions, WorkerResult

logger = logging.getLogger(__name__)


class ReviewThreadResolver:
    """Retired code-only resolver; callers must use the queue pipeline."""

    def __init__(
        self,
        *,
        options_provider: Callable[[], CIDriverOptions],
        repo_root_provider: Callable[[], Path],
        state_dir_provider: Callable[[], Path],
        status_tracker_provider: Callable[[], Any],
        get_worktree_path: Callable[[int, int], Path],
        get_pr_branch: Callable[[int], str],
        sync_worktree_and_snapshot_sha: Callable[[int, Path, str], str | None],
        push_ci_fix: Callable[..., bool],
        list_threads: Callable[[int, bool], list[dict[str, Any]]],
    ) -> None:
        """Initialise review-thread dependencies."""
        self._options = options_provider
        self._repo_root = repo_root_provider
        self._state_dir = state_dir_provider
        self._status = status_tracker_provider
        self._get_worktree_path = get_worktree_path
        self._get_pr_branch = get_pr_branch
        self._sync_worktree_and_snapshot_sha = sync_worktree_and_snapshot_sha
        self._push_ci_fix = push_ci_fix
        self._list_threads = list_threads

    def resolve_blocked_pr(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Refuse the retired code-only handoff before it changes a PR."""
        del acquired_slot
        logger.warning(
            "Issue #%s: PR #%s review-thread resolver is retired; use the queue pipeline",
            issue_number,
            pr_number,
        )
        return WorkerResult(
            issue_number=issue_number,
            success=False,
            error="review_thread_resolver_retired_use_pipeline",
            pr_number=pr_number,
        )

    def address_threads_once(
        self, issue_number: int, pr_number: int, threads: list[dict[str, Any]]
    ) -> bool:
        """Refuse the retired implementation-only mutation path."""
        del issue_number, pr_number, threads
        return False

    def list_unresolved_threads_safe(self, pr_number: int) -> list[dict[str, Any]] | None:
        """Fetch unresolved threads, returning ``None`` when the read is unavailable."""
        try:
            return self._list_threads(pr_number, self._options().dry_run)
        except Exception as exc:
            logger.info(
                "Issue PR #%s: failed to fetch unresolved review threads (%s); "
                "the queue pipeline must retry this review read",
                pr_number,
                exc,
            )
            return None

    def format_review_threads_block(self, pr_number: int) -> str:
        """Render unresolved review threads as a Markdown prompt block."""
        threads = self.list_unresolved_threads_safe(pr_number)
        if not threads:
            return ""
        lines = [
            "## Unresolved PR Review Threads",
            "",
            (
                "The queue pipeline reads every thread, implements a real fix, posts an "
                "implementation reply after the push, and lets a fresh reviewer validate "
                "and resolve or return it."
            ),
            "",
        ]
        for i, thread in enumerate(threads, 1):
            loc = thread.get("path") or "<no path>"
            line_no = thread.get("line")
            loc_str = f"{loc}:{line_no}" if line_no is not None else loc
            body = (thread.get("body") or "").strip() or "<empty body>"
            lines.extend([f"### Thread {i} - {loc_str}", "", body, ""])
        lines.extend(["---", ""])
        return "\n".join(lines)
