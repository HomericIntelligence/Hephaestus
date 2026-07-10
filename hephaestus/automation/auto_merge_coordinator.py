"""Auto-merge arming and terminal-state routing for drive-green."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from typing import Any, Literal, cast

from hephaestus.constants import read_timeout_env
from hephaestus.github.auto_merge import defer_auto_merge

from .git_utils import issue_ref, pr_ref
from .models import CIDriverOptions, WorkerResult
from .pr_manager import pr_has_implementation_go_label

logger = logging.getLogger(__name__)

AUTO_MERGE_POLICY_CHECK = "auto-merge-policy"
TerminalOutcome = Literal["MERGED", "CLOSED", "FAILING", "DIRTY", "BLOCKED", "TIMEOUT"]


def without_auto_merge_policy(check_names: list[str]) -> list[str]:
    """Return failing checks that can plausibly be fixed by a CI-fix agent."""
    return [name for name in check_names if name != AUTO_MERGE_POLICY_CHECK]


class AutoMergeCoordinator:
    """Owns auto-merge writes and terminal PR polling."""

    def __init__(
        self,
        *,
        options_provider: Callable[[], CIDriverOptions],
        status_tracker_provider: Callable[[], Any],
        get_pr_branch: Callable[[int], str],
        is_bot_pr_mode: Callable[[int, int], bool],
        gh_call: Callable[..., subprocess.CompletedProcess[str]],
        gh_pr_state: Callable[[int], dict[str, Any] | None],
        gh_pr_checks: Callable[[int, bool], list[dict[str, Any]]],
        failing_required_check_names: Callable[[int], list[str]],
        pending_required_check_names: Callable[[int], list[str]],
        fix_flow: Any,
        arming: Any,
        review_threads: Any,
        attempt_mechanical_rebase: Callable[[int, int, int], bool],
        recheck_and_arm_after_fix: Callable[..., WorkerResult | None],
    ) -> None:
        """Initialise auto-merge dependencies."""
        self._options = options_provider
        self._status = status_tracker_provider
        self._get_pr_branch = get_pr_branch
        self._is_bot_pr_mode = is_bot_pr_mode
        self._gh_call = gh_call
        self._gh_pr_state = gh_pr_state
        self._gh_pr_checks = gh_pr_checks
        self._failing_required_check_names = failing_required_check_names
        self._pending_required_check_names = pending_required_check_names
        self._fix_flow = fix_flow
        self._arming = arming
        self._review_threads = review_threads
        self._attempt_mechanical_rebase = attempt_mechanical_rebase
        self._recheck_and_arm_after_fix = recheck_and_arm_after_fix

    def defer_auto_merge(self, pr_number: int) -> bool:
        """Fail closed by disabling and reading back auto-merge for one PR."""
        return defer_auto_merge(pr_number, lambda args: self._gh_call(args, check=False))

    def arm_all_unarmed_open_prs(self, open_prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Contain every open PR retained by the legacy final-sweep API.

        The historical name is kept for compatibility, but #2054 reverses its
        behavior: an existing arm is disabled and a new arm is never created.
        Open PRs remain in the result so callers report that manual action is
        still required.
        """
        contained_prs: list[dict[str, Any]] = []
        disable_failures: list[int] = []
        for pr in open_prs:
            number = pr.get("number")
            if not isinstance(number, int) or number < 0:
                contained_prs.append(pr)
                continue
            if not self.defer_auto_merge(number):
                disable_failures.append(number)
                contained_prs.append(pr)
                continue
            contained_prs.append({**pr, "autoMergeRequest": None})
        if disable_failures:
            logger.error(
                "Could not verify auto-merge disabled for legacy open PR(s): %s",
                sorted(disable_failures),
            )
        return contained_prs

    def arm_and_wait_for_merge(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Enable auto-merge, record arming, and route the terminal outcome."""
        self._status().update_slot(acquired_slot, f"{pr_ref(pr_number)}: enabling auto-merge")
        if self._options().dry_run:
            logger.info(
                "[dry_run] Would enable auto-merge for PR #%s (issue #%s)",
                pr_number,
                issue_number,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        merge_ok = self.enable_auto_merge(
            pr_number, is_bot_pr=self._is_bot_pr_mode(issue_number, pr_number)
        )
        if not merge_ok:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                pr_number=pr_number,
                error=f"auto-merge failed for PR {pr_ref(pr_number)}",
            )
        self._status().update_slot(
            acquired_slot, f"{pr_ref(pr_number)}: arming for post-merge /learn"
        )
        gh_state = self._gh_pr_state(pr_number)
        pr_head_sha = (gh_state or {}).get("headRefOid", "") or ""
        self._arming.record_arming(pr_number, self._get_pr_branch(pr_number), pr_head_sha)
        outcome = self.wait_for_pr_terminal(issue_number, pr_number)
        if outcome == "FAILING":
            fix_result = self._fix_flow.attempt_ci_fixes(issue_number, pr_number, acquired_slot)
            if fix_result is not None and fix_result.success:
                return (
                    self._recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot)
                    or fix_result
                )
            return fix_result or WorkerResult(
                issue_number=issue_number,
                success=False,
                pr_number=pr_number,
                error=f"CI fix failed after {self._options().max_fix_iterations} attempt(s)",
            )
        if outcome == "DIRTY":
            return self.resolve_dirty_pr(issue_number, pr_number, acquired_slot)
        if outcome == "BLOCKED":
            return cast(
                WorkerResult,
                self._review_threads.resolve_blocked_pr(issue_number, pr_number, acquired_slot),
            )
        return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

    def wait_for_pr_terminal(self, issue_number: int, pr_number: int) -> TerminalOutcome:
        """Poll an armed PR until it reaches a terminal or actionable state."""
        if self._options().dry_run:
            return "TIMEOUT"
        max_wait = read_timeout_env("HEPH_PR_MERGE_MAX_WAIT", 1800)
        elapsed = 0
        attempt = 0
        while True:
            gh_state = self._gh_pr_state(pr_number)
            state = ((gh_state or {}).get("state") or "").upper()
            if state == "MERGED":
                logger.info("Issue #%s: PR #%s merged", issue_number, pr_number)
                return "MERGED"
            if state == "CLOSED":
                logger.info("Issue #%s: PR #%s closed without merging", issue_number, pr_number)
                return "CLOSED"
            failing = self._failing_required_check_names(pr_number)
            fixable_failing = without_auto_merge_policy(failing)
            if fixable_failing:
                logger.warning(
                    "Issue #%s: PR #%s went red while awaiting merge (failing: %s)",
                    issue_number,
                    pr_number,
                    ", ".join(fixable_failing),
                )
                return "FAILING"
            merge_status = ((gh_state or {}).get("mergeStateStatus") or "").upper()
            if merge_status in ("DIRTY", "CONFLICTING"):
                logger.warning(
                    "Issue #%s: PR #%s is %s while armed; needs rebase/resolution",
                    issue_number,
                    pr_number,
                    merge_status,
                )
                return "DIRTY"
            policy_only_failure = bool(failing) and not fixable_failing
            if merge_status == "BLOCKED" and not failing:
                pending = self._pending_required_check_names(pr_number)
                if not pending:
                    logger.warning(
                        "Issue #%s: PR #%s is BLOCKED by branch protection",
                        issue_number,
                        pr_number,
                    )
                    return "BLOCKED"
            if merge_status == "BLOCKED" and policy_only_failure:
                logger.info(
                    "Issue #%s: PR #%s is BLOCKED only by auto-merge-policy",
                    issue_number,
                    pr_number,
                )
            sleep_secs = min(2**attempt, 60)
            if elapsed + sleep_secs > max_wait:
                logger.warning(
                    "Issue #%s: PR #%s still OPEN after %ss (limit %ss)",
                    issue_number,
                    pr_number,
                    elapsed,
                    max_wait,
                )
                return "TIMEOUT"
            self._status().update_slot(
                0,
                f"{issue_ref(issue_number)}: PR #{pr_number} awaiting merge ({elapsed}s elapsed)",
            )
            time.sleep(sleep_secs)
            elapsed += sleep_secs
            attempt += 1

    def resolve_dirty_pr(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Resolve an armed-but-DIRTY PR via rebase or a targeted CI-fix session."""
        if self._options().dry_run:
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        if self._attempt_mechanical_rebase(issue_number, pr_number, acquired_slot):
            rearmed = self._recheck_and_arm_after_fix(
                issue_number, pr_number, acquired_slot, resolve_dirty=False
            )
            return rearmed or WorkerResult(
                issue_number=issue_number, success=True, pr_number=pr_number
            )
        base_branch = "main"
        try:
            result = self._gh_call(
                ["pr", "view", str(pr_number), "--json", "baseRefName"], check=False
            )
            base_branch = dict(json.loads(result.stdout or "{}")).get("baseRefName") or "main"
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.debug("Failed to determine PR base branch for #%s: %s", pr_number, exc)
        conflict_context = (
            f"This PR has a MERGE CONFLICT with `origin/{base_branch}` "
            "(mergeStateStatus=DIRTY) - it cannot merge until the conflict is "
            f"resolved. Rebase the PR head branch onto `origin/{base_branch}` and "
            "resolve every conflict, keeping both the PR's intent and the latest "
            "base changes. Then commit the resolution (signed). There may be NO "
            "failing CI checks - the conflict itself is the blocker."
        )
        fix_result = self._fix_flow.attempt_ci_fixes(
            issue_number, pr_number, acquired_slot, extra_context=conflict_context
        )
        if fix_result is not None and fix_result.success:
            return (
                self._recheck_and_arm_after_fix(
                    issue_number, pr_number, acquired_slot, resolve_dirty=False
                )
                or fix_result
            )
        return WorkerResult(
            issue_number=issue_number,
            success=False,
            pr_number=pr_number,
            error=f"PR {pr_ref(pr_number)} has an unresolved merge conflict",
        )

    def enable_auto_merge(self, pr_number: int, is_bot_pr: bool = False) -> bool:
        """Refuse legacy automatic arming until the strict-review gate exists.

        The queue pipeline is the production entry point, but compatibility
        callers must be fail-closed too: a stale implementation-GO label must
        never reactivate auto-merge through this legacy coordinator.
        """
        del is_bot_pr
        if not self.defer_auto_merge(pr_number):
            logger.error("Could not verify auto-merge disabled for PR #%s", pr_number)
            return False
        logger.error("Refusing to arm auto-merge for PR #%s until #2055 lands", pr_number)
        return False

    def pr_has_implementation_go(self, pr_number: int) -> bool:
        """Return whether a PR has the implementation-review GO label."""
        try:
            result = self._gh_call(["pr", "view", str(pr_number), "--json", "labels"], check=False)
            return pr_has_implementation_go_label(dict(json.loads(result.stdout or "{}")))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not fetch PR #%s labels for implementation-GO gate: %s",
                pr_number,
                exc,
            )
            return False
