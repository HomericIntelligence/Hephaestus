"""CI check inspection collaborator: PR state polling, review threads, CI log fetching.

Extracted from :class:`~hephaestus.automation.ci_driver.CIDriver` as a narrow
SRP collaborator (#1289). Owns all logic that reads CI-check state, review
threads, and PR merge-state — nothing that writes to GitHub.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from hephaestus.automation.github_api import (
    _gh_call,
    gh_pr_checks,
    gh_pr_list_unresolved_threads,
    gh_pr_resolve_thread,
)
from hephaestus.automation.models import CIDriverOptions, WorkerResult

logger = logging.getLogger(__name__)


class CICheckInspector:
    """Inspects CI check state, fetches CI logs, and manages review threads.

    Args:
        options: CI driver configuration options.
        get_pr_branch: Provider that returns the head-branch name for a PR number.
        get_worktree_path: Provider that returns the checked-out worktree path for
            an (issue_number, pr_number) pair.
        status_tracker_update_slot: Callable(slot, message) for status-bar updates.

    """

    def __init__(
        self,
        *,
        options: CIDriverOptions,
        get_pr_branch: Any,  # Callable[[int], str]
        get_worktree_path: Any,  # Callable[[int, int], Path]
        status_tracker_update_slot: Any,  # Callable[[int, str], None]
    ) -> None:
        """Initialize the inspector; wire cross-collaborator slots after construction."""
        self.options = options
        self._get_pr_branch = get_pr_branch
        self._get_worktree_path = get_worktree_path
        self._status_tracker_update_slot = status_tracker_update_slot

        # Wired by CIDriver after construction — delegates to CIDriver methods
        # that remain on the god class (arming store, post-merge /learn, etc.).
        self._load_arming_state_fn: Any = None
        self._clear_arming_state_fn: Any = None
        self._learn_record_terminal_fn: Any = None
        self._run_drive_green_learnings_fn: Any = None
        self._run_drive_green_compact_fn: Any = None
        self._mark_drive_green_learn_result_fn: Any = None
        self._save_arming_state_fn: Any = None
        self._state_dir: Path | None = None

    # ------------------------------------------------------------------
    # CI log fetching
    # ------------------------------------------------------------------

    def _get_failing_ci_logs(self, pr_number: int) -> str:
        """Fetch combined failure logs for recent failed CI runs on a PR.

        Scopes the ``gh run list`` query to the PR's head branch so we only
        see runs that belong to this PR rather than the most-recent repo-wide
        runs (the previous repo-wide query could return runs for other PRs
        and even other branches, making the logs useless for fixing *this* PR).

        Args:
            pr_number: GitHub PR number.

        Returns:
            Combined log string, truncated to 10 000 characters.

        """
        try:
            branch = self._get_pr_branch(pr_number)
            result2 = _gh_call(
                [
                    "run",
                    "list",
                    "--branch",
                    branch,
                    "--status",
                    "failure",
                    "--limit",
                    "10",
                    "--json",
                    "databaseId,conclusion,name,headSha",
                ],
                check=False,
            )
            runs: list[dict[str, Any]] = json.loads(result2.stdout or "[]")
            failed_runs = [r for r in runs if r.get("conclusion") == "failure"][:3]

            logs: list[str] = []
            for run_info in failed_runs:
                run_id = run_info.get("databaseId")
                run_name = run_info.get("name", str(run_id))
                if not run_id:
                    continue
                try:
                    log_result = _gh_call(
                        ["run", "view", str(run_id), "--log-failed"],
                        check=False,
                    )
                    logs.append(f"=== {run_name} ===\n{log_result.stdout[:3000]}")
                except Exception as log_err:
                    logger.debug("Could not fetch log for run %s: %s", run_id, log_err)

            return "\n\n".join(logs)[:10000]

        except Exception as e:
            logger.warning("Could not fetch CI logs for PR #%s: %s", pr_number, e)
            return ""

    # ------------------------------------------------------------------
    # PR state polling
    # ------------------------------------------------------------------

    def _gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Return ``{state, headRefOid, mergedAt, mergeStateStatus}`` or ``None``.

        Used by the on-drive-start check (#840) to detect post-merge so the
        ``/learn`` capture can fire exactly once per issue, and by
        ``_wait_for_pr_terminal`` to detect a ``DIRTY`` (merge-conflict) PR that
        would otherwise sit armed-but-unmergeable until the timeout.
        """
        try:
            result = _gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "state,headRefOid,mergedAt,mergeStateStatus",
                ],
                check=False,
            )
            return dict(json.loads(result.stdout or "{}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not fetch PR #%s state for arming check: %s",
                pr_number,
                exc,
            )
            return None

    def _wait_for_pr_terminal(self, issue_number: int, pr_number: int) -> str:
        """Block until an armed PR reaches a terminal state, or times out.

        Once a PR's auto-merge is armed, the driver historically observed the
        OPEN state once and returned success — declaring the repo "not done"
        on PRs that were seconds away from merging (#838 false-failure class).
        Instead, poll the PR until it actually finishes so the driver can react
        to the real outcome (merge, abandonment, or freshly-red CI).

        Polls ``_gh_pr_state`` with exponential backoff (``min(2**n, 60)`` cap,
        matching the CI-pending poll loop in ``_drive_issue``), bounded by
        ``HEPH_PR_MERGE_MAX_WAIT`` seconds (default 1800). On each OPEN poll we
        also inspect required checks: a required check concluding ``failure``
        returns ``FAILING`` straight away rather than waiting out the timeout.

        Args:
            issue_number: GitHub issue number (for status/log lines).
            pr_number: PR whose merge we are waiting on.

        Returns:
            One of ``"MERGED"``, ``"CLOSED"``, ``"FAILING"``, ``"DIRTY"`` (the
            PR has a merge conflict and will never merge while armed — caller
            should rebase/resolve), ``"BLOCKED"`` (branch-protection gate such
            as unresolved conversations or a required human review blocks the
            merge — nothing the bot can fix; caller should leave armed and
            yield), or ``"TIMEOUT"``.

        """
        from hephaestus.automation.git_utils import issue_ref

        if self.options.dry_run:
            return "TIMEOUT"

        max_wait = int(os.environ.get("HEPH_PR_MERGE_MAX_WAIT", "1800"))
        elapsed = 0
        attempt = 0
        iref = issue_ref(issue_number)

        while True:
            gh_state = self._gh_pr_state(pr_number)
            state = ((gh_state or {}).get("state") or "").upper()

            if state == "MERGED":
                logger.info("Issue #%s: PR #%s merged", issue_number, pr_number)
                return "MERGED"
            if state == "CLOSED":
                logger.info(
                    "Issue #%s: PR #%s closed without merging while waiting",
                    issue_number,
                    pr_number,
                )
                return "CLOSED"

            failing = self._failing_required_check_names(pr_number)
            if failing:
                logger.warning(
                    "Issue #%s: PR #%s went red while awaiting merge (failing: %s)",
                    issue_number,
                    pr_number,
                    ", ".join(failing),
                )
                return "FAILING"

            merge_status = ((gh_state or {}).get("mergeStateStatus") or "").upper()
            if merge_status in ("DIRTY", "CONFLICTING"):
                logger.warning(
                    "Issue #%s: PR #%s is %s (merge conflict) while armed; needs rebase/resolution",
                    issue_number,
                    pr_number,
                    merge_status,
                )
                return "DIRTY"

            if merge_status == "BLOCKED" and not failing:
                pending = self._pending_required_check_names(pr_number)
                if not pending:
                    logger.warning(
                        "Issue #%s: PR #%s is BLOCKED by branch protection "
                        "(unresolved conversations or required review) — "
                        "cannot auto-merge; leaving armed and exiting poll early",
                        issue_number,
                        pr_number,
                    )
                    return "BLOCKED"

            sleep_secs = min(2**attempt, 60)
            if elapsed + sleep_secs > max_wait:
                logger.warning(
                    "Issue #%s: PR #%s still OPEN after %ss (limit %ss); leaving armed and pending",
                    issue_number,
                    pr_number,
                    elapsed,
                    max_wait,
                )
                return "TIMEOUT"

            self._status_tracker_update_slot(
                0,
                f"{iref}: PR #{pr_number} awaiting merge ({elapsed}s elapsed)",
            )
            time.sleep(sleep_secs)
            elapsed += sleep_secs
            attempt += 1

    # ------------------------------------------------------------------
    # Arming-record sweep
    # ------------------------------------------------------------------

    def _sweep_orphaned_arming_records(self) -> None:
        """Drop CLOSED records and capture missed ``/learn`` for MERGED orphans (#848).

        The per-issue ``_check_arming_on_drive_start`` only fires when the
        issue is in the current run's input list. If a PR was replaced via
        the fresh-branch-reopen workflow (the replacement PR merged out of
        band of the bot, the original was closed), the arming record points
        at the closed PR and the issue itself is closed — the next run's
        ``gh issue list --state open`` won't include it, so the record
        leaks forever and ``/learn`` is silently lost.

        Sweep every ``drive-green-armed-*.json`` at startup: drop records
        whose PR is CLOSED-not-merged, fire ``/learn`` once for records
        whose PR is MERGED (then mark explicit succeeded/failed learn status),
        leave OPEN records alone for the normal per-issue path to handle.
        """
        state_dir = self._state_dir
        if state_dir is None:
            logger.info("Arming sweep skipped: state_dir not wired")
            return
        try:
            records = sorted(state_dir.glob("drive-green-armed-*.json"))
        except OSError as exc:
            logger.info("Arming sweep skipped: state_dir scan failed (%s)", exc)
            return
        if not records:
            return
        logger.info("Sweeping %s arming record(s) for orphan resolution", len(records))
        for path in records:
            stem = path.stem  # drive-green-armed-<issue>
            try:
                issue_number = int(stem.rsplit("-", 1)[-1])
            except ValueError:
                logger.info("Arming sweep: ignoring malformed filename %s", path.name)
                continue
            record = self._load_arming_state_fn(issue_number)
            if record is None:
                continue
            if self._learn_record_terminal_fn(record):
                continue
            pr_number = record.get("pr_number")
            if not isinstance(pr_number, int):
                logger.info(
                    "Arming sweep: dropping record %s with non-integer pr_number",
                    path.name,
                )
                self._clear_arming_state_fn(issue_number)
                continue
            gh_state = self._gh_pr_state(pr_number)
            if gh_state is None:
                continue
            state = (gh_state.get("state") or "").upper()
            if state == "MERGED":
                logger.info(
                    "Arming sweep: issue #%s / PR #%s MERGED; firing /learn",
                    issue_number,
                    pr_number,
                )
                learn_succeeded = self._run_drive_green_learnings_fn(issue_number, pr_number)
                self._run_drive_green_compact_fn(issue_number, pr_number)
                self._mark_drive_green_learn_result_fn(
                    issue_number,
                    record,
                    succeeded=learn_succeeded,
                )
            elif state == "CLOSED":
                logger.info(
                    "Arming sweep: issue #%s / PR #%s CLOSED-not-merged; dropping record",
                    issue_number,
                    pr_number,
                )
                self._clear_arming_state_fn(issue_number)

    # ------------------------------------------------------------------
    # Per-issue arming check
    # ------------------------------------------------------------------

    def _check_arming_on_drive_start(
        self, issue_number: int, pr_number: int
    ) -> WorkerResult | None:
        """Fire ``/learn`` if a prior run armed and the PR has since merged.

        Called at the top of ``_drive_issue``. Returns:

        - ``WorkerResult(success=True, ...)`` if the arming record handled
          the issue (merge detected + ``/learn`` attempted, OR still in flight,
          OR a terminal learn result already exists). Caller returns this
          directly without doing any further drive work.
        - ``None`` if the issue should fall through to the normal drive
          path (no arming record, arming stale, or PR abandoned).
        """
        from hephaestus.automation.git_utils import issue_ref

        record = self._load_arming_state_fn(issue_number)
        if record is None:
            return None
        if self._learn_record_terminal_fn(record):
            logger.info(
                "Issue #%s: /learn already terminal (%s at %s); skipping further drive",
                issue_number,
                record.get("learn_status") or "succeeded",
                record.get("learn_captured_at")
                or record.get("learn_succeeded_at")
                or record.get("learn_attempted_at"),
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        gh_state = self._gh_pr_state(pr_number)
        if gh_state is None:
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        state = (gh_state.get("state") or "").upper()
        current_sha = gh_state.get("headRefOid") or ""

        if state == "MERGED":
            logger.info(
                "Issue #%s: PR #%s detected as MERGED; capturing /learn",
                issue_number,
                pr_number,
            )
            self._status_tracker_update_slot(
                0, f"{issue_ref(issue_number)}: capturing post-merge /learn"
            )
            learn_succeeded = self._run_drive_green_learnings_fn(issue_number, pr_number)
            self._run_drive_green_compact_fn(issue_number, pr_number)
            self._mark_drive_green_learn_result_fn(
                issue_number,
                record,
                succeeded=learn_succeeded,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        if state == "CLOSED":
            logger.info(
                "Issue #%s: PR #%s was CLOSED without merging; dropping arming record",
                issue_number,
                pr_number,
            )
            self._clear_arming_state_fn(issue_number)
            return None

        # OPEN
        armed_sha = record.get("head_sha_at_arming") or ""
        if current_sha and armed_sha and current_sha != armed_sha:
            logger.info(
                "Issue #%s: PR #%s head advanced from %s to %s since arming; re-entering drive",
                issue_number,
                pr_number,
                armed_sha[:8],
                current_sha[:8],
            )
            self._clear_arming_state_fn(issue_number)
            return None

        logger.info(
            "Issue #%s: PR #%s still OPEN at the armed SHA; waiting for merge",
            issue_number,
            pr_number,
        )
        outcome = self._wait_for_pr_terminal(issue_number, pr_number)
        if outcome == "MERGED":
            self._status_tracker_update_slot(
                0, f"{issue_ref(issue_number)}: capturing post-merge /learn"
            )
            learn_succeeded = self._run_drive_green_learnings_fn(issue_number, pr_number)
            self._run_drive_green_compact_fn(issue_number, pr_number)
            self._mark_drive_green_learn_result_fn(
                issue_number,
                record,
                succeeded=learn_succeeded,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        if outcome == "CLOSED":
            self._clear_arming_state_fn(issue_number)
            return None
        if outcome in ("FAILING", "DIRTY"):
            self._clear_arming_state_fn(issue_number)
            return None
        return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

    # ------------------------------------------------------------------
    # Review thread helpers
    # ------------------------------------------------------------------

    def _list_unresolved_threads_safe(self, pr_number: int) -> list[dict[str, Any]]:
        """Fetch unresolved review threads, swallowing lookup failures.

        Shared by the prompt-context formatter and the post-fix bot-thread
        reply/resolve step so both run off a single fetch contract. Network/JSON
        errors are downgraded to an info log and yield an empty list — neither
        caller is ever gated on review-thread availability (#846).
        """
        try:
            return gh_pr_list_unresolved_threads(pr_number, dry_run=self.options.dry_run)
        except Exception as exc:
            logger.info(
                "Issue PR #%s: failed to fetch unresolved review threads (%s); "
                "skipping review-thread handling",
                pr_number,
                exc,
            )
            return []

    def _format_review_threads_block(self, pr_number: int) -> str:
        """Render unresolved PR review threads as a Markdown block for the prompt.

        Returns the empty string when there are no unresolved threads or when
        the lookup fails — the CI fix loop is never gated on review-thread
        availability (#846).
        """
        threads = self._list_unresolved_threads_safe(pr_number)
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
        for i, t in enumerate(threads, 1):
            loc = t.get("path") or "<no path>"
            line_no = t.get("line")
            loc_str = f"{loc}:{line_no}" if line_no is not None else loc
            body = (t.get("body") or "").strip() or "<empty body>"
            lines.append(f"### Thread {i} — {loc_str}")
            lines.append("")
            lines.append(body)
            lines.append("")
        lines.append("---")
        lines.append("")
        return "\n".join(lines)

    def _reply_and_resolve_bot_threads(self, pr_number: int) -> int:
        """Resolve automated review threads after a successful CI fix.

        ci_driver surfaces unresolved threads to the fix prompt but cannot rely
        on GitHub auto-resolving a bot thread when its line moves. After a fix
        lands, resolve each BOT-authored unresolved thread without adding
        another reply comment, so automated review comments are closed rather
        than left dangling. Human threads are left untouched. Best-effort: a
        failure on one thread is logged and skipped (never blocks the fix).

        Returns the number of threads resolved.
        """
        if self.options.dry_run:
            return 0
        threads = self._list_unresolved_threads_safe(pr_number)
        resolved = 0
        for t in threads:
            if not self._is_bot_author(t.get("author") or ""):
                continue
            thread_id = t.get("id")
            if not thread_id:
                continue
            try:
                gh_pr_resolve_thread(thread_id, dry_run=False)
                resolved += 1
            except Exception as exc:
                logger.info(
                    "PR #%s: could not resolve bot thread %s (%s); skipping",
                    pr_number,
                    thread_id,
                    exc,
                )
        if resolved:
            logger.info(
                "PR #%s: resolved %s automated review thread(s)",
                pr_number,
                resolved,
            )
        return resolved

    @staticmethod
    def _is_bot_author(login: str) -> bool:
        """Return True for automated review authors (GitHub App / bot accounts).

        GitHub App authors carry a ``[bot]`` suffix on their login
        (``github-actions[bot]``, ``coderabbitai[bot]``, ``dependabot[bot]``).
        Human review threads are never auto-resolved — only the bot's own reply
        thread is closed after the CI fix addresses it.
        """
        return login.endswith("[bot]")

    # ------------------------------------------------------------------
    # Required CI check name queries
    # ------------------------------------------------------------------

    def _failing_required_check_names(self, pr_number: int) -> list[str]:
        """Return names of required checks that are currently failing.

        Used by the no-commit retry path (#846) to name the actual offenders
        verbatim in the force-engagement prompt. Returns an empty list if
        the lookup fails — the caller treats that as "cannot prove still
        red" and skips the retry rather than launching Claude blind.
        """
        try:
            checks = gh_pr_checks(pr_number, dry_run=self.options.dry_run)
        except Exception as exc:
            logger.info(
                "PR #%s: failed to re-check CI for no-commit retry decision (%s)",
                pr_number,
                exc,
            )
            return []
        if not checks:
            return []
        required = [c for c in checks if c.get("required")] or checks
        return [
            c.get("name", "")
            for c in required
            if c.get("status") == "completed" and c.get("conclusion") == "failure"
        ]

    def _pending_required_check_names(self, pr_number: int) -> list[str]:
        """Return names of required checks that are still in flight (not completed).

        Used by the BLOCKED early-exit guard in ``_wait_for_pr_terminal`` to
        distinguish branch-protection blocks (all checks green but conversations
        unresolved) from pending-CI blocks (checks still running).  Returns an
        empty list on lookup failure — the caller then conservatively assumes no
        checks are pending and exits the poll.
        """
        try:
            checks = gh_pr_checks(pr_number, dry_run=self.options.dry_run)
        except Exception as exc:
            logger.info(
                "PR #%s: failed to fetch CI checks for BLOCKED pending guard (%s)",
                pr_number,
                exc,
            )
            return []
        if not checks:
            return []
        required = [c for c in checks if c.get("required")] or checks
        return [c.get("name", "") for c in required if c.get("status") != "completed"]
