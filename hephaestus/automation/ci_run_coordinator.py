"""Top-level run coordinator for drive-green."""

from __future__ import annotations

import enum
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, cast

from ._review_utils import drain_completed_futures, print_worker_summary
from .auto_merge_coordinator import (
    AUTO_MERGE_POLICY_CHECK,
    without_auto_merge_policy,
)
from .git_utils import issue_ref, pr_ref
from .models import WorkerResult

logger = logging.getLogger(__name__)


class CiConclusion(enum.Enum):
    """Pure classification of a PR's required-check rollup (issue #1816)."""

    GREEN = "green"
    FAILING = "failing"
    PENDING = "pending"
    NO_CHECKS = "no_checks"


#: The legacy ``all_green`` conclusion set (``ci_driver._drive_issue`` /
#: ``CIDriveRunCoordinator.drive_issue``): a PR is GREEN only when every
#: required check concluded in one of these. CI containment revokes any stale
#: auto-merge request before a repair can change the reviewed head.
GREEN_CONCLUSIONS: frozenset[str] = frozenset({"success", "skipped", "neutral"})


def classify_ci_state(checks: list[dict[str, Any]]) -> CiConclusion:
    """Classify already-fetched CI checks WITHOUT any sleep/backoff.

    Extracted from poll_ci_until_concluded (issue #1816) so a pipeline stage
    can classify in one sub-second call and timer-park on PENDING. GREEN
    mirrors the legacy ``all_green`` semantics EXACTLY (conclusions all in
    :data:`GREEN_CONCLUSIONS`). The pipeline then contains auto-merge and stops
    until the queue review path handles it. Returns:
        NO_CHECKS: empty list — no CI configured; caller treats as success.
        PENDING:   at least one required check has status != "completed".
        GREEN:     all required completed, conclusions all in
                   {success, skipped, neutral} (legacy ``all_green``).
        FAILING:   all required completed but not all green — an explicit
                   "failure" or a residual conclusion (cancelled, timed_out,
                   action_required, ...). The residual class is routed to the
                   fix leg; the legacy driver no-op'd it
                   (``_handle_failing_pr``: "non-failure conclusions are
                   treated as no-op"), which stranded such PRs — sending them
                   to the fix agent is the pipeline's deliberate repair.
    """
    if not checks:
        return CiConclusion.NO_CHECKS
    required = [c for c in checks if c.get("required", False)] or checks
    if not all(c.get("status") == "completed" for c in required):
        return CiConclusion.PENDING
    if all(c.get("conclusion") in GREEN_CONCLUSIONS for c in required):
        return CiConclusion.GREEN
    return CiConclusion.FAILING


class PrMergeState(enum.Enum):
    """Pure classification of a legacy PR's merge state (issue #1816)."""

    MERGED = "merged"
    CLOSED = "closed"
    FAILING = "failing"
    DIRTY = "dirty"
    BLOCKED = "blocked"
    PENDING = "pending"


def classify_pr_merge_state(
    gh_state: dict[str, Any] | None,
    failing: list[str],
    fixable_failing: list[str],
    pending: list[str],
) -> PrMergeState:
    """Branch logic of _wait_for_pr_terminal (issue #1816), no sleep/no I/O.

    Mirrors ci_driver.py:1515-1582 exactly. The policy-only-failure logging
    branch (:1575-1582) is intentionally omitted: it only logs and does NOT
    change the returned state (falls through to PENDING either way), so
    dropping it preserves behavior — verified against ci_driver.py:1575-1582.
    """
    state = ((gh_state or {}).get("state") or "").upper()
    if state == "MERGED":
        return PrMergeState.MERGED
    if state == "CLOSED":
        return PrMergeState.CLOSED
    if fixable_failing:
        return PrMergeState.FAILING
    merge_status = ((gh_state or {}).get("mergeStateStatus") or "").upper()
    if merge_status in ("DIRTY", "CONFLICTING"):
        return PrMergeState.DIRTY
    if merge_status == "BLOCKED" and not failing and not pending:
        return PrMergeState.BLOCKED
    return PrMergeState.PENDING


def _contain_remaining_prs(
    auto_merge: Any, remaining: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Contain every retained final-sweep PR or preserve an empty result."""
    if not remaining:
        return remaining
    return cast(list[dict[str, Any]], auto_merge.arm_all_unarmed_open_prs(remaining))


class CIDriveRunCoordinator:
    """Coordinates a full drive-green run across discovered PR work items."""

    def __init__(
        self,
        *,
        options_provider: Any,
        worktree_manager: Any,
        status_tracker: Any,
        discovery: Any,
        check_inspector: Any,
        fix_flow: Any,
        auto_merge: Any,
        arming: Any,
        set_shared_pr_issues: Any,
    ) -> None:
        """Initialise the run coordinator."""
        self._options = options_provider
        self._worktrees = worktree_manager
        self._status = status_tracker
        self._discovery = discovery
        self._check_inspector = check_inspector
        self._fix_flow = fix_flow
        self._auto_merge = auto_merge
        self._arming = arming
        self._set_shared_pr_issues = set_shared_pr_issues
        self._lock = threading.Lock()
        self.open_prs_remaining: list[dict[str, Any]] = []

    def run(self) -> dict[int, WorkerResult]:
        """Run drive-green for the configured issue/PR inputs."""
        options = self._options()
        logger.info(
            "Starting CI driver for %s issue(s) with %s parallel workers",
            len(options.issues),
            options.max_workers,
        )
        if not options.dry_run:
            self._arming.sweep_orphaned_records()
        if not options.issues and not options.prs and not options.include_bot_prs:
            logger.warning("No issues, no direct PRs, and bot-PR discovery disabled")
            return {}
        try:
            workset = self._discovery.discover_workset(options.issues)
        except Exception:
            # Discovery is untrusted input to the containment boundary. A
            # failure cannot prove the repository has no armed PRs, so run the
            # full final sweep before propagating the discovery failure.
            self.open_prs_remaining = self._final_open_prs({})
            raise
        self._set_shared_pr_issues(workset.shared_pr_issues)
        pr_map = workset.pr_map
        if not pr_map:
            logger.warning(
                "No open PRs found for the specified issues (and no open bot PRs) "
                "- nothing to drive"
            )
            self.open_prs_remaining = self._final_open_prs(pr_map)
            return {}
        logger.info("Found %s PR(s) to drive to green: %s", len(pr_map), pr_map)
        try:
            results = self._drive_pr_map(pr_map)
        finally:
            if not options.dry_run:
                try:
                    self._worktrees.cleanup_all()
                except Exception:
                    logger.exception("Error during worktree cleanup in CIDriver.run()")
            if self._worktrees.preserved:
                logger.info("Preserved worktrees (contain uncommitted changes):")
                for issue_num, path in self._worktrees.preserved:
                    logger.info("  #%d: %s", issue_num, path)
        print_worker_summary("CI Driver Summary", results)
        self.open_prs_remaining = self._final_open_prs(pr_map)
        return results

    def _drive_pr_map(self, pr_map: dict[int, int]) -> dict[int, WorkerResult]:
        results: dict[int, WorkerResult] = {}
        with ThreadPoolExecutor(max_workers=self._options().max_workers) as executor:
            futures: dict[Future[Any], int] = {}
            for idx, (issue_num, pr_num) in enumerate(pr_map.items()):
                future = executor.submit(self.drive_issue, issue_num, pr_num, idx)
                futures[future] = issue_num
            for future in drain_completed_futures(futures):
                issue_num = futures.pop(future)
                try:
                    result = future.result()
                    with self._lock:
                        results[issue_num] = result
                    if result.success:
                        logger.info("Issue #%s: CI drive completed", issue_num)
                    else:
                        logger.error("Issue #%s: CI drive failed: %s", issue_num, result.error)
                except Exception as exc:
                    logger.error("Issue #%s raised exception: %s", issue_num, exc)
                    with self._lock:
                        results[issue_num] = WorkerResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(exc),
                        )
        return results

    def _final_open_prs(self, pr_map: dict[int, int]) -> list[dict[str, Any]]:
        if self._options().dry_run:
            return []
        remaining = cast(list[dict[str, Any]], self._discovery.list_open_prs_remaining())
        if self._options().issues and pr_map and remaining:
            scoped_prs = set(pr_map.values())
            complete_returned_identity = all(
                isinstance(pr.get("number"), int)
                and pr["number"] > 0
                and isinstance(pr.get("headRefName"), str)
                and pr["headRefName"].strip()
                for pr in remaining
            )
            if complete_returned_identity:
                scoped_rows = [pr for pr in remaining if pr.get("number") in scoped_prs]
                if {pr["number"] for pr in scoped_rows} != scoped_prs:
                    return _contain_remaining_prs(self._auto_merge, remaining)
                scoped_heads = {str(pr["headRefName"]).strip() for pr in scoped_rows}
                remaining = [
                    pr
                    for pr in remaining
                    if pr.get("number") == -1
                    or pr.get("number") in scoped_prs
                    or (
                        isinstance(pr.get("headRefName"), str)
                        and pr["headRefName"].strip() in scoped_heads
                    )
                ]
        remaining = _contain_remaining_prs(self._auto_merge, remaining)
        if remaining:
            logger.warning("%d open PR(s) remain on the repo - not done:", len(remaining))
            for pr in remaining:
                am_state = (
                    "armed (waiting on CI / branch protection)"
                    if pr.get("autoMergeRequest")
                    else "NOT armed (needs manual action)"
                )
                logger.warning(
                    "  - PR #%s %r head=%s auto-merge=%s",
                    pr.get("number"),
                    pr.get("title", ""),
                    pr.get("headRefName", ""),
                    am_state,
                )
        return remaining

    def drive_issue(self, issue_number: int, pr_number: int, slot_id: int) -> WorkerResult:
        """Contain a legacy PR and stop until the queue review path handles it."""
        with self._status.slot() as acquired_slot:
            if acquired_slot is None:
                return WorkerResult(
                    issue_number=issue_number,
                    success=False,
                    error="Failed to acquire worker slot",
                )
            try:
                if not self._auto_merge.defer_auto_merge(pr_number):
                    return WorkerResult(
                        issue_number=issue_number,
                        success=False,
                        pr_number=pr_number,
                        error="auto_merge_disable_failed",
                    )
                return WorkerResult(
                    issue_number=issue_number,
                    success=False,
                    pr_number=pr_number,
                    error="merge_wait_unavailable",
                )
            except Exception as exc:
                logger.error("Issue #%s: unexpected error: %s", issue_number, exc)
                return WorkerResult(issue_number=issue_number, success=False, error=str(exc)[:200])

    def poll_ci_until_concluded(
        self, issue_number: int, pr_number: int, acquired_slot: int, max_wait: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        """Poll CI until all required checks conclude, no checks exist, or timeout hits."""
        poll_elapsed = 0
        poll_attempt = 0
        while True:
            checks = self._check_inspector.gh_pr_checks(pr_number, self._options().dry_run)
            conclusion = classify_ci_state(checks)
            if conclusion is CiConclusion.NO_CHECKS:
                logger.info("Issue #%s: no CI checks found for PR #%s", issue_number, pr_number)
                return None
            if conclusion is not CiConclusion.PENDING:  # GREEN or FAILING → concluded
                required_checks = [c for c in checks if c.get("required", False)] or checks
                return checks, required_checks
            sleep_secs = min(2**poll_attempt, 60)
            if poll_elapsed + sleep_secs > max_wait:
                logger.warning(
                    "Issue #%s: CI checks still pending after %ss (limit %ss)",
                    issue_number,
                    poll_elapsed,
                    max_wait,
                )
                return None
            self._status.update_slot(
                acquired_slot,
                f"{pr_ref(pr_number)}: waiting for CI checks "
                f"(attempt {poll_attempt + 1}, {poll_elapsed}s elapsed)",
            )
            time.sleep(sleep_secs)
            poll_elapsed += sleep_secs
            poll_attempt += 1

    def handle_failing_pr(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        required_checks: list[dict[str, Any]],
    ) -> WorkerResult:
        """Handle a PR whose required checks concluded non-green."""
        failing = [c for c in required_checks if c.get("conclusion") == "failure"]
        if not failing:
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        failing_names = [str(c.get("name") or "") for c in failing]
        fixable_names = without_auto_merge_policy(failing_names)
        if not fixable_names and AUTO_MERGE_POLICY_CHECK in failing_names:
            if not self._auto_merge.pr_has_implementation_go(pr_number):
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
            return cast(
                WorkerResult,
                self._auto_merge.arm_and_wait_for_merge(issue_number, pr_number, acquired_slot),
            )
        fix_result = cast(
            WorkerResult | None,
            self._fix_flow.attempt_ci_fixes(issue_number, pr_number, acquired_slot),
        )
        if fix_result is not None and fix_result.success:
            return (
                self.recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot) or fix_result
            )
        if fix_result is not None:
            return fix_result
        return WorkerResult(
            issue_number=issue_number,
            success=False,
            pr_number=pr_number,
            error=f"CI fix failed after {self._options().max_fix_iterations} attempt(s)",
        )

    def recheck_and_arm_after_fix(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        *,
        resolve_dirty: bool = True,
    ) -> WorkerResult | None:
        """Re-poll post-fix CI and contain legacy auto-merge when the PR is green."""
        if self._options().dry_run:
            return None
        checks = _poll_post_fix_required(
            issue_number,
            pr_number,
            acquired_slot,
            self._options().poll_max_wait,
            self._options().dry_run,
            self._check_inspector.gh_pr_checks,
            self._status,
        )
        if not checks:
            return None
        if not all(c.get("conclusion") in ("success", "skipped", "neutral") for c in checks):
            return None
        if not self._auto_merge.pr_has_implementation_go(pr_number):
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        merge = self._auto_merge.enable_auto_merge(
            pr_number, is_bot_pr=self._discovery.is_bot_pr_mode(issue_number, pr_number)
        )
        if not merge:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                pr_number=pr_number,
                error=f"auto-merge failed for PR {pr_ref(pr_number)}",
            )
        gh_state = self._auto_merge._gh_pr_state(pr_number)
        self._arming.record_arming(
            pr_number,
            self._auto_merge._get_pr_branch(pr_number),
            (gh_state or {}).get("headRefOid", "") or "",
        )
        outcome = self._auto_merge.wait_for_pr_terminal(issue_number, pr_number)
        if resolve_dirty and outcome == "DIRTY":
            return cast(
                WorkerResult,
                self._auto_merge.resolve_dirty_pr(issue_number, pr_number, acquired_slot),
            )
        return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)


def _poll_post_fix_required(
    issue_number: int,
    pr_number: int,
    acquired_slot: int,
    max_wait: int,
    dry_run: bool,
    gh_pr_checks: Any,
    status_tracker: Any,
) -> list[dict[str, Any]] | None:
    elapsed = 0
    attempt = 0
    while True:
        checks = gh_pr_checks(pr_number, dry_run)
        required = [c for c in checks if c.get("required", False)] or checks
        if required and all(c.get("status") == "completed" for c in required):
            return required
        sleep_secs = min(2**attempt, 60)
        if elapsed + sleep_secs > max_wait:
            logger.info(
                "Issue #%s: post-fix CI still pending after %ss; leaving for next run",
                issue_number,
                elapsed,
            )
            return None
        status_tracker.update_slot(
            acquired_slot,
            f"{issue_ref(issue_number)}: awaiting post-fix CI ({elapsed}s)",
        )
        time.sleep(sleep_secs)
        elapsed += sleep_secs
        attempt += 1
