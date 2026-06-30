"""CI driver automation: polls CI checks and drives PRs to green.

Provides:
- Parallel CI check polling across multiple issues
- Automatic fix session on red required checks
- Auto-merge enablement when all required checks are green
- Dry-run support with early return before any GitHub write or git push
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    resolve_agent,
    run_agent_session,
    uses_direct_agent_runner,
)
from hephaestus.cli.utils import (
    add_advise_timeout_arg,
    add_agent_timeout_arg,
    add_learn_timeout_arg,
    add_poll_max_wait_arg,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.constants import AUTOMATION_LOG_FORMAT, LOG_DATEFMT, read_timeout_env
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock

from ._review_utils import (
    _discover_prs_simple,
    build_automation_parser,
    ensure_state_dir,
    find_pr_for_issue,
    load_impl_session_id,
    load_state_file,
    log_file_path,
    parse_json_block,
    print_worker_summary,
)
from .address_review import (
    _parse_addressed_block,
    resolve_addressed_threads,
    run_address_fix_session,
)
from .advise_runner import run_advise
from .arming_state import ArmingStateStore
from .ci_check_inspector import (
    FAILING_CHECK_CONCLUSIONS as FAILING_CHECK_CONCLUSIONS,  # re-export
    CICheckInspector,
)
from .ci_fix_orchestrator import CIFixOrchestrator
from .claude_invoke import invoke_claude_with_session
from .claude_models import advise_model, codex_advise_model
from .claude_timeouts import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_CI_POLL_MAX_WAIT,
)
from .git_utils import (
    get_repo_root,
    get_repo_slug,
    issue_ref,
    pr_ref,
)
from .github_api import (
    _gh_call,
    gh_issue_json,
    gh_pr_checks,
    gh_pr_list_unresolved_threads,
    gh_pr_resolve_thread,
)
from .models import CIDriverOptions, WorkerResult
from .post_merge_processor import PostMergeProcessor
from .pr_discovery import PRDiscovery
from .pr_manager import pr_has_implementation_go_label
from .prompts import get_advise_prompt_builder
from .session_naming import AGENT_ADVISE
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)

# FAILING_CHECK_CONCLUSIONS moved to ci_check_inspector.py (#1357) and
# re-exported from the import block above for backward compatibility — the
# module-level _pr_is_failing predicate and loop_runner._count_failing_prs
# both still import it from here.

# Max address-review passes for a green-but-BLOCKED PR before leaving it armed
# (#1348). The progress guard stops earlier whenever a pass resolves no new
# threads; this cap bounds the case where each pass keeps making *some* progress
# but never fully clears the set, so an unsatisfiable thread can never spin
# forever.
_BLOCKED_ADDRESS_MAX_ATTEMPTS = 2
_AUTO_MERGE_POLICY_CHECK = "auto-merge-policy"


def _without_auto_merge_policy(check_names: list[str]) -> list[str]:
    """Return failing checks that can plausibly be fixed by a CI-fix agent."""
    return [name for name in check_names if name != _AUTO_MERGE_POLICY_CHECK]


def _pr_is_failing(pr: dict[str, Any]) -> bool:
    """Return True iff this PR row is one drive-green should pick up.

    A PR is "failing" when it is open, non-draft, and either
    mergeStateStatus is BLOCKED or any statusCheckRollup entry's
    conclusion is in FAILING_CHECK_CONCLUSIONS. BLOCKED captures the
    branch-protection/required-review-not-met case; the conclusion check
    captures every CI red. PENDING is intentionally excluded — the driver
    waits for terminal state elsewhere.
    """
    if pr.get("isDraft"):
        return False
    if pr.get("mergeStateStatus") == "BLOCKED":
        return True
    rollup = pr.get("statusCheckRollup") or []
    return any(c.get("conclusion") in FAILING_CHECK_CONCLUSIONS for c in rollup)


class CIDriver:
    """Drives open PRs toward green CI by fixing failures and enabling auto-merge.

    Features:
    - Parallel CI check polling across multiple issues
    - Distinguishes required vs non-required checks
    - Up to ``max_fix_iterations`` fix attempts per failing PR (default 1)
    - Enables auto-merge once all required checks are green
    - Dry-run mode exits before any write or push
    """

    def __init__(self, options: CIDriverOptions) -> None:
        """Initialize the CI driver.

        Args:
            options: CI driver configuration options.

        """
        self.options = options
        self.repo_root = get_repo_root()
        self.state_dir = ensure_state_dir(self.repo_root)
        self._arming_store = ArmingStateStore(lambda: self.state_dir)

        self.worktree_manager = WorktreeManager()
        self.status_tracker = StatusTracker(options.max_workers)
        self.lock = threading.Lock()
        # Populated at the end of run() with the open PRs left on the repo
        # after the per-issue drive completes. The repo is only "done" when
        # this is empty (#838).
        self.open_prs_remaining: list[dict[str, Any]] = []
        # Populated by _discover_prs: maps pr_number -> ALL issues that
        # resolved to that PR. Used when arming /learn for the success path
        # so every sibling issue covered by a multi-issue PR gets its own
        # arming record (and therefore its own /learn capture once the PR
        # actually merges in a subsequent run). #840 +on top of #834.
        self.shared_pr_issues: dict[int, list[int]] = {}

        # Narrow-callable collaborators extracted from CIDriver (#1357 /
        # refs #1179, #1289). Each receives only the specific Callable[[], T]
        # providers / bound methods it needs (DIP) — never ``self``. The
        # injected callables are wrapped in lambdas (or passed as bound methods
        # that delegate further) so ``patch.object(driver, "_method")`` in tests
        # still intercepts through the indirection at call time. The viewer-login
        # cache (#821) now lives inside PRDiscovery.
        self._pr_discovery = PRDiscovery(
            options_provider=lambda: self.options,
            status_tracker_provider=lambda: self.status_tracker,
            repo_root_provider=lambda: self.repo_root,
            pr_merge_state_provider=lambda pr_number: self._pr_merge_state(pr_number),
        )
        self._check_inspector = CICheckInspector(
            get_pr_branch=lambda pr_number: self._get_pr_branch(pr_number),
            options_provider=lambda: self.options,
        )
        self._fix_orchestrator = CIFixOrchestrator(
            options_provider=lambda: self.options,
            repo_root_provider=lambda: self.repo_root,
            state_dir_provider=lambda: self.state_dir,
            status_tracker_provider=lambda: self.status_tracker,
            get_pr_branch=lambda pr_number: self._get_pr_branch(pr_number),
            get_worktree_path=lambda issue_number, pr_number: self._get_worktree_path(
                issue_number, pr_number
            ),
            format_review_threads_block=lambda pr_number: self._format_review_threads_block(
                pr_number
            ),
            failing_required_check_names=lambda pr_number: self._failing_required_check_names(
                pr_number
            ),
        )
        self._post_merge = PostMergeProcessor(
            options_provider=lambda: self.options,
            repo_root_provider=lambda: self.repo_root,
            get_worktree_path=lambda issue_number, pr_number: self._get_worktree_path(
                issue_number, pr_number
            ),
            load_arming_state=lambda issue_number: self._load_arming_state(issue_number),
            save_arming_state=lambda issue_number, record: self._save_arming_state(
                issue_number, record
            ),
        )

    def run(self) -> dict[int, WorkerResult]:  # noqa: C901  # orchestration: thread pool + finally + preserve report across exception paths
        """Run the CI driver on all configured issues.

        Returns:
            Dictionary mapping issue number to WorkerResult.

        """
        logger.info(
            "Starting CI driver for %s issue(s) with %s parallel workers",
            len(self.options.issues),
            self.options.max_workers,
        )

        # Sweep orphaned arming records BEFORE discovery (#848). PRs whose
        # tracking issue is closed will not appear in any future issue list,
        # so the only chance to fire their post-merge ``/learn`` is a
        # state_dir scan independent of the run's input issue list.
        if not self.options.dry_run:
            self._sweep_orphaned_arming_records()

        # Empty --issues is allowed: bot-PR discovery (#848) and failing-PR
        # discovery (#819) may still surface open PRs to drive. Only abort if
        # ALL input sources are off: no issues, no direct PRs, and no bot
        # discovery (failing-PR discovery auto-enables when --issues is empty,
        # so it has no opt-out to check).
        if not self.options.issues and not self.options.prs and not self.options.include_bot_prs:
            logger.warning("No issues, no direct PRs, and bot-PR discovery disabled")
            return {}

        # Pre-discover PRs — only submit workers for issues that have an open PR.
        # This prevents Claude from being launched for issues with no PR at all.
        pr_map = self._discover_prs(self.options.issues)
        if not pr_map:
            logger.warning(
                "No open PRs found for the specified issues (and no open bot PRs) "
                "— nothing to drive"
            )
            return {}

        logger.info("Found %s PR(s) to drive to green: %s", len(pr_map), pr_map)

        results: dict[int, WorkerResult] = {}

        try:
            with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
                futures: dict[Future[Any], int] = {}

                for idx, (issue_num, pr_num) in enumerate(pr_map.items()):
                    future = executor.submit(self._drive_issue, issue_num, pr_num, idx)
                    futures[future] = issue_num

                while futures:
                    try:
                        done, _pending = wait(
                            futures.keys(), timeout=1.0, return_when=FIRST_COMPLETED
                        )
                    except Exception:
                        time.sleep(0.1)
                        continue

                    for future in done:
                        issue_num = futures.pop(future)
                        try:
                            result = future.result()
                            with self.lock:
                                results[issue_num] = result
                            if result.success:
                                logger.info("Issue #%s: CI drive completed", issue_num)
                            else:
                                logger.error(
                                    "Issue #%s: CI drive failed: %s", issue_num, result.error
                                )
                        except Exception as e:
                            logger.error("Issue #%s raised exception: %s", issue_num, e)
                            with self.lock:
                                results[issue_num] = WorkerResult(
                                    issue_number=issue_num,
                                    success=False,
                                    error=str(e),
                                )
        finally:
            # Always clean up worktrees, even on KeyboardInterrupt or exception.
            # Mirror the pattern from implementer.py:178-185.
            if not self.options.dry_run:
                try:
                    self.worktree_manager.cleanup_all()
                except Exception:
                    logger.exception("Error during worktree cleanup in CIDriver.run()")

            # Report any worktrees that were preserved due to uncommitted changes.
            preserved = self.worktree_manager.preserved
            if preserved:
                logger.info("Preserved worktrees (contain uncommitted changes):")
                for issue_num, path in preserved:
                    logger.info("  #%d: %s", issue_num, path)
                logger.info("Inspect or discard them with: git worktree remove --force <path>")

        self._print_summary(results)

        # After every input issue's drive completes, verify the repo is truly
        # clean by listing remaining open PRs. The repo is only "done" when
        # this is empty (#838). Stored on the driver so ``main()`` can check
        # it without changing ``run()``'s established return type.
        self.open_prs_remaining = [] if self.options.dry_run else self._list_open_prs_remaining()
        # When the operator scoped the run with --issues, "repo done" and
        # auto-merge arming must consider ONLY the PRs this run drove (the
        # pr_map values), not every open PR on the repo. Otherwise a scoped
        # run arms unrelated PRs and fails rc=1 on out-of-scope open PRs (POLA,
        # mirrors the discovery gates above). The no-args sweep keeps the full
        # repo-wide done-check.
        if self.options.issues and self.open_prs_remaining:
            scoped_prs = set(pr_map.values())
            self.open_prs_remaining = [
                pr for pr in self.open_prs_remaining if pr.get("number") in scoped_prs
            ]
        # Arm auto-merge on every implementation-GO un-armed open PR before
        # the final report (#882). The implementation-GO label is the policy
        # gate: PRs must not be queued for merge until in-loop implementation
        # review has approved them.
        if self.open_prs_remaining and not self.options.dry_run:
            self.open_prs_remaining = self._arm_all_unarmed_open_prs(self.open_prs_remaining)
        if self.open_prs_remaining:
            logger.warning(
                "%d open PR(s) remain on the repo — not done:",
                len(self.open_prs_remaining),
            )
            for pr in self.open_prs_remaining:
                # ``autoMergeRequest`` is None when auto-merge is not armed;
                # the blob is non-None otherwise. We don't trust auto-merge
                # alone to mean "done" since CI may still fail, branch
                # protection may block, etc. — but we surface the state for
                # triage so operators can see WHY each PR is still open.
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

        return results

    def _list_open_prs_remaining(self) -> list[dict[str, Any]]:
        """Delegate to PRDiscovery (extracted #1357)."""
        return self._pr_discovery.list_open_prs_remaining()

    def _pr_merge_state(self, pr_number: Any) -> tuple[str, str]:
        """Delegate to PRDiscovery (extracted #1357)."""
        return self._pr_discovery.pr_merge_state(pr_number)

    def _arm_all_unarmed_open_prs(self, open_prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Arm auto-merge on every implementation-GO un-armed open PR (#882).

        The per-issue drive only arms PRs it processed; a PR fixed-and-pushed by
        the driver (or one that arrived green from another actor) can end CLEAN
        but un-armed and never merge. This pass arms only PRs already marked
        ``state:implementation-go`` so review approval remains the merge gate.

        Returns the open-PR list with ``autoMergeRequest`` refreshed for any PR
        that armed, so the caller's final gate reports the true state.
        """
        armed_now: list[int] = []
        for pr in open_prs:
            number = pr.get("number")
            if not isinstance(number, int) or number < 0:
                continue
            if pr.get("autoMergeRequest"):
                continue  # already armed
            if not pr_has_implementation_go_label(pr):
                logger.info(
                    "PR #%s lacks state:implementation-go; leaving auto-merge disabled",
                    number,
                )
                continue
            if self._enable_auto_merge(number, is_bot_pr=bool(pr.get("isBot"))):
                armed_now.append(number)
        if not armed_now:
            return open_prs
        logger.info(
            "Armed auto-merge on %d previously-unarmed open PR(s): %s",
            len(armed_now),
            sorted(armed_now),
        )
        # Re-list so the final gate sees the freshly-armed PRs as armed_pending
        # rather than needs_action.
        return self._list_open_prs_remaining()

    def _resolve_viewer_login(self) -> str:
        """Delegate to PRDiscovery (extracted #1357)."""
        return self._pr_discovery.resolve_viewer_login()

    def _discover_bot_prs(self) -> dict[int, int]:
        """Delegate to PRDiscovery (extracted #1357)."""
        return self._pr_discovery.discover_bot_prs()

    def _discover_failing_prs(self) -> dict[int, int]:
        """Delegate to PRDiscovery (extracted #1357)."""
        return self._pr_discovery.discover_failing_prs(_pr_is_failing)

    def _is_bot_pr_mode(self, issue_number: int, pr_number: int) -> bool:
        """Delegate to PRDiscovery (extracted #1357)."""
        return self._pr_discovery.is_bot_pr_mode(issue_number, pr_number)

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:  # noqa: C901  # orchestration: multi-PR dedup with fallback discovery across issue/PR mappings
        """Pre-discover open PRs for all issues, deduped by PR.

        When a single PR closes multiple issues (a legitimate ``pr-policy``
        configuration — audit rollups, dependency bumps that cover several
        CVEs, etc.), every one of those issues resolves to the same PR. The
        downstream worker loop would then race N threads to check the same
        branch out into N different worktree paths, and ``git worktree add``
        rejects all but the first because a branch can only be checked out
        once. The losers were marked CI-failed even though the PR was being
        driven correctly by the first issue (#834).

        We dedupe at discovery time: keep one canonical issue per PR (the
        lowest-numbered, for deterministic ordering and stable logs), and
        defer the others.

        When ``options.include_bot_prs`` is True (default), the result is
        unioned with every open ``is_bot=true`` PR on the repo (#848). Bot
        PRs lack ``Closes #N`` links and would otherwise be invisible. Each
        bot PR is keyed by its own number as the synthetic issue.

        Args:
            issue_numbers: Issue numbers to check

        Returns:
            Mapping of canonical_issue_number -> pr_number, with at most one
            entry per PR.

        """
        # Per-issue lookup first; preserve insertion order so the "lowest
        # numbered issue wins" tie-break is stable across runs given the same
        # input list.
        raw_map = _discover_prs_simple(
            issue_numbers,
            find_pr_for_issue,
            on_missing=lambda issue_num: logger.info(
                "Issue #%s: no open PR found, skipping", issue_num
            ),
        )

        # Group by PR, then pick a canonical issue per PR (the smallest one)
        # and log the deferred siblings so operators can see the dedupe.
        pr_to_issues: dict[int, list[int]] = {}
        for issue_num, pr_num in raw_map.items():
            pr_to_issues.setdefault(pr_num, []).append(issue_num)

        # Stash the full PR→[issues] map so the success path (#840) can write
        # an arming record for *every* sibling issue when a shared-PR group
        # auto-merge-arms. Without this, only the canonical issue would ever
        # get its post-merge ``/learn`` capture; the other N-1 deferred
        # issues would silently lose their lessons.
        self.shared_pr_issues = {pr: sorted(issues) for pr, issues in pr_to_issues.items()}

        deduped: dict[int, int] = {}
        for pr_num, issues in pr_to_issues.items():
            canonical = min(issues)
            deduped[canonical] = pr_num
            if len(issues) > 1:
                deferred = sorted(i for i in issues if i != canonical)
                logger.info(
                    "PR #%s closes multiple issues %s; driving via issue #%s, "
                    "deferring %s (single PR cannot be checked out into multiple "
                    "worktrees concurrently)",
                    pr_num,
                    sorted(issues),
                    canonical,
                    deferred,
                )

        # Direct PR mode (#918). Operator-supplied PR numbers bypass
        # find_pr_for_issue entirely. Validate each PR exists and is OPEN
        # via ``gh pr view`` before adding to the work set; invalid PRs are
        # logged and skipped, not raised, so one typo doesn't kill the batch.
        # Keyed by PR number as the synthetic issue, matching the bot-PR
        # convention so ``_is_bot_pr_mode`` short-circuits downstream.
        for pr_num in self.options.prs:
            if pr_num in deduped.values():
                logger.info(
                    "Direct PR #%s already discovered via --issues; skipping duplicate",
                    pr_num,
                )
                continue
            if not self._validate_pr_open(pr_num):
                logger.warning("Direct PR #%s is not OPEN or does not exist; skipping", pr_num)
                continue
            deduped[pr_num] = pr_num
            self.shared_pr_issues.setdefault(pr_num, [pr_num])

        # Union with open bot-authored PRs (#848). Bot PRs are keyed by their
        # own number as the synthetic issue; ``_is_bot_pr_mode`` detects the
        # equality and short-circuits downstream ``gh issue view`` calls that
        # would otherwise 404 on the synthetic key. PRs already discovered via
        # the issue-driven path are NOT overwritten — a bot PR that happens to
        # carry a Closes link (rare but possible) stays under its real issue.
        #
        # Like failing-PR discovery below, this widening is suppressed when the
        # operator passed --issues: a scoped run must touch ONLY the selected
        # issues' PRs, not unrelated Dependabot PRs (POLA, #819).
        if self.options.include_bot_prs and not self.options.issues:
            bot_prs = self._discover_bot_prs()
            for pr_num, _ in bot_prs.items():
                if pr_num in deduped.values():
                    continue
                deduped[pr_num] = pr_num
                # Bot PRs are PR-scoped only; record the PR-as-issue mapping
                # so the shared-PR fan-out machinery treats the bot PR as a
                # solo work item without inventing nonexistent issue numbers.
                self.shared_pr_issues.setdefault(pr_num, [pr_num])

        # Union with failing-PR discovery ONLY when the operator did not pass
        # --issues. The issue's POLA contract (#819) says: "when provided, keep
        # today's issue-driven path." So scoped runs stay narrow; only the
        # no-args path widens to "every failing PR on the repo."
        if not self.options.issues:
            already_known: set[int] = set(deduped.values())
            failing_prs = self._discover_failing_prs()
            for pr_num in failing_prs:
                if pr_num in already_known:
                    continue
                deduped[pr_num] = pr_num
                already_known.add(pr_num)
                self.shared_pr_issues.setdefault(pr_num, [pr_num])
        return deduped

    def _poll_ci_until_concluded(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        max_wait: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        """Poll CI checks with exponential backoff until all required checks conclude.

        Returns ``(checks, required_checks)`` when all checks have concluded,
        or None when no checks exist or the poll deadline is exceeded.
        """
        poll_elapsed = 0
        poll_attempt = 0
        while True:
            checks: list[dict[str, Any]] = gh_pr_checks(pr_number, dry_run=self.options.dry_run)
            if not checks:
                logger.info("Issue #%s: no CI checks found for PR #%s", issue_number, pr_number)
                return None

            required_checks = [c for c in checks if c.get("required", False)] or checks
            if all(c["status"] == "completed" for c in required_checks):
                return checks, required_checks

            sleep_secs = min(2**poll_attempt, 60)
            if poll_elapsed + sleep_secs > max_wait:
                logger.warning(
                    "Issue #%s: CI checks still pending after %ss (limit %ss), "
                    "treating as not yet failing",
                    issue_number,
                    poll_elapsed,
                    max_wait,
                )
                return None

            self.status_tracker.update_slot(
                acquired_slot,
                f"{pr_ref(pr_number)}: waiting for CI checks (attempt {poll_attempt + 1}, {poll_elapsed}s elapsed)",  # noqa: E501
            )
            logger.debug(
                "Issue #%s: CI checks pending, sleeping %ss (attempt %s, %ss elapsed)",
                issue_number,
                sleep_secs,
                poll_attempt + 1,
                poll_elapsed,
            )
            time.sleep(sleep_secs)
            poll_elapsed += sleep_secs
            poll_attempt += 1

    def _arm_and_wait_for_merge(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Enable auto-merge, arm the drive-green record, then block until terminal state.

        If CI goes red post-arm, falls into the fix path. DIRTY/BLOCKED outcomes
        are delegated to their respective helpers.
        """
        self.status_tracker.update_slot(acquired_slot, f"{pr_ref(pr_number)}: enabling auto-merge")
        if self.options.dry_run:
            logger.info(
                "[dry_run] Would enable auto-merge for PR #%s (issue #%s)", pr_number, issue_number
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        merge_ok = self._enable_auto_merge(
            pr_number, is_bot_pr=self._is_bot_pr_mode(issue_number, pr_number)
        )
        if merge_ok:
            self.status_tracker.update_slot(
                acquired_slot, f"{pr_ref(pr_number)}: arming for post-merge /learn"
            )
            gh_state = self._gh_pr_state(pr_number)
            pr_head_sha = (gh_state or {}).get("headRefOid", "") or ""
            self._arm_drive_green(pr_number, self._get_pr_branch(pr_number), pr_head_sha)

            outcome = self._wait_for_pr_terminal(issue_number, pr_number)
            if outcome == "FAILING":
                fix_result = self._attempt_ci_fixes(issue_number, pr_number, acquired_slot)
                if fix_result is not None and fix_result.success:
                    rearmed = self._recheck_and_arm_after_fix(
                        issue_number, pr_number, acquired_slot
                    )
                    return rearmed if rearmed is not None else fix_result
                if fix_result is not None:
                    return fix_result
                return WorkerResult(
                    issue_number=issue_number,
                    success=False,
                    pr_number=pr_number,
                    error=f"CI fix failed after {self.options.max_fix_iterations} attempt(s)",
                )
            if outcome == "DIRTY":
                return self._resolve_dirty_pr(issue_number, pr_number, acquired_slot)
            if outcome == "BLOCKED":
                return self._resolve_blocked_pr(issue_number, pr_number, acquired_slot)
        return WorkerResult(
            issue_number=issue_number,
            success=merge_ok,
            pr_number=pr_number,
            error=None if merge_ok else f"auto-merge failed for PR {pr_ref(pr_number)}",
        )

    def _handle_green_pr(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Handle a PR where all required checks are green.

        Short-circuits if the implementation-review GO label is missing (the
        implementer loop hasn't approved yet). Otherwise enables auto-merge and
        blocks until terminal.
        """
        if not self._pr_has_implementation_go(pr_number):
            logger.info(
                "Issue #%s: PR #%s is green but lacks state:implementation-go; "
                "leaving auto-merge disabled until implementation review approves it",
                issue_number,
                pr_number,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        return self._arm_and_wait_for_merge(issue_number, pr_number, acquired_slot)

    def _handle_failing_pr(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        required_checks: list[dict[str, Any]],
    ) -> WorkerResult:
        """Handle a PR where at least one required check has failed.

        Non-failure conclusions (e.g. cancelled) are treated as no-op.
        """
        failing = [c for c in required_checks if c.get("conclusion") == "failure"]
        if not failing:
            logger.info(
                "Issue #%s: PR #%s checks concluded with non-green, non-failure conclusions (e.g. cancelled)",  # noqa: E501
                issue_number,
                pr_number,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        failing_names = [str(c.get("name") or "") for c in failing]
        fixable_failing_names = _without_auto_merge_policy(failing_names)
        if not fixable_failing_names and _AUTO_MERGE_POLICY_CHECK in failing_names:
            if not self._pr_has_implementation_go(pr_number):
                logger.info(
                    "Issue #%s: PR #%s only fails auto-merge-policy but lacks "
                    "state:implementation-go; leaving auto-merge disabled until "
                    "implementation review approves it",
                    issue_number,
                    pr_number,
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
            logger.info(
                "Issue #%s: PR #%s only fails auto-merge-policy; enabling auto-merge",
                issue_number,
                pr_number,
            )
            return self._arm_and_wait_for_merge(issue_number, pr_number, acquired_slot)

        fix_result = self._attempt_ci_fixes(issue_number, pr_number, acquired_slot)
        if fix_result is not None and fix_result.success:
            rearmed = self._recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot)
            if rearmed is not None:
                return rearmed
            return fix_result
        if fix_result is not None:
            return fix_result
        return WorkerResult(
            issue_number=issue_number,
            success=False,
            pr_number=pr_number,
            error=f"CI fix failed after {self.options.max_fix_iterations} attempt(s)",
        )

    def _drive_issue(self, issue_number: int, pr_number: int, slot_id: int) -> WorkerResult:
        """Drive a single issue's PR toward green CI.

        Args:
            issue_number: GitHub issue number.
            pr_number: Pre-discovered open PR number for this issue.
            slot_id: Worker slot ID for status tracking.

        Returns:
            WorkerResult indicating success or failure.

        """
        with self.status_tracker.slot() as acquired_slot:
            if acquired_slot is None:
                return WorkerResult(
                    issue_number=issue_number, success=False, error="Failed to acquire worker slot"
                )

            try:
                armed_result = self._check_arming_on_drive_start(issue_number, pr_number)
                if armed_result is not None:
                    return armed_result

                if self.options.enable_mechanical_rebase and not self.options.dry_run:
                    self._attempt_mechanical_rebase(issue_number, pr_number, acquired_slot)

                self.status_tracker.update_slot(
                    acquired_slot, f"{pr_ref(pr_number)}: fetching checks"
                )

                poll_result = self._poll_ci_until_concluded(
                    issue_number, pr_number, acquired_slot, self.options.poll_max_wait
                )
                if poll_result is None:
                    return WorkerResult(
                        issue_number=issue_number, success=True, pr_number=pr_number
                    )
                _checks, required_checks = poll_result

                all_green = all(
                    c.get("conclusion") in ("success", "skipped", "neutral")
                    for c in required_checks
                )
                if all_green:
                    return self._handle_green_pr(issue_number, pr_number, acquired_slot)
                return self._handle_failing_pr(
                    issue_number, pr_number, acquired_slot, required_checks
                )

            except Exception as e:
                logger.error("Issue #%s: unexpected error: %s", issue_number, e)
                return WorkerResult(issue_number=issue_number, success=False, error=str(e)[:200])

    def _attempt_mechanical_rebase(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
    ) -> bool:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator.attempt_mechanical_rebase(
            issue_number, pr_number, acquired_slot
        )

    def _run_advise(self, issue_number: int) -> str:
        """Pull prior learnings from ProjectMnemosyne before the CI fix loop.

        Stage 3's advise-first step. Runs under ``AGENT_ADVISE`` (its own cheap,
        read-only session), gated by ``enable_advise``; the findings are
        prepended to the CI fix-session prompt. Delegates the Mnemosyne setup +
        prompt build to the shared :mod:`advise_runner`; any failure degrades to
        a skip marker so the drive never aborts over missing advice.
        """
        issue_data = gh_issue_json(issue_number)
        issue_title = issue_data.get("title", f"Issue #{issue_number}")
        issue_body = issue_data.get("body", "")

        def _invoke(prompt: str) -> str:
            if uses_direct_agent_runner(self.options.agent):
                result = run_agent_session(
                    agent=self.options.agent,
                    prompt=prompt,
                    cwd=self.repo_root,
                    timeout=self.options.advise_timeout,
                    model=direct_agent_model(
                        self.options.agent,
                        "HEPH_ADVISE_MODEL",
                        codex_default=codex_advise_model(),
                    ),
                    sandbox="read-only",
                )
                return (result.stdout or "").strip()
            repo_slug = get_repo_slug(self.repo_root)
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_ADVISE,
                prompt=prompt,
                model=advise_model(),
                cwd=self.repo_root,
                timeout=self.options.advise_timeout,
                output_format="text",
                allowed_tools="Read,Glob,Grep,Bash",
            )
            return (stdout or "").strip()

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=_invoke,
            build_prompt=get_advise_prompt_builder(self.options.agent),
        )

    def _recheck_and_arm_after_fix(
        self, issue_number: int, pr_number: int, acquired_slot: int, *, resolve_dirty: bool = True
    ) -> WorkerResult | None:
        """After a CI fix is pushed, re-poll checks and arm if now green.

        The fix push re-triggers CI. Historically ``_attempt_ci_fixes`` returned
        success the instant a fix landed and never came back to arm the PR, so a
        now-green PR sat ``NOT armed`` forever (observed: ProjectHermes #645,
        which ended ``CLEAN`` but un-armed). This re-enters the
        check→arm→wait flow ONCE.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            acquired_slot: Worker slot ID for status tracking.
            resolve_dirty: When True (the default, used by the primary fix paths)
                an armed PR that goes ``DIRTY`` while we wait is routed to
                ``_resolve_dirty_pr`` (#1347). ``_resolve_dirty_pr`` calls this
                method back after a rebase/agent fix; those callbacks pass
                ``resolve_dirty=False`` so a still-DIRTY PR does NOT re-dispatch
                into ``_resolve_dirty_pr`` — breaking the
                resolve→recheck→resolve recursion at depth one.

        Returns:
            A terminal ``WorkerResult`` if the PR armed (and we waited on it), or
            ``None`` if CI is still pending / not green yet — in which case the
            caller keeps the fix's success result and a later run arms it.

        """
        if self.options.dry_run:
            return None

        # Bounded poll for the freshly-pushed run to conclude. Reuse the same
        # backoff/cap pattern as the main poll loop.
        max_wait = self.options.poll_max_wait
        elapsed = 0
        attempt = 0
        while True:
            checks = gh_pr_checks(pr_number, dry_run=self.options.dry_run)
            required = [c for c in checks if c.get("required", False)] or checks
            if required and all(c.get("status") == "completed" for c in required):
                break
            sleep_secs = min(2**attempt, 60)
            if elapsed + sleep_secs > max_wait:
                logger.info(
                    "Issue #%s: post-fix CI still pending after %ss; leaving for next run",
                    issue_number,
                    elapsed,
                )
                return None
            self.status_tracker.update_slot(
                acquired_slot,
                f"{issue_ref(issue_number)}: awaiting post-fix CI ({elapsed}s)",
            )
            time.sleep(sleep_secs)
            elapsed += sleep_secs
            attempt += 1

        required = [c for c in checks if c.get("required", False)] or checks
        if not all(c.get("conclusion") in ("success", "skipped", "neutral") for c in required):
            # Still red after the fix — let the normal failure handling stand.
            return None

        if not self._pr_has_implementation_go(pr_number):
            logger.info(
                "Issue #%s: PR #%s is green after fix but lacks state:implementation-go; "
                "leaving auto-merge disabled until implementation review approves it",
                issue_number,
                pr_number,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        self.status_tracker.update_slot(
            acquired_slot, f"{pr_ref(pr_number)}: enabling auto-merge (post-fix)"
        )
        merge_ok = self._enable_auto_merge(
            pr_number, is_bot_pr=self._is_bot_pr_mode(issue_number, pr_number)
        )
        if not merge_ok:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                pr_number=pr_number,
                error=f"auto-merge failed for PR {pr_ref(pr_number)}",
            )
        gh_state = self._gh_pr_state(pr_number)
        pr_head_sha = (gh_state or {}).get("headRefOid", "") or ""
        pr_head_branch = self._get_pr_branch(pr_number)
        self._arm_drive_green(pr_number, pr_head_branch, pr_head_sha)
        outcome = self._wait_for_pr_terminal(issue_number, pr_number)
        # An armed PR can go DIRTY (merge conflict) after the fix push. Mirror
        # the primary arm path (_arm_and_wait_for_merge) and route it to the
        # conflict resolver instead of silently reporting success (#1347).
        # ``resolve_dirty`` gates this so the resolver's own callback into this
        # method cannot re-trigger resolution and recurse indefinitely.
        if resolve_dirty and outcome == "DIRTY":
            return self._resolve_dirty_pr(issue_number, pr_number, acquired_slot)
        return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

    def _resolve_dirty_pr(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Resolve an armed-but-DIRTY (merge-conflict) PR (#838 follow-up).

        ``_wait_for_pr_terminal`` returns ``"DIRTY"`` for an armed PR with a
        merge conflict — it can never merge while armed, and waiting out the
        1800s timeout makes no progress. First try the cheap mechanical rebase;
        if that lands cleanly the push re-triggers CI and we re-arm. If the
        rebase still conflicts, hand it to the agent with explicit
        conflict-resolution instructions (the normal fix path only fires on
        failing *checks*, and a conflicted PR has none — so the agent would
        otherwise get no guidance).

        Returns a terminal ``WorkerResult``.
        """
        if self.options.dry_run:
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        # 1. Cheap path: mechanical rebase. Returns True only on a clean
        #    rebase+push, in which case re-poll and arm the rebased head.
        if self._attempt_mechanical_rebase(issue_number, pr_number, acquired_slot):
            rearmed = self._recheck_and_arm_after_fix(
                issue_number, pr_number, acquired_slot, resolve_dirty=False
            )
            if rearmed is not None:
                return rearmed
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        # 2. Rebase still conflicts → agent resolves the conflict explicitly.
        base_branch = "main"
        try:
            result = _gh_call(["pr", "view", str(pr_number), "--json", "baseRefName"], check=False)
            base_branch = dict(json.loads(result.stdout or "{}")).get("baseRefName") or "main"
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.debug(
                "Failed to determine PR base branch for #%s; defaulting to 'main': %s",
                pr_number,
                exc,
            )
        conflict_context = (
            f"This PR has a MERGE CONFLICT with `origin/{base_branch}` "
            f"(mergeStateStatus=DIRTY) — it cannot merge until the conflict is "
            f"resolved. Rebase the PR head branch onto `origin/{base_branch}` and "
            f"resolve every conflict, keeping both the PR's intent and the latest "
            f"base changes. Then commit the resolution (signed). There may be NO "
            f"failing CI checks — the conflict itself is the blocker."
        )
        fix_result = self._attempt_ci_fixes(
            issue_number, pr_number, acquired_slot, extra_context=conflict_context
        )
        if fix_result is not None and fix_result.success:
            rearmed = self._recheck_and_arm_after_fix(
                issue_number, pr_number, acquired_slot, resolve_dirty=False
            )
            return rearmed if rearmed is not None else fix_result
        return WorkerResult(
            issue_number=issue_number,
            success=False,
            pr_number=pr_number,
            error=f"PR {pr_ref(pr_number)} has an unresolved merge conflict",
        )

    def _resolve_blocked_pr(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Address unresolved review threads on a green-but-BLOCKED PR (#1348).

        ``_wait_for_pr_terminal`` returns ``"BLOCKED"`` when CI is green but
        branch protection still gates the merge. With
        ``required_review_thread_resolution: true`` the most common cause is
        unresolved review threads: the PR is armed (auto-merge enabled) but can
        never merge while a thread stays open. The old BLOCKED branch yielded
        success without ever addressing the threads, so the same
        "Found N unresolved thread(s)" recurred every loop and the PR sat
        armed-but-unmergeable forever.

        This dispatches the existing address-review engine — the same
        :func:`run_address_fix_session` → push → :func:`resolve_addressed_threads`
        sequence the fresh-implementation loop uses (#28/#1083). A progress
        guard bounds the work: each attempt snapshots the unresolved-thread set,
        and if an address pass does not SHRINK that set (no real progress, e.g.
        a thread the agent cannot satisfy) we stop rather than spin. Attempts
        are capped at ``_BLOCKED_ADDRESS_MAX_ATTEMPTS`` so an unsatisfiable
        thread never loops forever. When there are no unresolved threads, the
        BLOCK is from something else and we keep the armed yield (nothing to do).

        Returns a terminal ``WorkerResult``; the PR is left armed on any
        non-progress / failure path so a later run (or a reviewer) can finish it.
        """
        armed_yield = WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        if self.options.dry_run:
            return armed_yield

        threads = self._list_unresolved_threads_safe(pr_number)
        if not threads:
            # BLOCKED but no unresolved threads — gated by something else
            # (e.g. a still-pending required check or a missing approval).
            # Leave it armed; this path made no false promise of progress.
            return armed_yield

        addressed_any = False
        for attempt in range(1, _BLOCKED_ADDRESS_MAX_ATTEMPTS + 1):
            prior_ids = {t["id"] for t in threads if t.get("id")}
            self.status_tracker.update_slot(
                acquired_slot,
                f"{pr_ref(pr_number)}: addressing review threads [A{attempt}]",
            )
            progressed = self._address_threads_once(issue_number, pr_number, threads)
            addressed_any = addressed_any or progressed

            threads = self._list_unresolved_threads_safe(pr_number)
            remaining_ids = {t["id"] for t in threads if t.get("id")}
            if not remaining_ids:
                # Everything resolved — re-enter check→arm→wait so the now
                # unblocked PR can proceed to merge.
                break
            # PROGRESS GUARD (#1348): if the unresolved set did not SHRINK,
            # this attempt made no headway. Do not loop again — a thread the
            # agent cannot satisfy would otherwise spin forever.
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

        # Threads were addressed + pushed; re-enter the check→arm→wait flow so
        # the now-resolved PR can move toward merge (mirrors the CI-fix path).
        rearmed = self._recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot)
        return rearmed if rearmed is not None else armed_yield

    def _address_threads_once(
        self, issue_number: int, pr_number: int, threads: list[dict[str, Any]]
    ) -> bool:
        """Run one address-review fix session over *threads*, then push + resolve.

        Mirrors the implementer loop's in-loop address step
        (:meth:`ReviewPhase._run_address_review_step`): syncs the PR worktree,
        runs :func:`run_address_fix_session`, pushes the resulting commit via
        the shared CI-fix push contract (head-advancement + lease), then resolves
        only the threads the agent explicitly reported as addressed (with the
        hallucination guard inside :func:`resolve_addressed_threads`).

        Returns ``True`` iff a real commit was pushed (the agent made progress).
        """
        worktree_path = self._get_worktree_path(issue_number, pr_number)
        pr_head_branch = self._get_pr_branch(pr_number)
        pre_agent_sha = self._sync_worktree_and_snapshot_sha(
            issue_number, worktree_path, pr_head_branch
        )
        if pre_agent_sha is None:
            return False

        log_file = log_file_path(self.state_dir, "address-review-blocked", issue_number)
        try:
            fix_result = run_address_fix_session(
                issue_number=issue_number,
                pr_number=pr_number,
                worktree_path=worktree_path,
                threads=threads,
                agent=self.options.agent,
                repo_root=self.repo_root,
                parse_fn=_parse_addressed_block,
                log_file=log_file,
                dry_run=self.options.dry_run,
                timeout=self.options.agent_timeout,
                advise_timeout=self.options.advise_timeout,
            )
        except RuntimeError as exc:
            logger.warning(
                "Issue #%s: address-review session failed for PR #%s: %s",
                issue_number,
                pr_number,
                exc,
            )
            return False

        # Gate on a REAL pushed commit, exactly like the CI-fix path: the
        # agent's self-reported ``addressed`` list is untrusted, so only a
        # head-advancing, pushable worktree counts as progress.
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
            dry_run=self.options.dry_run,
        )
        return True

    def _attempt_ci_fixes(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        extra_context: str = "",
    ) -> WorkerResult | None:
        """Attempt CI fix iterations for a failing PR.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            acquired_slot: Worker slot ID for status tracking.
            extra_context: Optional text prepended to the CI failure logs in the
                fix-session prompt — e.g. merge-conflict resolution instructions
                for a DIRTY PR, where ``_get_failing_ci_logs`` alone is empty.

        Returns:
            WorkerResult on success or dry-run, None if all iterations failed.

        """
        # Advise-first (#30): pull prior learnings once before the fix loop, so
        # we only spend an advise call on PRs that actually need fixing. Fed
        # into every fix-session prompt below. Skipped for bot-PR work items
        # (#848): the issue number is synthetic (equals the PR number) so
        # ``gh issue view`` would 404; there is also no human-authored issue
        # body that would meaningfully steer the advise prompt.
        # #1587: a prior drive-green pass may have already pushed a CI fix whose
        # CI simply had not concluded when that pass gave up ("post-fix CI still
        # pending ... leaving for next run"). Re-dispatching a full CI-fix agent
        # against the SAME tip just re-derives "the fix is already in place" after
        # a multi-minute, multi-turn session. If the PR head is unchanged since
        # the last fix this driver recorded, skip the agent and let the
        # recheck/arm poll wait for the pending CI instead.
        if not self.options.dry_run and self._ci_fix_already_pushed_for_current_head(
            issue_number, pr_number
        ):
            logger.info(
                "Issue #%s: PR #%s head unchanged since the last CI fix this driver pushed; "
                "skipping redundant CI-fix agent and awaiting pending CI",
                issue_number,
                pr_number,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        advise_findings = ""
        if self.options.enable_advise and not self._is_bot_pr_mode(issue_number, pr_number):
            self.status_tracker.update_slot(acquired_slot, f"{issue_ref(issue_number)}: advising")
            advise_findings = self._run_advise(issue_number)

        for iteration in range(self.options.max_fix_iterations):
            self.status_tracker.update_slot(
                acquired_slot,
                f"{issue_ref(issue_number)}: fetching CI logs (attempt {iteration + 1})",
            )
            ci_logs = self._get_failing_ci_logs(pr_number)
            if extra_context:
                ci_logs = f"{extra_context}\n\n{ci_logs}".strip()
            session_id = self._load_impl_session_id(issue_number)
            worktree_path = self._get_worktree_path(issue_number, pr_number)
            # Resolve the PR's actual head-branch name once per iteration. The
            # CI fix push must target THIS remote ref even if Claude switches
            # branches locally during the session (#832).
            pr_head_branch = self._get_pr_branch(pr_number)

            if self.options.dry_run:
                logger.info(
                    "[dry_run] Would run CI fix session for PR #%s (issue #%s, iteration %s)",
                    pr_number,
                    issue_number,
                    iteration + 1,
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            self.status_tracker.update_slot(
                acquired_slot,
                f"{issue_ref(issue_number)}: running CI fix session (attempt {iteration + 1})",
            )
            fixed = self._run_ci_fix_session(
                issue_number,
                pr_number,
                worktree_path,
                ci_logs,
                session_id,
                advise_findings,
                pr_head_branch=pr_head_branch,
            )
            if fixed:
                logger.info(
                    "Issue #%s: CI fix applied successfully (attempt %s)",
                    issue_number,
                    iteration + 1,
                )
                # #1587: record the pushed head SHA so a later drive-green pass can
                # detect "fix already pushed, CI still pending" and skip a
                # redundant agent re-run.
                self._record_ci_fix_head(pr_number)
                # Acknowledge automated review comments the fix addressed by
                # replying + resolving the bot threads (human threads untouched).
                self._reply_and_resolve_bot_threads(pr_number)
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            logger.warning("Issue #%s: CI fix attempt %s failed", issue_number, iteration + 1)

        return None

    def _ci_fix_marker_path(self, pr_number: int) -> Path:
        """Path of the marker recording the head SHA of this PR's last CI fix (#1587)."""
        return self.state_dir / f"last-ci-fix-{pr_number}.json"

    def _record_ci_fix_head(self, pr_number: int) -> None:
        """Persist the current PR head SHA after a successful CI fix push (#1587)."""
        gh_state = self._gh_pr_state(pr_number) or {}
        head_sha = str(gh_state.get("headRefOid") or "")
        if not head_sha:
            return
        try:
            write_secure(
                self._ci_fix_marker_path(pr_number),
                json.dumps({"pr_number": pr_number, "head_sha": head_sha}) + "\n",
            )
        except OSError as exc:
            logger.warning(
                "Issue: failed to write last-ci-fix marker for PR #%s: %s", pr_number, exc
            )

    def _ci_fix_already_pushed_for_current_head(self, issue_number: int, pr_number: int) -> bool:
        """Return True if the PR head is unchanged since this driver's last CI fix (#1587).

        Reads the ``last-ci-fix-<pr>.json`` marker and compares its recorded head
        SHA to the PR's current ``headRefOid``. When they match, the fix this
        driver already pushed is still the tip — CI is merely pending — so a fresh
        CI-fix agent would only re-derive "already fixed". Any missing marker,
        missing SHA, or lookup failure returns False (do not skip on uncertainty).
        """
        marker = self._ci_fix_marker_path(pr_number)
        if not marker.exists():
            return False
        try:
            recorded = str(dict(json.loads(marker.read_text())).get("head_sha") or "")
        except (OSError, json.JSONDecodeError):
            return False
        if not recorded:
            return False
        gh_state = self._gh_pr_state(pr_number) or {}
        current = str(gh_state.get("headRefOid") or "")
        return bool(current) and current == recorded

    def _validate_pr_open(self, pr_number: int) -> bool:
        """Return True iff ``pr_number`` exists and is in OPEN state.

        Used by direct --prs mode (#918) to filter out typo'd or closed PR
        numbers before worker submission. Mirrors the strategy-2 check in
        ``find_pr_for_issue``.

        Args:
            pr_number: GitHub PR number.

        Returns:
            True if the PR exists and is OPEN, False otherwise.

        """
        try:
            result = _gh_call(
                ["pr", "view", str(pr_number), "--json", "number,state"],
                check=False,
            )
            data = json.loads(result.stdout or "{}")
            return str(data.get("state", "")).upper() == "OPEN"
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.debug("PR #%s validation failed: %s", pr_number, exc)
            return False

    def _get_pr_branch(self, pr_number: int) -> str:
        """Get the head branch name of a PR.

        Args:
            pr_number: GitHub PR number.

        Returns:
            Branch name string.

        """
        try:
            result = _gh_call(
                ["pr", "view", str(pr_number), "--json", "headRefName"],
                check=False,
            )
            data = json.loads(result.stdout or "{}")
            branch: str = data.get("headRefName", f"pr-{pr_number}")
            return branch
        except Exception as e:
            logger.warning("Could not fetch branch for PR #%s: %s", pr_number, e)
            return f"pr-{pr_number}"

    def _get_worktree_path(self, issue_number: int, pr_number: int) -> Path:
        """Resolve the worktree path for a given issue/PR.

        Tries review state first, then creates a new worktree if needed.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.

        Returns:
            Path to the worktree directory.

        """
        data = load_state_file(self.state_dir, "review", issue_number, state_logger=logger)
        if data and data.get("worktree_path"):
            wt = Path(data["worktree_path"])
            if wt.exists():
                return wt

        # Fallback: create a new worktree for the PR head branch
        branch = self._get_pr_branch(pr_number)
        return self.worktree_manager.create_worktree(issue_number, branch)

    def _get_failing_ci_logs(self, pr_number: int) -> str:
        """Delegate to CICheckInspector (extracted #1357)."""
        return self._check_inspector.get_failing_ci_logs(pr_number)

    # ------------------------------------------------------------------
    # Drive-green arming records (#840)
    #
    # Each issue whose PR auto-merge-armed gets a small JSON record at
    # ``state_dir / "drive-green-armed-<issue>.json"`` so the NEXT run can
    # detect "the PR finally merged" and fire ``/learn`` exactly once — even
    # for the N-1 deferred siblings from the #834 shared-PR dedupe. Firing
    # ``/learn`` on auto-merge-armed (the prior behavior) polluted
    # ProjectMnemosyne with lessons from PRs that never actually shipped.
    # ------------------------------------------------------------------

    def _arming_state_path(self, issue_number: int) -> Path:
        return self._arming_store.path(issue_number)

    def _load_arming_state(self, issue_number: int) -> dict[str, Any] | None:
        """Return the parsed arming record for ``issue_number`` or ``None``."""
        return self._arming_store.load(issue_number)

    def _save_arming_state(self, issue_number: int, record: dict[str, Any]) -> None:
        """Persist the arming record. Best-effort; logs and swallows IO errors."""
        self._arming_store.save(issue_number, record)

    def _clear_arming_state(self, issue_number: int) -> None:
        self._arming_store.clear(issue_number)

    @staticmethod
    def _learn_record_terminal(record: dict[str, Any]) -> bool:
        """Return whether a drive-green /learn record should not be retried."""
        if record.get("learn_captured_at") or record.get("learn_succeeded_at"):
            return True
        return str(record.get("learn_status") or "").lower() in {"succeeded", "failed"}

    def _mark_drive_green_learn_result(
        self,
        issue_number: int,
        record: dict[str, Any],
        *,
        succeeded: bool,
    ) -> None:
        """Delegate to PostMergeProcessor (extracted #1357)."""
        self._post_merge.mark_drive_green_learn_result(issue_number, record, succeeded=succeeded)

    def _arm_drive_green(self, pr_number: int, pr_head_branch: str, pr_head_sha: str) -> None:
        """Record arming for every issue that resolved to ``pr_number``.

        Called on the auto-merge-armed success path in the same run. For a
        shared-PR group (#834), this writes one arming record per sibling
        issue so each one gets its own ``/learn`` capture once the PR merges
        in a subsequent run. The canonical issue and all deferred siblings
        share the same ``pr_number`` and ``pr_head_branch`` — they differ
        only in the issue id encoded in the filename.
        """
        siblings = self.shared_pr_issues.get(pr_number, [])
        if not siblings:
            # Defensive: the PR map should always know the issue, but if not
            # we still want SOMETHING to fire /learn on the next run.
            return
        armed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for issue_num in siblings:
            existing = self._load_arming_state(issue_num) or {}
            if self._learn_record_terminal(existing):
                # Already attempted terminally — don't overwrite learn evidence.
                continue
            record = {
                "pr_number": pr_number,
                "pr_head_branch": pr_head_branch,
                "head_sha_at_arming": pr_head_sha,
                "armed_at": armed_at,
                "learn_attempted_at": None,
                "learn_captured_at": None,
                "learn_status": None,
                "learn_succeeded_at": None,
            }
            self._save_arming_state(issue_num, record)
            logger.info(
                "Issue #%s: armed for /learn on merge of PR #%s (head=%s @ %s)",
                issue_num,
                pr_number,
                pr_head_branch,
                pr_head_sha[:8] if pr_head_sha else "?",
            )

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

    def _pr_has_implementation_go(self, pr_number: int) -> bool:
        """Return whether a PR has the implementation-review GO label."""
        try:
            result = _gh_call(
                ["pr", "view", str(pr_number), "--json", "labels"],
                check=False,
            )
            return pr_has_implementation_go_label(dict(json.loads(result.stdout or "{}")))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not fetch PR #%s labels for implementation-GO gate: %s",
                pr_number,
                exc,
            )
            return False

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
        if self.options.dry_run:
            return "TIMEOUT"

        max_wait = read_timeout_env("HEPH_PR_MERGE_MAX_WAIT", 1800)
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

            # Still OPEN (or unknown). If a required check has gone red since
            # arming, stop waiting and let the caller drive a fix.
            failing = self._failing_required_check_names(pr_number)
            fixable_failing = _without_auto_merge_policy(failing)
            if fixable_failing:
                logger.warning(
                    "Issue #%s: PR #%s went red while awaiting merge (failing: %s)",
                    issue_number,
                    pr_number,
                    ", ".join(fixable_failing),
                )
                return "FAILING"

            # An armed PR that is DIRTY/CONFLICTING has a merge conflict with
            # the base branch — it can never merge while armed, so waiting out
            # the full timeout is pointless. Stop and let the caller rebase /
            # hand it to the agent to resolve the conflict.
            merge_status = ((gh_state or {}).get("mergeStateStatus") or "").upper()
            if merge_status in ("DIRTY", "CONFLICTING"):
                logger.warning(
                    "Issue #%s: PR #%s is %s (merge conflict) while armed; needs rebase/resolution",
                    issue_number,
                    pr_number,
                    merge_status,
                )
                return "DIRTY"

            # A BLOCKED PR is gated by branch protection (e.g. required
            # conversation resolution, required human review) rather than by
            # CI checks. Exit early only when we can confirm the block is a
            # branch-protection gate and not just in-flight checks: GitHub
            # also reports BLOCKED while required checks are still running.
            # Guard: no failing AND no pending required checks.
            policy_only_failure = bool(failing) and not fixable_failing
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
            if merge_status == "BLOCKED" and policy_only_failure:
                logger.info(
                    "Issue #%s: PR #%s is BLOCKED only by auto-merge-policy; "
                    "waiting for the policy check to refresh after arming",
                    issue_number,
                    pr_number,
                )

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

            self.status_tracker.update_slot(
                0,
                f"{iref}: PR #{pr_number} awaiting merge ({elapsed}s elapsed)",
            )
            time.sleep(sleep_secs)
            elapsed += sleep_secs
            attempt += 1

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
        # Serialize the sweep across the issue-major loop's per-issue ci-driver
        # SUBPROCESSES (#1567). They share one ``state_dir`` and each runs this
        # sweep on startup; without a cross-process lock, two subprocesses both
        # find the same MERGED orphan and both fire ``/learn`` +
        # ``create_worktree`` on the same path → ``fatal: ... already exists``
        # (the in-process WorktreeManager lock can't see another process). Hold
        # the lock across the whole sweep so a second sweeper, on acquiring,
        # re-globs + re-loads records and finds them already terminal/cleared.
        with file_lock(self.state_dir / "orphan-sweep.lock"):
            self._sweep_orphaned_arming_records_locked()

    def _sweep_orphaned_arming_records_locked(self) -> None:
        """Body of :meth:`_sweep_orphaned_arming_records`, run under the lock.

        Globs and processes records inside the cross-process lock so concurrent
        ci-driver subprocesses never double-process the same arming record.
        """
        try:
            records = sorted(self.state_dir.glob("drive-green-armed-*.json"))
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
            record = self._load_arming_state(issue_number)
            if record is None:
                continue
            if self._learn_record_terminal(record):
                continue
            pr_number = record.get("pr_number")
            if not isinstance(pr_number, int):
                logger.info(
                    "Arming sweep: dropping record %s with non-integer pr_number",
                    path.name,
                )
                self._clear_arming_state(issue_number)
                continue
            gh_state = self._gh_pr_state(pr_number)
            if gh_state is None:
                # Unknown state — leave alone; the per-issue path or the
                # next sweep can retry.
                continue
            state = (gh_state.get("state") or "").upper()
            if state == "MERGED":
                logger.info(
                    "Arming sweep: issue #%s / PR #%s MERGED; firing /learn",
                    issue_number,
                    pr_number,
                )
                learn_succeeded = self._run_drive_green_learnings(issue_number, pr_number)
                self._run_drive_green_compact(issue_number, pr_number)
                self._mark_drive_green_learn_result(
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
                self._clear_arming_state(issue_number)
            # OPEN: leave for the per-issue path / next sweep.

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
        record = self._load_arming_state(issue_number)
        if record is None:
            return None
        if self._learn_record_terminal(record):
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
            # Treat unknown state as "still in flight" — don't redo the drive.
            # The next run will retry the check.
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        state = (gh_state.get("state") or "").upper()
        current_sha = gh_state.get("headRefOid") or ""

        if state == "MERGED":
            logger.info(
                "Issue #%s: PR #%s detected as MERGED; capturing /learn",
                issue_number,
                pr_number,
            )
            self.status_tracker.update_slot(
                0, f"{issue_ref(issue_number)}: capturing post-merge /learn"
            )
            learn_succeeded = self._run_drive_green_learnings(issue_number, pr_number)
            self._run_drive_green_compact(issue_number, pr_number)
            self._mark_drive_green_learn_result(
                issue_number,
                record,
                succeeded=learn_succeeded,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

        if state == "CLOSED":
            # PR closed without merging. Lessons are not load-bearing if
            # nothing shipped. Drop the record and fall through so the
            # normal flow can decide what to do (likely "no open PR").
            logger.info(
                "Issue #%s: PR #%s was CLOSED without merging; dropping arming record",
                issue_number,
                pr_number,
            )
            self._clear_arming_state(issue_number)
            return None

        # OPEN
        armed_sha = record.get("head_sha_at_arming") or ""
        if current_sha and armed_sha and current_sha != armed_sha:
            # The PR was force-pushed (or rebased) after arming. The arming
            # is stale — re-enter the drive so it can re-arm at the new tip.
            logger.info(
                "Issue #%s: PR #%s head advanced from %s to %s since arming; re-entering drive",
                issue_number,
                pr_number,
                armed_sha[:8],
                current_sha[:8],
            )
            self._clear_arming_state(issue_number)
            return None

        # Still in flight at the same arming SHA — auto-merge is presumably
        # still waiting on CI / branch protection. Block until it finishes
        # (#838) rather than returning success on a PR that may still go red.
        logger.info(
            "Issue #%s: PR #%s still OPEN at the armed SHA; waiting for merge",
            issue_number,
            pr_number,
        )
        outcome = self._wait_for_pr_terminal(issue_number, pr_number)
        if outcome == "MERGED":
            # Fire the post-merge /learn exactly once, mirroring the MERGED
            # branch above so we don't lose the capture by exiting early.
            self.status_tracker.update_slot(
                0, f"{issue_ref(issue_number)}: capturing post-merge /learn"
            )
            learn_succeeded = self._run_drive_green_learnings(issue_number, pr_number)
            self._run_drive_green_compact(issue_number, pr_number)
            self._mark_drive_green_learn_result(
                issue_number,
                record,
                succeeded=learn_succeeded,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        if outcome == "CLOSED":
            self._clear_arming_state(issue_number)
            return None
        if outcome in ("FAILING", "DIRTY"):
            # PR went red (FAILING) or now conflicts (DIRTY) after arming —
            # clear the stale arming and fall through so the normal drive path
            # re-arms and routes to the fix / _resolve_dirty_pr handling.
            self._clear_arming_state(issue_number)
            return None
        # TIMEOUT / BLOCKED — still pending; leave armed for a later pass to resolve.
        return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Load the agent session ID from the implementer's saved state.

        Thin wrapper around :func:`._review_utils.load_impl_session_id`, kept
        as a method so existing patch-by-method test seams hold.

        Args:
            issue_number: GitHub issue number.

        Returns:
            Session ID string, or None if not found.

        """
        return load_impl_session_id(self.state_dir, issue_number, self.options.agent)

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

    @staticmethod
    def _is_bot_author(login: str) -> bool:
        """Return True for automated review authors (GitHub App / bot accounts).

        GitHub App authors carry a ``[bot]`` suffix on their login
        (``github-actions[bot]``, ``coderabbitai[bot]``, ``dependabot[bot]``).
        Human review threads are never auto-resolved — only the bot's own reply
        thread is closed after the CI fix addresses it.
        """
        return login.endswith("[bot]")

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

    def _failing_required_check_names(self, pr_number: int) -> list[str]:
        """Delegate to CICheckInspector (extracted #1357)."""
        return self._check_inspector.failing_required_check_names(pr_number)

    def _tracked_worktree_changes(self, worktree_path: Path, issue_number: int) -> list[str]:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator._tracked_worktree_changes(worktree_path, issue_number)

    def _pending_required_check_names(self, pr_number: int) -> list[str]:
        """Delegate to CICheckInspector (extracted #1357)."""
        return self._check_inspector.pending_required_check_names(pr_number)

    def _force_engagement_prompt(
        self,
        *,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        pr_head_branch: str,
        failing_check_names: list[str],
        review_threads_block: str,
        dirty_tracked_changes: list[str] | None = None,
    ) -> str:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator.force_engagement_prompt(
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            pr_head_branch=pr_head_branch,
            failing_check_names=failing_check_names,
            review_threads_block=review_threads_block,
            dirty_tracked_changes=dirty_tracked_changes,
        )

    def _record_repeated_no_commit(
        self,
        *,
        issue_number: int,
        pr_number: int,
        pr_head_branch: str,
        failing_check_names: list[str],
    ) -> None:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        self._fix_orchestrator.record_repeated_no_commit(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            failing_check_names=failing_check_names,
        )

    def _invoke_agent_session(
        self,
        *,
        prompt: str,
        session_id: str | None,
        worktree_path: Path,
        issue_number: int,
        pr_number: int,
    ) -> subprocess.CompletedProcess[str]:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator.invoke_agent_session(
            prompt=prompt,
            session_id=session_id,
            worktree_path=worktree_path,
            issue_number=issue_number,
            pr_number=pr_number,
        )

    def _push_ci_fix(
        self,
        *,
        worktree_path: Path,
        pre_agent_sha: str,
        issue_number: int,
        pr_number: int,
        pr_head_branch: str,
        session_id: str | None,
    ) -> bool:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator.push_ci_fix(
            worktree_path=worktree_path,
            pre_agent_sha=pre_agent_sha,
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            session_id=session_id,
        )

    def _retry_no_commit_once(
        self,
        *,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        pr_head_branch: str,
        pre_agent_sha: str,
        session_id: str | None,
        max_retries: int = 2,
    ) -> bool:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator.retry_no_commit_once(
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            pr_head_branch=pr_head_branch,
            pre_agent_sha=pre_agent_sha,
            session_id=session_id,
            max_retries=max_retries,
        )

    def _head_advanced(
        self,
        worktree_path: Path,
        pre_agent_sha: str,
        issue_number: int,
    ) -> bool:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator._head_advanced(worktree_path, pre_agent_sha, issue_number)

    def _git_stdout_for_push_guard(
        self,
        worktree_path: Path,
        issue_number: int,
        argv: list[str],
        failure_message: str,
    ) -> str | None:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator._git_stdout_for_push_guard(
            worktree_path, issue_number, argv, failure_message
        )

    def _ci_fix_head_is_pushable(
        self,
        worktree_path: Path,
        issue_number: int,
        *,
        base_ref: str = "origin/main",
    ) -> bool:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator._ci_fix_head_is_pushable(
            worktree_path, issue_number, base_ref=base_ref
        )

    def _sync_worktree_and_snapshot_sha(
        self, issue_number: int, worktree_path: Path, pr_head_branch: str
    ) -> str | None:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator.sync_worktree_and_snapshot_sha(
            issue_number, worktree_path, pr_head_branch
        )

    def _build_ci_fix_prompt(
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        ci_logs: str,
        pr_head_branch: str,
        advise_findings: str,
    ) -> str:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator.build_ci_fix_prompt(
            issue_number,
            pr_number,
            worktree_path,
            ci_logs,
            pr_head_branch,
            advise_findings,
        )

    def _run_ci_fix_session(
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        ci_logs: str,
        session_id: str | None,
        advise_findings: str = "",
        *,
        pr_head_branch: str,
    ) -> bool:
        """Delegate to CIFixOrchestrator (extracted #1357)."""
        return self._fix_orchestrator.run_ci_fix_session(
            issue_number,
            pr_number,
            worktree_path,
            ci_logs,
            session_id,
            advise_findings,
            pr_head_branch=pr_head_branch,
        )

    def _enable_auto_merge(self, pr_number: int, is_bot_pr: bool = False) -> bool:
        """Enable auto-merge for the given PR using squash strategy.

        First attempts ``gh pr merge --auto --squash``. This repo is
        squash-only — rebase merges are disabled by branch protection, so the
        primary path MUST use ``--squash``. On failure, if
        ``options.force_merge_on_stall`` is set, falls back to a direct
        squash merge (``gh pr merge --squash --delete-branch``). If both
        strategies fail, logs an ERROR and returns False.

        For bot-authored PRs (Dependabot etc., #848) a green PR left ``NOT
        armed`` accumulates forever and keeps the repo perpetually "not done".
        Those PRs are low-risk dependency bumps, so before giving up we make a
        strategy-agnostic ``gh pr merge --auto`` attempt (no ``--squash``),
        letting whatever merge method the repo actually allows take effect.

        Args:
            pr_number: GitHub PR number.
            is_bot_pr: True when this is a bot-authored PR, enabling the
                strategy-agnostic arming retry described above.

        Returns:
            True if auto-merge was enabled (or fallback merge succeeded),
            False if both strategies failed.

        """
        try:
            _gh_call(["pr", "merge", str(pr_number), "--auto", "--squash"])
            logger.info("Enabled auto-merge for PR #%s", pr_number)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(
                "Could not enable auto-merge (--squash) for PR #%s: %s; "
                "will attempt squash-merge fallback if force_merge_on_stall is set",
                pr_number,
                e,
            )

        # Bot PRs: try arming without forcing a strategy before stronger
        # fallbacks. A repo that disallows squash (rebase/merge-only) would
        # otherwise strand every Dependabot PR as NOT armed.
        if is_bot_pr:
            try:
                _gh_call(["pr", "merge", str(pr_number), "--auto"])
                logger.info("Enabled auto-merge (strategy-agnostic) for bot PR #%s", pr_number)
                return True
            except subprocess.CalledProcessError as bot_err:
                logger.warning(
                    "Could not enable strategy-agnostic auto-merge for bot PR #%s: %s",
                    pr_number,
                    bot_err,
                )

        if not self.options.force_merge_on_stall:
            logger.error(
                "PR #%s: auto-merge failed and force_merge_on_stall is not set; "
                "skipping squash-merge fallback",
                pr_number,
            )
            return False

        # Fallback: direct squash merge
        try:
            _gh_call(["pr", "merge", str(pr_number), "--squash", "--delete-branch"])
            logger.info("Squash-merged PR #%s via fallback", pr_number)
            return True
        except subprocess.CalledProcessError as fallback_err:
            logger.error(
                "PR #%s: both auto-merge and squash-merge fallback failed: %s",
                pr_number,
                fallback_err,
            )
            return False

    def _run_drive_green_learnings(self, issue_number: int, pr_number: int) -> bool:
        """Delegate to PostMergeProcessor (extracted #1357)."""
        return self._post_merge.run_drive_green_learnings(issue_number, pr_number)

    def _run_drive_green_compact(self, issue_number: int, pr_number: int) -> bool:
        """Delegate to PostMergeProcessor (extracted #1357)."""
        return self._post_merge.run_drive_green_compact(issue_number, pr_number)

    def _parse_json_block(self, text: str) -> dict[str, Any]:
        """Extract and parse the first JSON block or raw JSON object.

        Args:
            text: Input text that may contain a JSON block.

        Returns:
            Parsed dictionary, or empty dict if no valid JSON found.

        """
        return parse_json_block(
            text,
            default={},
            parse_error_default={},
            raw_json_fallback=True,
            use_last_block=False,
        )

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print a summary of CI drive results.

        Args:
            results: Mapping of issue number to WorkerResult.

        """
        print_worker_summary("CI Driver Summary", results)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


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
    """Build the argparse parser for the CI-driver CLI."""
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


def _evaluate_run_result(
    results: dict[int, WorkerResult],
    open_prs_remaining: list[dict[str, Any]],
    *,
    issues: list[int],
    as_json: bool,
) -> int:
    """Map a completed drive into a process exit code (#838).

    The repo is "done" when no PR still needs human action. After the
    wait-for-merge change the driver blocks on armed PRs until they merge or go
    red, so a PR that is STILL armed-and-pending at exit is just slow CI we
    already waited on — it must not red-flag the repo. We partition the
    remaining open PRs into ``armed_pending`` (auto-merge armed, merging on its
    own) vs ``needs_action`` (un-armed, genuinely stuck) and only fail on the
    latter (or on per-issue failures).

    An armed PR is only "merging on its own" while its merge-state is benign
    (CLEAN, or BLOCKED/UNSTABLE on in-flight CI). An armed PR whose merge-state
    is ``DIRTY`` / ``CONFLICTING`` has a permanent merge conflict with its base
    — it can NEVER merge while armed, so reporting it as "armed and still
    merging" is a false-green (#1328). Such PRs are reclassified OUT of
    ``armed_pending`` and INTO ``needs_action`` so the gate returns rc=1 and the
    JSON status surfaces them. This mirrors ``_wait_for_armed_merge``'s terminal
    handling, which already treats DIRTY/CONFLICTING as a stop-and-resolve case.

    Args:
        results: Per-issue worker results from ``CIDriver.run()``.
        open_prs_remaining: Open PRs left on the repo (each carries
            ``number``, ``autoMergeRequest``, and ``mergeStateStatus``).
        issues: The input issue list (echoed into JSON status).
        as_json: Whether to emit a machine-readable status line.

    Returns:
        ``0`` if clean (possibly with armed-pending PRs still merging),
        ``1`` if any issue failed or any PR needs manual action.

    """
    log = logging.getLogger(__name__)
    raw_failed = {num: result for num, result in results.items() if not result.success}

    # Terminal-state vocabulary shared with ``_wait_for_armed_merge``: a PR in
    # one of these merge-states has a real conflict with its base and can never
    # merge while armed — it needs manual/agent action, not more waiting.
    conflict_states = {"DIRTY", "CONFLICTING"}

    def _is_conflicting(pr: dict[str, Any]) -> bool:
        merge_state = str(pr.get("mergeStateStatus") or "").upper()
        mergeable = str(pr.get("mergeable") or "").upper()
        return merge_state in conflict_states or mergeable == "CONFLICTING"

    def _is_pending_review(pr: dict[str, Any]) -> bool:
        """Return True for an un-armed, non-conflicting PR awaiting review.

        #1576: such a PR is green but un-armed ONLY because it lacks
        ``state:implementation-go`` — the review gate has not approved it yet.
        That is NOT a merge failure (it is the system working as designed), so it
        must not count toward ``needs_action`` / rc=1, or the loop runner tags
        the owning issue ``state:skip`` every loop. A conflicting PR is excluded
        (it is genuinely stuck and stays in ``needs_action``).
        """
        return (
            not pr.get("autoMergeRequest")
            and not _is_conflicting(pr)
            and not pr_has_implementation_go_label(pr)
        )

    # Armed AND merge-state benign → genuinely merging on its own (rc=0). Armed
    # but CONFLICTING → false-green; demote to needs_action below.
    armed_pending = [
        pr.get("number")
        for pr in open_prs_remaining
        if pr.get("autoMergeRequest") and not _is_conflicting(pr)
    ]
    armed_pending_prs = set(armed_pending)
    stale_failed = [
        num
        for num, result in raw_failed.items()
        if result.pr_number is not None and result.pr_number in armed_pending_prs
    ]
    failed = [num for num in raw_failed if num not in stale_failed]
    # #1576: un-armed-because-awaiting-review PRs are neither armed nor stuck.
    pending_review = [pr.get("number") for pr in open_prs_remaining if _is_pending_review(pr)]
    # needs_action = genuinely stuck: un-armed for a reason OTHER than pending
    # review (e.g. conflicting), or armed-but-conflicting. Pending-review PRs are
    # explicitly excluded so they never force rc=1.
    needs_action = [
        pr.get("number")
        for pr in open_prs_remaining
        if (not pr.get("autoMergeRequest") or _is_conflicting(pr)) and not _is_pending_review(pr)
    ]
    if armed_pending:
        log.warning(
            "%s PR(s) armed and still merging (waited; not a failure): %s",
            len(armed_pending),
            armed_pending,
        )
    if stale_failed:
        log.info(
            "%s issue failure(s) correspond to armed pending PRs and are no longer actionable: %s",
            len(stale_failed),
            stale_failed,
        )
    if pending_review:
        log.info(
            "%s open PR(s) awaiting implementation review (not a failure): %s",
            len(pending_review),
            pending_review,
        )
    if failed or needs_action:
        if failed:
            log.error("CI drive failed for %s issue(s): %s", len(failed), failed)
        if needs_action:
            log.error(
                "Repo not done: %s open PR(s) need manual action: %s",
                len(needs_action),
                needs_action,
            )
        if as_json:
            emit_json_status(
                1,
                issues=issues,
                failed=failed,
                needs_action=needs_action,
                armed_pending=armed_pending,
                pending_review=pending_review,
            )
        return 1

    log.info("CI driver complete")
    if as_json:
        emit_json_status(
            0,
            issues=issues,
            failed=[],
            needs_action=[],
            armed_pending=armed_pending,
            pending_review=pending_review,
        )
    return 0


def main() -> int:
    """Execute the CI driver workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt.

    """
    args = _parse_args()
    configure_github_throttle_from_args(args)
    _setup_logging(args.verbose)
    agent = resolve_agent(args.agent)

    log = logging.getLogger(__name__)

    log.info(
        "Starting CI driver for issues: %s, direct PRs: %s",
        args.issues or "<discovery mode>",
        args.prs,
    )

    try:
        options = CIDriverOptions(
            issues=args.issues,
            prs=args.prs,
            agent=agent,
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            enable_advise=not args.no_advise,
            enable_ui=not args.no_ui and not args.json,
            verbose=args.verbose,
            include_bot_prs=args.include_bot_prs,
            include_all_authors=args.include_all_authors,
            enable_mechanical_rebase=args.enable_mechanical_rebase,
            max_fix_iterations=args.max_fix_iterations,
            agent_timeout=(
                args.agent_timeout if args.agent_timeout is not None else DEFAULT_AGENT_TIMEOUT
            ),
            advise_timeout=(
                args.advise_timeout if args.advise_timeout is not None else DEFAULT_AGENT_TIMEOUT
            ),
            learn_timeout=(
                args.learn_timeout if args.learn_timeout is not None else DEFAULT_AGENT_TIMEOUT
            ),
            poll_max_wait=(
                args.poll_max_wait if args.poll_max_wait is not None else DEFAULT_CI_POLL_MAX_WAIT
            ),
        )

        driver = CIDriver(options)
        results = driver.run()
        return _evaluate_run_result(
            results, driver.open_prs_remaining, issues=args.issues, as_json=args.json
        )

    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        if args.json:
            emit_json_status(130, message="interrupted")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
