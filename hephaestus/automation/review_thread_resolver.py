"""Review-thread resolution helpers for drive-green."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._review_utils import log_file_path
from .address_review import (
    _parse_addressed_block,
    resolve_addressed_threads,
    run_address_fix_session,
)
from .git_utils import pr_ref
from .models import CIDriverOptions, WorkerResult

logger = logging.getLogger(__name__)

_BLOCKED_ADDRESS_MAX_ATTEMPTS = 2


class ReviewThreadResolver:
    """Formats, addresses, and resolves PR review threads for drive-green."""

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
        recheck_and_arm_after_fix: Callable[[int, int, int], WorkerResult | None],
        list_threads: Callable[[int, bool], list[dict[str, Any]]],
        resolve_thread: Callable[[str, bool], None],
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
        self._recheck_and_arm_after_fix = recheck_and_arm_after_fix
        self._list_threads = list_threads
        self._resolve_thread = resolve_thread

    def resolve_blocked_pr(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Address unresolved threads on a green-but-BLOCKED PR."""
        armed_yield = WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        if self._options().dry_run:
            return armed_yield
        threads = self.list_unresolved_threads_safe(pr_number)
        if not threads:
            return armed_yield
        addressed_any = False
        for attempt in range(1, _BLOCKED_ADDRESS_MAX_ATTEMPTS + 1):
            prior_ids = {t["id"] for t in threads if t.get("id")}
            self._status().update_slot(
                acquired_slot,
                f"{pr_ref(pr_number)}: addressing review threads [A{attempt}]",
            )
            progressed = self.address_threads_once(issue_number, pr_number, threads)
            addressed_any = addressed_any or progressed
            threads = self.list_unresolved_threads_safe(pr_number)
            remaining_ids = {t["id"] for t in threads if t.get("id")}
            if not remaining_ids:
                break
            if not (prior_ids - remaining_ids):
                logger.info(
                    "Issue #%s: PR #%s address attempt %s resolved no new threads "
                    "(%s still unresolved); leaving armed for review",
                    issue_number,
                    pr_number,
                    attempt,
                    len(remaining_ids),
                )
                return armed_yield
        if not addressed_any:
            return armed_yield
        rearmed = self._recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot)
        return rearmed if rearmed is not None else armed_yield

    def address_threads_once(
        self, issue_number: int, pr_number: int, threads: list[dict[str, Any]]
    ) -> bool:
        """Run one address-review session, push the commit, then resolve addressed IDs."""
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
        presented_ids = {t["id"] for t in threads if t.get("id")}
        resolve_addressed_threads(
            fix_result.get("addressed", []),
            fix_result.get("replies", {}),
            presented_ids,
            dry_run=self._options().dry_run,
        )
        return True

    def list_unresolved_threads_safe(self, pr_number: int) -> list[dict[str, Any]]:
        """Fetch unresolved review threads, degrading lookup failures to an empty list."""
        try:
            return self._list_threads(pr_number, self._options().dry_run)
        except Exception as exc:
            logger.info(
                "Issue PR #%s: failed to fetch unresolved review threads (%s); "
                "skipping review-thread handling",
                pr_number,
                exc,
            )
            return []

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
                "thread means a reviewer (human or bot) flagged a real concern. Resolve "
                "the underlying issue in code; the thread is closed automatically when "
                "the line it points at changes."
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

    def reply_and_resolve_bot_threads(self, pr_number: int) -> int:
        """Resolve bot-authored unresolved threads after a successful CI fix."""
        if self._options().dry_run:
            return 0
        resolved = 0
        for thread in self.list_unresolved_threads_safe(pr_number):
            if not self._is_bot_author(thread.get("author") or ""):
                continue
            thread_id = thread.get("id")
            if not thread_id:
                continue
            try:
                self._resolve_thread(thread_id, False)
                resolved += 1
            except Exception as exc:
                logger.info(
                    "PR #%s: could not resolve bot thread %s (%s); skipping",
                    pr_number,
                    thread_id,
                    exc,
                )
        if resolved:
            logger.info("PR #%s: resolved %s automated review thread(s)", pr_number, resolved)
        return resolved

    @staticmethod
    def _is_bot_author(login: str) -> bool:
        """Return True for automated review authors."""
        return login.endswith("[bot]")
