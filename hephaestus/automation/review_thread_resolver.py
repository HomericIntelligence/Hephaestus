"""Review-thread handoff helpers for drive-green."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._review_utils import log_file_path
from .address_review import (
    _parse_addressed_block,
    run_address_fix_session,
)
from .git_utils import pr_ref
from .models import CIDriverOptions, WorkerResult

logger = logging.getLogger(__name__)


class ReviewThreadResolver:
    """Formats review-thread work and leaves GitHub resolution to a human."""

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
        """Push an attempted code fix, then hand open GitHub threads to a human."""
        armed_yield = WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        if self._options().dry_run:
            return armed_yield
        threads = self.list_unresolved_threads_safe(pr_number)
        if threads is None:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error="review_threads_unavailable",
                pr_number=pr_number,
            )
        if not threads:
            return armed_yield
        self._status().update_slot(
            acquired_slot,
            f"{pr_ref(pr_number)}: addressing review threads",
        )
        if not self.address_threads_once(issue_number, pr_number, threads):
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error="address_review_fix_failed",
                pr_number=pr_number,
            )
        logger.info(
            "Issue #%s: PR #%s agent code-fix claims are recorded; a human must verify, "
            "reply if needed, and resolve the still-open review threads",
            issue_number,
            pr_number,
        )
        return WorkerResult(
            issue_number=issue_number,
            success=False,
            error="human_review_thread_resolution_required",
            pr_number=pr_number,
        )

    def address_threads_once(
        self, issue_number: int, pr_number: int, threads: list[dict[str, Any]]
    ) -> bool:
        """Run one address-review session and push code without resolving threads."""
        worktree_path = self._get_worktree_path(issue_number, pr_number)
        pr_head_branch = self._get_pr_branch(pr_number)
        pre_agent_sha = self._sync_worktree_and_snapshot_sha(
            issue_number, worktree_path, pr_head_branch
        )
        if pre_agent_sha is None:
            return False
        log_file = log_file_path(self._state_dir(), "address-review-blocked", issue_number)
        try:
            fix_result = run_address_fix_session(
                issue_number=issue_number,
                pr_number=pr_number,
                worktree_path=worktree_path,
                threads=threads,
                agent=self._options().agent,
                repo_root=self._repo_root(),
                parse_fn=_parse_addressed_block,
                log_file=log_file,
                dry_run=self._options().dry_run,
                timeout=self._options().agent_timeout,
                advise_timeout=self._options().advise_timeout,
            )
        except RuntimeError as exc:
            logger.warning(
                "Issue #%s: address-review session failed for PR #%s: %s",
                issue_number,
                pr_number,
                exc,
            )
            return False
        pushed = self._push_ci_fix(
            worktree_path=worktree_path,
            pre_agent_sha=pre_agent_sha,
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            session_id=None,
        )
        if not pushed:
            return False
        logger.info(
            "Issue #%s: PR #%s recorded %s agent-addressed thread claim(s); "
            "GitHub threads remain for human resolution",
            issue_number,
            pr_number,
            len(fix_result.get("addressed", [])),
        )
        return True

    def list_unresolved_threads_safe(self, pr_number: int) -> list[dict[str, Any]] | None:
        """Fetch unresolved threads, returning ``None`` when the read is unavailable."""
        try:
            return self._list_threads(pr_number, self._options().dry_run)
        except Exception as exc:
            logger.info(
                "Issue PR #%s: failed to fetch unresolved review threads (%s); "
                "human intervention is required",
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
                "Address each thread below BEFORE pushing your CI fix. An unresolved "
                "thread means a reviewer (human or bot) flagged a real concern. Address "
                "the underlying issue in code, then ask a human reviewer to verify and "
                "resolve the thread."
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
