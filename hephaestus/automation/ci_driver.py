"""CI driver automation: polls CI checks and drives PRs to green.

Provides:
- Parallel CI check polling across multiple issues
- Automatic fix session on red required checks
- Auto-merge enablement when all required checks are green
- Dry-run support with early return before any GitHub write or git push
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    add_agent_argument,
    resolve_agent,
)
from hephaestus.cli.utils import add_dry_run_arg, add_json_arg, emit_json_status

from ._review_utils import add_max_workers_arg, find_pr_for_issue
from .arming_state import ArmingStateStore
from .ci_check_inspector import CICheckInspector
from .ci_fix_orchestrator import CIFixOrchestrator
from .claude_timeouts import (
    ci_poll_max_wait,
)
from .git_utils import (
    get_repo_info,
    get_repo_root,
    pr_ref,
)
from .github_api import (
    _gh_call,
    gh_pr_checks,
)
from .models import CIDriverOptions, WorkerResult
from .post_merge_processor import PostMergeProcessor
from .pr_discovery import PRDiscovery
from .pr_manager import pr_has_implementation_go_label
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)

# Conclusion values that indicate a PR's check rollup is failing in a way
# drive-green can act on. SUCCESS / SKIPPED / NEUTRAL / PENDING are
# explicitly excluded. Shared with loop_runner._count_failing_prs so the
# SKIP gate and the actual work list never drift (#819).
FAILING_CHECK_CONCLUSIONS: frozenset[str] = frozenset({"FAILURE", "CANCELLED", "TIMED_OUT"})


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
        self.state_dir = self.repo_root / "build" / ".issue_implementer"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._arming_store = ArmingStateStore(lambda: self.state_dir)

        self.worktree_manager = WorktreeManager()
        self.status_tracker = StatusTracker(options.max_workers)
        self.lock = threading.Lock()
        self.open_prs_remaining: list[dict[str, Any]] = []
        self.shared_pr_issues: dict[int, list[int]] = {}
        self._viewer_login: str = ""

        # --- SRP collaborators (#1289) ---

        def _spi_setter(m: dict[int, list[int]]) -> None:
            self.shared_pr_issues.clear()
            self.shared_pr_issues.update(m)

        self._pr_discovery = PRDiscovery(
            options=options,
            shared_pr_issues_setter=_spi_setter,
            shared_pr_issues_getter=lambda: self.shared_pr_issues,
        )

        self._pr_discovery._shared_pr_issues_setter = _spi_setter
        self._pr_discovery._viewer_login = self._viewer_login
        self._pr_discovery._enable_auto_merge_fn = lambda pr_number, is_bot_pr=False: (
            self._enable_auto_merge(pr_number, is_bot_pr)
        )
        self._pr_discovery._list_open_prs_remaining_fn = self._list_open_prs_remaining

        self._post_merge = PostMergeProcessor(
            options=options,
            repo_root=self.repo_root,
            get_worktree_path=lambda issue, pr: self._get_worktree_path(issue, pr),
            save_arming_state=self._save_arming_state,
            load_arming_state=self._load_arming_state,
            clear_arming_state=self._clear_arming_state,
            learn_record_terminal=self._learn_record_terminal,
        )
        self._post_merge._shared_pr_issues_getter = lambda pr_number: self.shared_pr_issues.get(
            pr_number, []
        )

        self._ci_check = CICheckInspector(
            options=options,
            get_pr_branch=lambda pr_number: self._get_pr_branch(pr_number),
            get_worktree_path=lambda issue, pr: self._get_worktree_path(issue, pr),
            status_tracker_update_slot=self.status_tracker.update_slot,
        )
        self._ci_check._state_dir = self.state_dir
        self._ci_check._load_arming_state_fn = self._load_arming_state
        self._ci_check._clear_arming_state_fn = self._clear_arming_state
        self._ci_check._learn_record_terminal_fn = self._learn_record_terminal
        self._ci_check._save_arming_state_fn = self._save_arming_state
        self._ci_check._run_drive_green_learnings_fn = self._run_drive_green_learnings
        self._ci_check._run_drive_green_compact_fn = self._run_drive_green_compact
        self._ci_check._mark_drive_green_learn_result_fn = self._mark_drive_green_learn_result

        self._ci_fix = CIFixOrchestrator(
            options=options,
            repo_root=self.repo_root,
            state_dir=self.state_dir,
            status_tracker=self.status_tracker,
        )
        self._ci_fix._get_pr_branch_fn = lambda pr_number: self._get_pr_branch(pr_number)
        self._ci_fix._get_worktree_path_fn = lambda issue, pr: self._get_worktree_path(issue, pr)
        self._ci_fix._is_bot_pr_mode_fn = self._is_bot_pr_mode
        self._ci_fix._pr_has_implementation_go_fn = self._pr_has_implementation_go
        self._ci_fix._enable_auto_merge_fn = lambda pr_number, is_bot_pr=False: (
            self._enable_auto_merge(pr_number, is_bot_pr)
        )
        self._ci_fix._gh_pr_state_fn = self._gh_pr_state
        self._ci_fix._arm_drive_green_fn = self._arm_drive_green
        self._ci_fix._wait_for_pr_terminal_fn = self._wait_for_pr_terminal
        self._ci_fix._reply_and_resolve_bot_threads_fn = self._reply_and_resolve_bot_threads
        self._ci_fix._format_review_threads_block_fn = self._format_review_threads_block
        self._ci_fix._failing_required_check_names_fn = self._failing_required_check_names
        self._ci_fix._tracked_worktree_changes_fn = self._tracked_worktree_changes

    def __setattr__(self, name: str, value: object) -> None:
        """Propagate state_dir changes to collaborators so tests can override it."""
        super().__setattr__(name, value)
        if name == "state_dir":
            for attr in ("_ci_fix", "_ci_check"):
                collab = self.__dict__.get(attr)
                if collab is not None:
                    if attr == "_ci_fix":
                        collab.state_dir = value
                    else:
                        collab._state_dir = value

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
        """Return the list of open PRs left on the repo after the drive (#838).

        A repo is only truly "driven" when there are zero open PRs left. The
        per-issue ``_drive_issue`` loop's notion of success — every issue's
        PR moved to green and/or got auto-merge enabled — does NOT imply the
        repo is clean: PRs that have not yet merged (auto-merge waiting on
        CI), PRs from issues outside the input set, and PRs opened by
        humans/other-automation all leave open work behind.

        Uses ``gh api --paginate`` so the result is the FULL set of open PRs,
        not a capped prefix. A repo with hundreds of dependabot PRs would
        otherwise pass the done-check after looking at only 100 of them.

        Returns:
            One dict per open PR with keys ``number``, ``title``,
            ``headRefName``, and ``autoMergeRequest`` (None or the auto-merge
            metadata blob). Empty list iff the repo is clean.

        """
        try:
            owner, repo = get_repo_info(self.repo_root)
        except RuntimeError as exc:
            logger.error("Could not resolve repo owner/name to list open PRs: %s", exc)
            # Unknown ownership ⇒ treat as not-done so operators investigate.
            return [{"number": -1, "title": "(unknown: cannot resolve repo)"}]

        # ``gh api --paginate`` walks ``Link: rel="next"`` headers and emits
        # a single concatenated JSON array across all pages. ``per_page=100``
        # is GitHub's max page size; we issue the minimum number of calls.
        # We use ``gh api`` directly (not ``gh pr list``) because the latter
        # caps at ``--limit`` even with paginate semantics; gh's REST proxy
        # paginates without an upper bound.
        try:
            result = _gh_call(
                [
                    "api",
                    "--paginate",
                    f"/repos/{owner}/{repo}/pulls?state=open&per_page=100",
                ],
                check=False,
            )
            raw_pulls: list[dict[str, Any]] = json.loads(result.stdout or "[]")
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            # If we cannot determine the open-PR count, the safest default is
            # to assume the repo is NOT done — surface the unknown state as a
            # failure so operators don't walk away on a false-green.
            logger.error("Could not list open PRs to verify repo done-state: %s", exc)
            return [{"number": -1, "title": "(unknown: gh api pulls failed)"}]

        # The REST shape exposes ``head.ref`` and ``auto_merge`` (snake_case);
        # normalise to the gh-CLI shape consumers downstream already use.
        viewer = "" if self.options.include_all_authors else self._resolve_viewer_login()
        normalised: list[dict[str, Any]] = []
        for pr in raw_pulls:
            user = pr.get("user") or {}
            if viewer and user.get("login") != viewer:
                if user.get("login") is None:
                    logger.warning(
                        "PR #%s has no user.login; skipping under author filter (#821)",
                        pr.get("number"),
                        extra={
                            "missing_field": "user.login",
                            "filter": "author",
                            "pr_number": pr.get("number"),
                        },
                    )
                continue  # #821: hide other-author PRs from the done-gate sweep
            labels = pr.get("labels") or []
            normalised.append(
                {
                    "number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "headRefName": (pr.get("head") or {}).get("ref", ""),
                    "autoMergeRequest": pr.get("auto_merge"),
                    "labels": [
                        label.get("name", "") for label in labels if isinstance(label, dict)
                    ],
                    "isBot": user.get("type") == "Bot",
                }
            )
        return normalised

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
        """Delegate to PRDiscovery collaborator (#1289)."""
        login = self._pr_discovery._resolve_viewer_login()
        self._viewer_login = self._pr_discovery._viewer_login
        return login

    def _discover_bot_prs(self) -> dict[int, int]:
        """Delegate to PRDiscovery collaborator (#1289)."""
        return self._pr_discovery._discover_bot_prs()

    def _discover_failing_prs(self) -> dict[int, int]:
        """Delegate to PRDiscovery collaborator (#1289)."""
        return self._pr_discovery._discover_failing_prs()

    def _is_bot_pr_mode(self, issue_number: int, pr_number: int) -> bool:
        """Delegate to PRDiscovery collaborator (#1289)."""
        return self._pr_discovery._is_bot_pr_mode(issue_number, pr_number)

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:
        """Delegate to PRDiscovery collaborator (#1289)."""
        result = self._pr_discovery._discover_prs(issue_numbers)
        # PRDiscovery wrote into shared_pr_issues via the setter; sync viewer login.
        self._viewer_login = self._pr_discovery._viewer_login
        return result

    def _drive_issue(  # noqa: C901  # orchestration: poll loop + required-check classification + CI-fix path
        self, issue_number: int, pr_number: int, slot_id: int
    ) -> WorkerResult:
        """Drive a single issue's PR toward green CI.

        The pr_number is pre-discovered by run() — no Claude agent is ever launched
        for issues that have no open PR.

        Args:
            issue_number: GitHub issue number.
            pr_number: Pre-discovered open PR number for this issue.
            slot_id: Worker slot ID for status tracking.

        Returns:
            WorkerResult indicating success or failure.

        """
        acquired_slot: int | None = self.status_tracker.acquire_slot()
        if acquired_slot is None:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error="Failed to acquire worker slot",
            )

        # Maximum wall-clock seconds to poll for pending CI checks before giving up.
        _ci_poll_max_wait: int = ci_poll_max_wait()

        try:
            # Detected-merge / armed-state short-circuit (#840). If a prior
            # run armed this issue's PR and GitHub now reports it MERGED,
            # capture /learn once and return. If it's still OPEN at the
            # armed SHA, no further drive work is needed. Falls through to
            # the normal drive only when there's no record, the arming is
            # stale, or the PR was abandoned without merging.
            armed_result = self._check_arming_on_drive_start(issue_number, pr_number)
            if armed_result is not None:
                return armed_result

            # 1b. Mechanical rebase first (#871). A PR that is merely behind the
            # base branch is rebased + pushed here with no agent spend; the push
            # re-triggers CI, and the poll loop below evaluates the rebased head.
            # PRs whose rebase hits real conflicts are left untouched and fall
            # through to the agent path (Claude handles the actual conflict).
            if self.options.enable_mechanical_rebase and not self.options.dry_run:
                self._attempt_mechanical_rebase(issue_number, pr_number, acquired_slot)

            self.status_tracker.update_slot(acquired_slot, f"{pr_ref(pr_number)}: fetching checks")

            # 2. Get CI checks — bounded poll loop for pending state.
            # The module docstring advertises "Parallel CI check polling" but the
            # original code returned success=True immediately for pending checks,
            # which meant stalled PRs were silently declared complete.  We now
            # wait up to HEPH_CI_POLL_MAX_WAIT seconds (default 600 s) using
            # exponential backoff before giving up.
            poll_elapsed = 0
            poll_attempt = 0
            checks: list[dict[str, Any]] = []
            required_checks: list[dict[str, Any]] = []
            all_green = False
            failing: list[dict[str, Any]] = []

            while True:
                checks = gh_pr_checks(pr_number, dry_run=self.options.dry_run)
                if not checks:
                    logger.info("Issue #%s: no CI checks found for PR #%s", issue_number, pr_number)
                    return WorkerResult(
                        issue_number=issue_number, success=True, pr_number=pr_number
                    )

                # 3. Classify: required vs non-required
                required_checks = [c for c in checks if c.get("required", False)]
                if not required_checks:
                    # No required checks defined — treat ALL checks as required
                    required_checks = checks

                # 4. Check if all required checks have a definitive conclusion.
                # "queued" / "in_progress" / "waiting" / "requested" are pending states.
                all_concluded = all(c["status"] == "completed" for c in required_checks)

                if all_concluded:
                    # All checks have a conclusion; evaluate pass/fail below.
                    break

                # At least one check is still pending.
                sleep_secs = min(2**poll_attempt, 60)
                if poll_elapsed + sleep_secs > _ci_poll_max_wait:
                    logger.warning(
                        "Issue #%s: CI checks still pending after %ss (limit %ss), "
                        "treating as not yet failing",
                        issue_number,
                        poll_elapsed,
                        _ci_poll_max_wait,
                    )
                    return WorkerResult(
                        issue_number=issue_number, success=True, pr_number=pr_number
                    )

                pref = pr_ref(pr_number)
                self.status_tracker.update_slot(
                    acquired_slot,
                    f"{pref}: waiting for CI checks "
                    f"(attempt {poll_attempt + 1}, {poll_elapsed}s elapsed)",
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

            all_green = all(
                c.get("conclusion") in ("success", "skipped", "neutral") for c in required_checks
            )

            if all_green:
                if not self._pr_has_implementation_go(pr_number):
                    logger.info(
                        "Issue #%s: PR #%s is green but lacks state:implementation-go; "
                        "leaving auto-merge disabled until implementation review approves it",
                        issue_number,
                        pr_number,
                    )
                    return WorkerResult(
                        issue_number=issue_number,
                        success=True,
                        pr_number=pr_number,
                    )

                self.status_tracker.update_slot(
                    acquired_slot, f"{pr_ref(pr_number)}: enabling auto-merge"
                )
                # DRY-RUN GUARD before auto-merge
                if self.options.dry_run:
                    logger.info(
                        "[dry_run] Would enable auto-merge for PR #%s (issue #%s)",
                        pr_number,
                        issue_number,
                    )
                    return WorkerResult(
                        issue_number=issue_number, success=True, pr_number=pr_number
                    )
                merge_ok = self._enable_auto_merge(
                    pr_number, is_bot_pr=self._is_bot_pr_mode(issue_number, pr_number)
                )
                if merge_ok:
                    # PR is green and auto-merge is enabled — but do NOT
                    # capture /learn here (#840). Auto-merge-armed is not
                    # the same as merged: CI flake, branch-protection block,
                    # human cancellation can still keep the PR open. Instead
                    # write an arming record per sibling issue (#834) and
                    # let the NEXT run's _check_arming_on_drive_start fire
                    # /learn once GitHub reports the PR as MERGED.
                    self.status_tracker.update_slot(
                        acquired_slot, f"{pr_ref(pr_number)}: arming for post-merge /learn"
                    )
                    gh_state = self._gh_pr_state(pr_number)
                    pr_head_sha = (gh_state or {}).get("headRefOid", "") or ""
                    pr_head_branch = self._get_pr_branch(pr_number)
                    self._arm_drive_green(pr_number, pr_head_branch, pr_head_sha)

                    # Block until the armed PR actually finishes instead of
                    # returning the instant auto-merge is enabled (#838). If CI
                    # goes red after arming, drop into the fix path; otherwise
                    # MERGED/CLOSED/TIMEOUT are all "nothing more to drive here".
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
                            error=(
                                f"CI fix failed after {self.options.max_fix_iterations} attempt(s)"
                            ),
                        )
                    if outcome == "DIRTY":
                        return self._resolve_dirty_pr(issue_number, pr_number, acquired_slot)
                    if outcome == "BLOCKED":
                        # Branch-protection gate (e.g. unresolved conversations).
                        # Nothing for the bot to fix — leave armed and yield success.
                        return WorkerResult(
                            issue_number=issue_number,
                            success=True,
                            pr_number=pr_number,
                        )
                return WorkerResult(
                    issue_number=issue_number,
                    success=merge_ok,
                    pr_number=pr_number,
                    error=None if merge_ok else f"auto-merge failed for PR {pr_ref(pr_number)}",
                )

            # 5. Some required checks failed
            failing = [c for c in required_checks if c.get("conclusion") == "failure"]
            if not failing:
                # All concluded but none are green and none are "failure" —
                # e.g. all cancelled.  Nothing for us to fix.
                logger.info(
                    "Issue #%s: PR #%s checks concluded with non-green, non-failure conclusions "
                    "(e.g. cancelled)",
                    issue_number,
                    pr_number,
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            # 6. Attempt fix iterations
            fix_result = self._attempt_ci_fixes(issue_number, pr_number, acquired_slot)
            if fix_result is not None and fix_result.success:
                # A fix was pushed, which re-triggers CI. The old code returned
                # success here and walked away, leaving a now-green PR NOT armed
                # (it never re-polled or enabled auto-merge). Re-enter the
                # check→arm→wait flow ONCE so the fixed PR actually arms.
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

        except Exception as e:
            logger.error("Issue #%s: unexpected error: %s", issue_number, e)
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error=str(e)[:200],
            )

        finally:
            self.status_tracker.release_slot(acquired_slot)

    def _attempt_mechanical_rebase(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
    ) -> bool:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._attempt_mechanical_rebase(issue_number, pr_number, acquired_slot)

    def _run_advise(self, issue_number: int) -> str:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._run_advise(issue_number)

    def _recheck_and_arm_after_fix(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult | None:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot)

    def _resolve_dirty_pr(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._resolve_dirty_pr(issue_number, pr_number, acquired_slot)

    def _attempt_ci_fixes(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        extra_context: str = "",
    ) -> WorkerResult | None:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._attempt_ci_fixes(issue_number, pr_number, acquired_slot, extra_context)

    def _find_pr_for_issue(self, issue_number: int) -> int | None:
        """Find the open PR for a single issue.

        Delegates to :func:`_review_utils.find_pr_for_issue` (two-strategy
        branch-name + body search). Sharing the helper with
        :class:`AddressReviewer` and :class:`PRReviewer` keeps strategy
        evolution in one place.

        Args:
            issue_number: GitHub issue number.

        Returns:
            PR number if found, None otherwise.

        """
        return find_pr_for_issue(issue_number)

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
        review_state_file = self.state_dir / f"review-{issue_number}.json"
        if review_state_file.exists():
            try:
                data = json.loads(review_state_file.read_text())
                if data.get("worktree_path"):
                    wt = Path(data["worktree_path"])
                    if wt.exists():
                        return wt
            except Exception as e:
                logger.debug("Could not read review state for issue #%s: %s", issue_number, e)

        # Fallback: create a new worktree for the PR head branch
        branch = self._get_pr_branch(pr_number)
        return self.worktree_manager.create_worktree(issue_number, branch)

    def _get_failing_ci_logs(self, pr_number: int) -> str:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._get_failing_ci_logs(pr_number)

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
        """Delegate to PostMergeProcessor collaborator (#1289)."""
        self._post_merge._mark_drive_green_learn_result(issue_number, record, succeeded=succeeded)

    def _arm_drive_green(self, pr_number: int, pr_head_branch: str, pr_head_sha: str) -> None:
        """Delegate to PostMergeProcessor collaborator (#1289)."""
        self._post_merge._arm_drive_green(pr_number, pr_head_branch, pr_head_sha)

    def _gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._gh_pr_state(pr_number)

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
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._wait_for_pr_terminal(issue_number, pr_number)

    def _sweep_orphaned_arming_records(self) -> None:
        """Delegate to CICheckInspector collaborator (#1289)."""
        self._ci_check._sweep_orphaned_arming_records()

    def _check_arming_on_drive_start(
        self, issue_number: int, pr_number: int
    ) -> WorkerResult | None:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._check_arming_on_drive_start(issue_number, pr_number)

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._load_impl_session_id(issue_number)

    def _list_unresolved_threads_safe(self, pr_number: int) -> list[dict[str, Any]]:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._list_unresolved_threads_safe(pr_number)

    def _format_review_threads_block(self, pr_number: int) -> str:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._format_review_threads_block(pr_number)

    @staticmethod
    def _is_bot_author(login: str) -> bool:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return CICheckInspector._is_bot_author(login)

    def _reply_and_resolve_bot_threads(self, pr_number: int) -> int:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._reply_and_resolve_bot_threads(pr_number)

    def _failing_required_check_names(self, pr_number: int) -> list[str]:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._failing_required_check_names(pr_number)

    def _tracked_worktree_changes(self, worktree_path: Path, issue_number: int) -> list[str]:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._tracked_worktree_changes(worktree_path, issue_number)

    def _pending_required_check_names(self, pr_number: int) -> list[str]:
        """Delegate to CICheckInspector collaborator (#1289)."""
        return self._ci_check._pending_required_check_names(pr_number)

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
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._force_engagement_prompt(
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
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._record_repeated_no_commit(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            failing_check_names=failing_check_names,
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
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._retry_no_commit_once(
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
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._head_advanced(worktree_path, pre_agent_sha, issue_number)

    def _git_stdout_for_push_guard(
        self,
        worktree_path: Path,
        issue_number: int,
        argv: list[str],
        failure_message: str,
    ) -> str | None:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._git_stdout_for_push_guard(
            worktree_path, issue_number, argv, failure_message
        )

    def _ci_fix_head_is_pushable(
        self,
        worktree_path: Path,
        issue_number: int,
        *,
        base_ref: str = "origin/main",
    ) -> bool:
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._ci_fix_head_is_pushable(worktree_path, issue_number, base_ref=base_ref)

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
        """Delegate to CIFixOrchestrator collaborator (#1289)."""
        return self._ci_fix._run_ci_fix_session(
            issue_number,
            pr_number,
            worktree_path,
            ci_logs,
            session_id,
            advise_findings,
            pr_head_branch=pr_head_branch,
        )

    def _enable_auto_merge(self, pr_number: int, is_bot_pr: bool = False) -> bool:
        """Delegate to PostMergeProcessor collaborator (#1289)."""
        return self._post_merge._enable_auto_merge(pr_number, is_bot_pr)

    def _run_drive_green_learnings(self, issue_number: int, pr_number: int) -> bool:
        """Delegate to PostMergeProcessor collaborator (#1289)."""
        return self._post_merge._run_drive_green_learnings(issue_number, pr_number)

    def _run_drive_green_compact(self, issue_number: int, pr_number: int) -> bool:
        """Delegate to PostMergeProcessor collaborator (#1289)."""
        return self._post_merge._run_drive_green_compact(issue_number, pr_number)

    def _parse_json_block(self, text: str) -> dict[str, Any]:
        """Extract and parse the first JSON block from a text string.

        Args:
            text: Input text that may contain a JSON block.

        Returns:
            Parsed dictionary, or empty dict if no valid JSON found.

        """
        import re

        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            with contextlib.suppress(json.JSONDecodeError):
                return dict(json.loads(match.group(1)))

        # Try raw JSON
        with contextlib.suppress(json.JSONDecodeError):
            return dict(json.loads(text))

        return {}

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print a summary of CI drive results.

        Args:
            results: Mapping of issue number to WorkerResult.

        """
        total = len(results)
        successful = sum(1 for r in results.values() if r.success)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("CI Driver Summary")
        logger.info("=" * 60)
        logger.info("Total issues: %s", total)
        logger.info("Successful: %s", successful)
        logger.info("Failed: %s", failed)

        if failed > 0:
            logger.info("Failed issues:")
            for issue_num, result in results.items():
                if not result.success:
                    logger.info("  #%s: %s", issue_num, result.error)


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
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the CI-driver CLI."""
    parser = argparse.ArgumentParser(
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
    add_agent_argument(parser)
    add_max_workers_arg(parser)
    add_dry_run_arg(
        parser,
        prefix="Suppress GitHub writes and git pushes (no comments, no merges, no pushes).",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable curses UI (use plain logging instead)",
    )
    parser.add_argument(
        "--no-advise",
        action="store_true",
        help="Skip the advise step before CI fixing",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
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
    add_json_arg(parser)
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

    Args:
        results: Per-issue worker results from ``CIDriver.run()``.
        open_prs_remaining: Open PRs left on the repo (each carries
            ``number`` and ``autoMergeRequest``).
        issues: The input issue list (echoed into JSON status).
        as_json: Whether to emit a machine-readable status line.

    Returns:
        ``0`` if clean (possibly with armed-pending PRs still merging),
        ``1`` if any issue failed or any PR needs manual action.

    """
    log = logging.getLogger(__name__)
    failed = [num for num, result in results.items() if not result.success]
    armed_pending = [pr.get("number") for pr in open_prs_remaining if pr.get("autoMergeRequest")]
    needs_action = [pr.get("number") for pr in open_prs_remaining if not pr.get("autoMergeRequest")]
    if armed_pending:
        log.warning(
            "%s PR(s) armed and still merging (waited; not a failure): %s",
            len(armed_pending),
            armed_pending,
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
            )
        return 1

    log.info("CI driver complete")
    if as_json:
        emit_json_status(0, issues=issues, failed=[], needs_action=[], armed_pending=armed_pending)
    return 0


def main() -> int:
    """Execute the CI driver workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt.

    """
    args = _parse_args()
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
