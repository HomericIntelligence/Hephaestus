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
import os
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    add_agent_argument,
    is_codex,
    resolve_agent,
    resume_codex_session,
    run_codex_session,
    session_agent_matches,
)
from hephaestus.cli.utils import add_json_arg, emit_json_status

from ._review_utils import find_pr_for_issue
from .advise_runner import run_advise
from .claude_invoke import invoke_claude_with_session
from .claude_models import advise_model, implementer_model, learn_model
from .claude_timeouts import (
    advise_claude_timeout,
    ci_driver_claude_timeout,
    ci_poll_max_wait,
    learn_claude_timeout,
)
from .git_utils import (
    get_repo_info,
    get_repo_root,
    get_repo_slug,
    issue_ref,
    pr_ref,
    push_current_branch_with_lease_on_divergence,
    rebase_worktree_onto,
    run,
    sync_worktree_to_remote_branch,
)
from .github_api import (
    _gh_call,
    gh_issue_json,
    gh_pr_checks,
    gh_pr_list_unresolved_threads,
    gh_pr_resolve_thread,
)
from .learn import compact_session
from .models import CIDriverOptions, WorkerResult
from .pr_manager import pr_has_implementation_go_label
from .prompts import get_advise_prompt_builder
from .session_naming import AGENT_ADVISE, AGENT_CI_DRIVER
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

    def run(self) -> dict[int, WorkerResult]:  # noqa: C901  # thread pool + finally + preserve report
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
        normalised: list[dict[str, Any]] = []
        for pr in raw_pulls:
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
                    "isBot": (pr.get("user") or {}).get("type") == "Bot",
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

    def _discover_bot_prs(self) -> dict[int, int]:
        """Enumerate every open ``is_bot=true`` PR on the repo (#848).

        Bot PRs (Dependabot, github-actions, etc.) carry NO ``Closes #N``
        link to an issue, so the issue-driven discovery path can never see
        them — they are architecturally invisible. Without this enumeration
        a repo can sit with dozens of stranded Dependabot PRs forever while
        the ecosystem script cheerfully reports "driven" because every
        listed issue had no matching PR.

        Returns a mapping where each bot PR's number is used both as the
        synthetic issue key AND the PR number. Downstream code is taught
        (``_is_bot_pr_mode``) to detect the equality and skip issue-data
        fetches that would 404 on a synthetic key.

        Returns:
            Mapping of ``pr_number -> pr_number`` for every open bot PR.
            Empty dict if the lookup fails or returns nothing — bot
            discovery must never abort the drive.

        """
        try:
            owner, repo = get_repo_info(self.repo_root)
        except RuntimeError as exc:
            logger.info("Bot-PR discovery skipped: could not resolve owner/name (%s)", exc)
            return {}

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
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            logger.info("Bot-PR discovery skipped: gh api failed (%s)", exc)
            return {}

        bot_prs: dict[int, int] = {}
        for pr in raw_pulls:
            user = pr.get("user") or {}
            if user.get("type") != "Bot":
                continue
            number = pr.get("number")
            if isinstance(number, int):
                bot_prs[number] = number

        if bot_prs:
            logger.info(
                "Discovered %s open bot-authored PR(s): %s",
                len(bot_prs),
                sorted(bot_prs),
            )
        return bot_prs

    def _discover_failing_prs(self) -> dict[int, int]:
        """Enumerate open non-draft PRs whose checks failed or merge is BLOCKED.

        Symmetrical to ``_discover_bot_prs``: the issue→PR direction (Closes #N)
        misses every PR with no Closes line and every PR linked to a closed
        issue (issue body §1, §2). One CLI call, PR-keyed, synthetic-issue
        invariant (pr_number == issue_number) so downstream ``_is_bot_pr_mode``
        short-circuits ``gh issue view`` identically to the bot path.

        Bounded by gh's --limit 1000 (its documented hard upper). A repo with
        more than 1000 failing open PRs is pathological — we log a WARNING
        so operators see the truncation rather than silently dropping work.

        Returns:
            Mapping pr_number -> pr_number for every failing open PR.
            Empty dict on any lookup failure — discovery must never abort
            the drive.

        """
        try:
            owner, repo = get_repo_info(self.repo_root)
        except RuntimeError as exc:
            logger.info("Failing-PR discovery skipped: could not resolve owner/name (%s)", exc)
            return {}
        try:
            result = _gh_call(
                [
                    "pr",
                    "list",
                    "--repo",
                    f"{owner}/{repo}",
                    "--state",
                    "open",
                    "--limit",
                    "1000",
                    "--json",
                    "number,isDraft,statusCheckRollup,mergeStateStatus",
                ],
            )
            pulls: list[dict[str, Any]] = json.loads(result.stdout or "[]")
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            logger.info("Failing-PR discovery skipped: gh pr list failed (%s)", exc)
            return {}
        if len(pulls) >= 1000:
            logger.warning(
                "Failing-PR discovery hit gh's 1000-PR cap on %s/%s — "
                "additional failing PRs may exist and are not visible to this run.",
                owner,
                repo,
            )
        failing: dict[int, int] = {}
        for pr in pulls:
            number = pr.get("number")
            if not isinstance(number, int):
                continue
            if _pr_is_failing(pr):
                failing[number] = number
        if failing:
            logger.info(
                "Discovered %s open failing PR(s): %s",
                len(failing),
                sorted(failing),
            )
        return failing

    def _is_bot_pr_mode(self, issue_number: int, pr_number: int) -> bool:
        """Return True iff this work item is a synthetic-issue bot PR (#848).

        The bot-PR enumeration uses the PR number as a stand-in for an
        issue number because Dependabot PRs have no associated issue.
        Anywhere we would normally call ``gh issue view <issue_number>``
        we must instead short-circuit; this helper centralises the check
        so a single rule (issue == pr) keeps both ends honest.
        """
        return issue_number == pr_number

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:  # noqa: C901
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
        raw_map: dict[int, int] = {}
        for issue_num in issue_numbers:
            pr_number = self._find_pr_for_issue(issue_num)
            if pr_number is not None:
                raw_map[issue_num] = pr_number
            else:
                logger.info("Issue #%s: no open PR found, skipping", issue_num)

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
        if self.options.include_bot_prs:
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

    def _drive_issue(  # noqa: C901  # poll loop + required-check classification + fix path
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

            self.status_tracker.update_slot(
                acquired_slot, f"{issue_ref(issue_number)}: fetching checks"
            )

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

                iref = issue_ref(issue_number)
                self.status_tracker.update_slot(
                    acquired_slot,
                    f"{iref}: waiting for CI checks "
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
                    acquired_slot, f"{issue_ref(issue_number)}: enabling auto-merge"
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
                        acquired_slot, f"{issue_ref(issue_number)}: arming for post-merge /learn"
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
        """Rebase a behind/conflicting PR onto its base branch with no agent (#871).

        The cheap, deterministic path: a PR that is merely behind its base (or
        whose changes don't textually overlap) is rebased and pushed here. Only a
        PR whose rebase hits real conflicts falls through to the CI-fix agent.

        Flow:

        1. Query ``mergeStateStatus`` / ``mergeable`` / ``baseRefName``. Skip
           (return ``False``) unless the PR is ``BEHIND`` or ``DIRTY`` /
           ``CONFLICTING`` — a PR already on top of its base needs no rebase and
           the normal check-status path handles it.
        2. Sync the worktree to the PR head, then ``rebase_worktree_onto`` the
           base branch.
        3. Clean rebase → push ``HEAD:<pr-head>`` with ``--force-with-lease`` and
           return ``True``. The push re-triggers CI; the caller's poll loop
           evaluates the rebased head.
        4. Conflicts → the rebase is aborted inside ``rebase_worktree_onto``;
           return ``False`` so the caller continues to the agent path.

        Any unexpected git/subprocess error is logged and swallowed (returns
        ``False``) — a mechanical-rebase failure must never crash the worker; the
        agent path is always the safe fallback.

        Args:
            issue_number: GitHub issue number for the PR.
            pr_number: PR number to rebase.
            acquired_slot: Worker slot ID for status tracking.

        Returns:
            ``True`` if the PR was mechanically rebased and pushed; ``False`` if
            no rebase was needed, the rebase conflicted, or an error occurred.

        """
        try:
            result = _gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "mergeStateStatus,mergeable,headRefName,baseRefName",
                ],
                check=False,
            )
            state = dict(json.loads(result.stdout or "{}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning(
                "Issue #%s: could not fetch PR #%s merge state for rebase; "
                "skipping mechanical rebase: %s",
                issue_number,
                pr_number,
                exc,
            )
            return False

        merge_state = str(state.get("mergeStateStatus") or "").upper()
        # Only behind-base / conflicting PRs need a rebase. CLEAN / BLOCKED /
        # UNSTABLE / HAS_HOOKS PRs are already on top of their base — let the
        # check-status path handle them. (BLOCKED is the review-gated case.)
        if merge_state not in ("BEHIND", "DIRTY", "CONFLICTING"):
            return False

        pr_head_branch = str(state.get("headRefName") or "") or self._get_pr_branch(pr_number)
        base_branch = str(state.get("baseRefName") or "main") or "main"
        if not pr_head_branch:
            logger.warning(
                "Issue #%s: PR #%s has no resolvable head branch; skipping rebase",
                issue_number,
                pr_number,
            )
            return False

        self.status_tracker.update_slot(
            acquired_slot,
            f"{issue_ref(issue_number)}: mechanical rebase onto {base_branch}",
        )

        try:
            worktree_path = self._get_worktree_path(issue_number, pr_number)
            # Land on the PR's actual remote head before rebasing so we replay the
            # PR's commits (not a stale local ref) onto the base (#832).
            sync_worktree_to_remote_branch(worktree_path, pr_head_branch)

            if not rebase_worktree_onto(worktree_path, base_branch):
                logger.info(
                    "Issue #%s: PR #%s (%s) has rebase conflicts onto %s; deferring to agent",
                    issue_number,
                    pr_number,
                    merge_state,
                    base_branch,
                )
                return False

            # Clean rebase rewrote history — lease-push to the PR head. The helper
            # no-ops cleanly if the rebase was already up to date (HEAD unchanged).
            push_current_branch_with_lease_on_divergence(
                worktree_path,
                branch=pr_head_branch,
                push_ref=f"HEAD:{pr_head_branch}",
            )
            logger.info(
                "Issue #%s: mechanically rebased PR #%s onto %s and pushed (no agent)",
                issue_number,
                pr_number,
                base_branch,
            )
            return True
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Issue #%s: mechanical rebase of PR #%s failed (%s); falling through to agent",
                issue_number,
                pr_number,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return False

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
            if is_codex(self.options.agent):
                result = run_codex_session(
                    prompt,
                    cwd=self.repo_root,
                    timeout=advise_claude_timeout(),
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
                timeout=advise_claude_timeout(),
                output_format="text",
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
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult | None:
        """After a CI fix is pushed, re-poll checks and arm if now green.

        The fix push re-triggers CI. Historically ``_attempt_ci_fixes`` returned
        success the instant a fix landed and never came back to arm the PR, so a
        now-green PR sat ``NOT armed`` forever (observed: ProjectHermes #645,
        which ended ``CLEAN`` but un-armed). This re-enters the
        check→arm→wait flow ONCE.

        Returns:
            A terminal ``WorkerResult`` if the PR armed (and we waited on it), or
            ``None`` if CI is still pending / not green yet — in which case the
            caller keeps the fix's success result and a later run arms it.

        """
        if self.options.dry_run:
            return None

        # Bounded poll for the freshly-pushed run to conclude. Reuse the same
        # backoff/cap pattern as the main poll loop.
        max_wait = ci_poll_max_wait()
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
            acquired_slot, f"{issue_ref(issue_number)}: enabling auto-merge (post-fix)"
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
        self._wait_for_pr_terminal(issue_number, pr_number)
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
            rearmed = self._recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot)
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
            rearmed = self._recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot)
            return rearmed if rearmed is not None else fix_result
        return WorkerResult(
            issue_number=issue_number,
            success=False,
            pr_number=pr_number,
            error=f"PR {pr_ref(pr_number)} has an unresolved merge conflict",
        )

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
                # Acknowledge automated review comments the fix addressed by
                # replying + resolving the bot threads (human threads untouched).
                self._reply_and_resolve_bot_threads(pr_number)
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            logger.warning("Issue #%s: CI fix attempt %s failed", issue_number, iteration + 1)

        return None

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
        return self.state_dir / f"drive-green-armed-{issue_number}.json"

    def _load_arming_state(self, issue_number: int) -> dict[str, Any] | None:
        """Return the parsed arming record for ``issue_number`` or ``None``."""
        path = self._arming_state_path(issue_number)
        if not path.exists():
            return None
        try:
            return dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read arming record for issue #%s: %s; ignoring",
                issue_number,
                exc,
            )
            return None

    def _save_arming_state(self, issue_number: int, record: dict[str, Any]) -> None:
        """Persist the arming record. Best-effort; logs and swallows IO errors."""
        path = self._arming_state_path(issue_number)
        try:
            path.write_text(json.dumps(record, indent=2, sort_keys=True))
        except OSError as exc:
            logger.warning(
                "Could not write arming record for issue #%s: %s",
                issue_number,
                exc,
            )

    def _clear_arming_state(self, issue_number: int) -> None:
        path = self._arming_state_path(issue_number)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "Could not delete arming record for issue #%s: %s",
                issue_number,
                exc,
            )

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
            if existing.get("learn_captured_at"):
                # Already captured — don't overwrite the captured timestamp.
                continue
            record = {
                "pr_number": pr_number,
                "pr_head_branch": pr_head_branch,
                "head_sha_at_arming": pr_head_sha,
                "armed_at": armed_at,
                "learn_captured_at": None,
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

            # Still OPEN (or unknown). If a required check has gone red since
            # arming, stop waiting and let the caller drive a fix.
            failing = self._failing_required_check_names(pr_number)
            if failing:
                logger.warning(
                    "Issue #%s: PR #%s went red while awaiting merge (failing: %s)",
                    issue_number,
                    pr_number,
                    ", ".join(failing),
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

            self.status_tracker.update_slot(
                0,
                f"{iref}: PR #{pr_number} awaiting merge ({elapsed}s elapsed)",
            )
            time.sleep(sleep_secs)
            elapsed += sleep_secs
            attempt += 1

    def _sweep_orphaned_arming_records(self) -> None:  # noqa: C901  # one record loop with three state branches; splitting it just hides the contract
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
        whose PR is MERGED (then mark ``learn_captured_at``), leave OPEN
        records alone for the normal per-issue path to handle.
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
            if record.get("learn_captured_at"):
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
                self._run_drive_green_learnings(issue_number, pr_number)
                self._run_drive_green_compact(issue_number, pr_number)
                record["learn_captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._save_arming_state(issue_number, record)
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
          the issue (merge detected + ``/learn`` fired, OR still in flight,
          OR already-captured). Caller returns this directly without doing
          any further drive work.
        - ``None`` if the issue should fall through to the normal drive
          path (no arming record, arming stale, or PR abandoned).
        """
        record = self._load_arming_state(issue_number)
        if record is None:
            return None
        if record.get("learn_captured_at"):
            logger.info(
                "Issue #%s: /learn already captured at %s; skipping further drive",
                issue_number,
                record["learn_captured_at"],
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
            self._run_drive_green_learnings(issue_number, pr_number)
            self._run_drive_green_compact(issue_number, pr_number)
            # Mark captured even if /learn failed — it's best-effort and
            # retrying it on every subsequent run would churn API calls.
            record["learn_captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._save_arming_state(issue_number, record)
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
            self._run_drive_green_learnings(issue_number, pr_number)
            self._run_drive_green_compact(issue_number, pr_number)
            record["learn_captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._save_arming_state(issue_number, record)
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

        Args:
            issue_number: GitHub issue number.

        Returns:
            Session ID string, or None if not found.

        """
        # The implementer persists its state to ``issue-<n>.json`` (see
        # ImplementationStateManager.save), NOT ``state-<n>.json``. Reading the
        # wrong name always missed, so this lookup silently never resumed the
        # implementer's session — masked for Claude (deterministic-UUID resume)
        # but breaking Codex session continuity on the CI-fix path.
        state_file = self.state_dir / f"issue-{issue_number}.json"
        if not state_file.exists():
            logger.debug("No implementer state file for issue #%s", issue_number)
            return None

        try:
            data = json.loads(state_file.read_text())
            session_id: str | None = data.get("session_id")
            session_agent: str | None = data.get("session_agent")
            if session_id and not session_agent_matches(session_agent, self.options.agent):
                logger.info(
                    "Skipping impl session for issue #%s: session belongs to %s, "
                    "selected agent is %s",
                    issue_number,
                    session_agent or "claude",
                    self.options.agent,
                )
                return None
            if session_id:
                logger.debug("Loaded session_id for issue #%s: %s...", issue_number, session_id[:8])
            return session_id
        except Exception as e:
            logger.warning("Could not load session_id for issue #%s: %s", issue_number, e)
            return None

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
        """Reply to and resolve automated review threads after a successful CI fix.

        ci_driver surfaces unresolved threads to the fix prompt but cannot rely
        on GitHub auto-resolving a bot thread when its line moves. After a fix
        lands, post a templated reply on each BOT-authored unresolved thread and
        resolve it, so the PR's automated review comments are acknowledged
        rather than left dangling. Human threads are left untouched. Best-effort:
        a failure on one thread is logged and skipped (never blocks the fix).

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
                gh_pr_resolve_thread(
                    thread_id,
                    "Addressed by the automated CI fix on this PR.",
                    dry_run=False,
                )
                resolved += 1
            except Exception as exc:
                logger.info(
                    "PR #%s: could not reply/resolve bot thread %s (%s); skipping",
                    pr_number,
                    thread_id,
                    exc,
                )
        if resolved:
            logger.info(
                "PR #%s: replied to and resolved %s automated review thread(s)",
                pr_number,
                resolved,
            )
        return resolved

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

    def _force_engagement_prompt(
        self,
        *,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        pr_head_branch: str,
        failing_check_names: list[str],
        review_threads_block: str,
    ) -> str:
        """Build the retry prompt when the agent returned without committing (#846).

        The retry must engage the agent enough to either (a) produce a real
        fix or (b) explicitly say why CI cannot pass. The prompt names the
        failing checks verbatim, re-emphasises the existing PR/branch
        invariant, and re-emphasises signed commits — a no-commit retry is a
        contract violation that the agent has to address head-on.
        """
        failing_block = "\n".join(f"- {n}" for n in failing_check_names) or "- (unknown)"
        return (
            f"{review_threads_block}"
            f"## Force-Engagement Retry — Previous Turn Produced No Commit\n\n"
            f"You just returned from a CI-fix session for PR {pr_ref(pr_number)} "
            f"(issue {issue_ref(issue_number)}) WITHOUT producing a new commit on "
            f"branch `{pr_head_branch}`. The required CI checks below are STILL "
            f"failing on the remote:\n\n"
            f"{failing_block}\n\n"
            f"Returning no commit when required checks are still red is itself a "
            f"bug — fix the code so the failing checks pass. If no code fix is "
            f"possible, DO NOT commit a 'blocker' file: a new Markdown/docs file "
            f"will itself fail the repo's lint gates (e.g. markdownlint) and turn "
            f"one red check into two. Instead leave the tree unchanged and report "
            f"the blocker via the `BLOCKED:` line below — do NOT commit any file to "
            f"document it.\n\n"
            f"Working directory: {worktree_path}\n"
            f"Current branch (DO NOT change, DO NOT create a new branch): "
            f"{pr_head_branch}\n\n"
            f"Required behaviour:\n"
            f"1. Re-read the failing check logs for the names listed above.\n"
            f"2. Make the minimal change that addresses each failure.\n"
            f"3. Run `pixi run python -m pytest tests/ -v` and "
            f"`pre-commit run --all-files` locally to verify before committing. "
            f"This MUST include any markdown/lint hooks — every file you add or "
            f"edit has to pass the repo's own linters, with no rule disabled.\n"
            f"4. **Every commit MUST be cryptographically signed (`git commit -S`).** "
            f"NEVER use `--no-verify`. The repository's CI gate rejects unsigned "
            f"commits and any commit that bypassed pre-commit hooks.\n"
            f"5. Do NOT run `git checkout -b`, `git switch -c`, or any command "
            f"that creates or switches branches — the fix has to land on "
            f"`{pr_head_branch}`.\n"
            f"6. Do NOT add a new top-level `CI_BLOCKER.md` or similar doc file to "
            f"record the blocker — use the `BLOCKED:` line instead.\n\n"
            f"If after the steps above you still cannot produce a commit, reply "
            f"with a single line `BLOCKED: <one-sentence reason>` and stop."
        )

    def _record_repeated_no_commit(
        self,
        *,
        issue_number: int,
        pr_number: int,
        pr_head_branch: str,
        failing_check_names: list[str],
    ) -> None:
        """Persist a marker for the next ecosystem run (#846).

        Writes ``state_dir / "repeated-no-commit-<pr>.json"`` so a future
        run (and the human reading the logs) can see which PRs got stuck
        in the no-commit loop. We deliberately do NOT delete the arming
        record here — the PR is still open and may yet land via another
        actor; the marker file is purely a forensics aid.
        """
        marker = self.state_dir / f"repeated-no-commit-{pr_number}.json"
        try:
            marker.write_text(
                json.dumps(
                    {
                        "issue_number": issue_number,
                        "pr_number": pr_number,
                        "pr_head_branch": pr_head_branch,
                        "failing_required_checks": failing_check_names,
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                )
                + "\n"
            )
        except OSError as exc:
            logger.warning(
                "Issue #%s: failed to write repeated-no-commit marker for PR #%s: %s",
                issue_number,
                pr_number,
                exc,
            )

    def _retry_no_commit_once(  # codex/claude branches stay coupled to keep one retry path
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
        """Re-invoke the agent up to ``max_retries`` times after a no-commit turn (#846).

        A single no-op agent turn used to be terminal: with
        ``max_fix_iterations=1`` the whole issue failed the instant the first
        force-engagement retry also returned no commit. That cost real PRs
        (AchaeanFleet #691/#683, Hermes #643). We now re-engage up to
        ``max_retries`` times before recording the forensics marker, re-checking
        between attempts so a PR that goes green (or where a commit lands)
        short-circuits immediately.

        Only fires when the PR still has failing required checks — a green PR
        that arrived via concurrent activity should NOT be perturbed. Stays on
        the same ``(repo, issue, AGENT_CI_DRIVER)`` session so the agent's
        transcript is continuous; the existing PR and branch are preserved
        (no new PR, no new branch, no force-push to a new ref).

        Args:
            issue_number: GitHub issue number for the PR.
            pr_number: PR number to retry on.
            worktree_path: Worktree the agent runs in (same as the first turn).
            pr_head_branch: PR head branch name; the retry must not switch off it.
            pre_agent_sha: HEAD SHA from before the first turn (used as the
                no-commit baseline for the retry too — if the retry doesn't
                advance past this, we treat it as repeated-no-commit).
            session_id: Codex resume id (Claude resumes by deterministic UUID).

        Returns:
            True if the retry produced a new commit (caller should push).
            False if CI is green now, the retry returned no commit again, or
            any error path fired. On repeated-no-commit, writes a forensics
            marker to ``state_dir``.

        """
        failing: list[str] = []
        for retry in range(1, max_retries + 1):
            failing = self._failing_required_check_names(pr_number)
            if not failing:
                logger.info(
                    "Issue #%s: no-commit turn but PR #%s has no failing required "
                    "checks; skipping force-engagement retry",
                    issue_number,
                    pr_number,
                )
                return False

            review_threads_block = self._format_review_threads_block(pr_number)
            retry_prompt = self._force_engagement_prompt(
                issue_number=issue_number,
                pr_number=pr_number,
                worktree_path=worktree_path,
                pr_head_branch=pr_head_branch,
                failing_check_names=failing,
                review_threads_block=review_threads_block,
            )

            logger.warning(
                "Issue #%s: no-commit on CI fix turn; re-invoking with "
                "force-engagement prompt (retry %s/%s, failing: %s)",
                issue_number,
                retry,
                max_retries,
                ", ".join(failing) or "<unknown>",
            )

            try:
                if is_codex(self.options.agent):
                    if session_id:
                        try:
                            resume_codex_session(
                                session_id,
                                retry_prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                            )
                        except subprocess.CalledProcessError as exc:
                            logger.warning(
                                "Issue #%s: Codex retry resume failed for PR #%s; "
                                "falling back to fresh session: %s",
                                issue_number,
                                pr_number,
                                (exc.stderr or exc.stdout or "")[:300],
                            )
                            run_codex_session(
                                retry_prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                                sandbox="workspace-write",
                            )
                    else:
                        run_codex_session(
                            retry_prompt,
                            cwd=worktree_path,
                            timeout=ci_driver_claude_timeout(),
                            sandbox="workspace-write",
                        )
                else:
                    repo_slug = get_repo_slug(self.repo_root)
                    invoke_claude_with_session(
                        repo=repo_slug,
                        issue=issue_number,
                        agent=AGENT_CI_DRIVER,
                        prompt=retry_prompt,
                        model=implementer_model(),
                        cwd=worktree_path,
                        timeout=ci_driver_claude_timeout(),
                        output_format="json",
                        allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                        extra_args=["--dangerously-skip-permissions"],
                        input_via_stdin=True,
                    )
            except subprocess.CalledProcessError as exc:
                logger.error(
                    "Issue #%s: no-commit retry session failed for PR #%s: %s",
                    issue_number,
                    pr_number,
                    (exc.stderr or exc.stdout or "")[:300],
                )
                return False
            except subprocess.TimeoutExpired:
                logger.error(
                    "Issue #%s: no-commit retry session timed out for PR #%s",
                    issue_number,
                    pr_number,
                )
                return False

            if self._head_advanced(worktree_path, pre_agent_sha, issue_number):
                return True

            logger.warning(
                "Issue #%s: still no commit on PR #%s after force-engagement retry %s/%s",
                issue_number,
                pr_number,
                retry,
                max_retries,
            )

        logger.error(
            "Issue #%s: REPEATED no-commit on PR #%s after %s force-engagement "
            "retries; marking and moving on",
            issue_number,
            pr_number,
            max_retries,
        )
        self._record_repeated_no_commit(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            failing_check_names=failing,
        )
        return False

    def _head_advanced(
        self,
        worktree_path: Path,
        pre_agent_sha: str,
        issue_number: int,
    ) -> bool:
        """Return True iff HEAD has moved past ``pre_agent_sha`` after the agent ran.

        Called between the agent session and the push to detect the
        no-commit-made case (#836). When ``pre_agent_sha`` still matches HEAD
        the agent did not commit anything, so a force-with-lease push of
        HEAD:<branch> would be a silent 0-exit no-op and the driver would
        falsely log "pushed CI fixes". We instead log a warning and return
        False so the iteration counts as failed.

        Args:
            worktree_path: Worktree to inspect.
            pre_agent_sha: HEAD SHA captured right after the pre-agent sync.
            issue_number: For log context.

        Returns:
            True if HEAD moved (something to push); False if it did not (or
            if reading HEAD failed — we treat that as "don't push" too).

        """
        try:
            post_agent_sha = run(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to read HEAD after CI fix session: %s",
                issue_number,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return False
        if post_agent_sha == pre_agent_sha:
            logger.warning(
                "Issue #%s: agent session produced no new commit (HEAD unchanged "
                "at %s); skipping push and treating iteration as failed",
                issue_number,
                pre_agent_sha[:8],
            )
            return False
        return True

    def _run_ci_fix_session(  # noqa: C901  # provider resume/fallback paths are intentionally coupled
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
        """Invoke the selected coding agent to fix CI failures, then push the result.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            worktree_path: Path to the checked-out worktree.
            ci_logs: Combined CI failure log text.
            session_id: Optional agent session ID to resume.
            advise_findings: Prior learnings from the advise step, prepended to
                the prompt as context. Empty or a skip marker contributes nothing.
            pr_head_branch: The PR's head-branch name on the remote. The push
                uses this as the destination refspec so the fix lands on the
                actual PR branch even if the agent switched branches locally
                during the session (#832).

        Returns:
            True if the fix session succeeded and the branch was pushed.

        """
        # Sync the worktree to the PR's actual remote head BEFORE the agent
        # runs. WorktreeManager may have reused a stale local branch ref that
        # pointed at an old ``main`` tip — without this reset the agent would
        # commit on top of the wrong base and the force-with-lease push would
        # either no-op or regress the PR (#832).
        try:
            sync_worktree_to_remote_branch(worktree_path, pr_head_branch)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to sync worktree to origin/%s before CI fix: %s",
                issue_number,
                pr_head_branch,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return False

        # Snapshot HEAD immediately after the sync. The push helper exits 0
        # silently when HEAD == origin/<branch> (nothing to push), so without
        # this guard we logged "pushed CI fixes" for sessions that returned
        # without committing — the remote was unchanged but the driver
        # claimed success (#836). The post-agent HEAD is compared below.
        try:
            pre_agent_sha = run(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to snapshot HEAD before CI fix session: %s",
                issue_number,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return False

        advise_block = ""
        findings = advise_findings.strip()
        if findings and not findings.startswith("<!-- advise step skipped"):
            advise_block = f"## Prior Learnings from Team Knowledge Base\n\n{findings}\n\n---\n\n"
        # Inject any unresolved PR review threads at the top of the prompt so
        # the agent addresses reviewer feedback BEFORE re-running CI — an
        # unresolved bot/human comment is usually the actual blocker (#846).
        review_threads_block = self._format_review_threads_block(pr_number)
        prompt = (
            f"{advise_block}"
            f"{review_threads_block}"
            f"Fix the CI failures for PR {pr_ref(pr_number)} (issue {issue_ref(issue_number)}).\n\n"
            f"Working directory: {worktree_path}\n"
            f"Current branch (DO NOT change): {pr_head_branch}\n\n"
            f"CI failure logs:\n{ci_logs}\n\n"
            "Fix the code to make the CI checks pass. After fixing:\n"
            "1. Run: pixi run python -m pytest tests/ -v\n"
            "2. Run: pre-commit run --all-files\n"
            "3. Commit changes (do NOT push) on the current branch — DO NOT run "
            "`git checkout -b`, `git switch -c`, or any other command that creates "
            "or switches to a different branch\n"
            "4. Every commit MUST be cryptographically signed (`git commit -S`); "
            "NEVER use `--no-verify`.\n\n"
            f"Commit message: fix: Address CI failures for PR {pr_ref(pr_number)}\n"
        )

        try:
            if is_codex(self.options.agent):
                try:
                    if session_id:
                        try:
                            codex_result = resume_codex_session(
                                session_id,
                                prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                            )
                        except subprocess.CalledProcessError as e:
                            logger.warning(
                                "Issue #%s: Codex resume session %r failed for PR #%s; "
                                "falling back to fresh session: %s",
                                issue_number,
                                session_id,
                                pr_number,
                                (e.stderr or e.stdout or "")[:300],
                            )
                            codex_result = run_codex_session(
                                prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                                sandbox="workspace-write",
                            )
                    else:
                        codex_result = run_codex_session(
                            prompt,
                            cwd=worktree_path,
                            timeout=ci_driver_claude_timeout(),
                            sandbox="workspace-write",
                        )
                    logger.debug(
                        "Issue #%s: Codex CI fix output: %s",
                        issue_number,
                        codex_result.stdout[:500],
                    )
                except subprocess.CalledProcessError as e:
                    logger.error(
                        "Issue #%s: Codex CI fix session returned exit code %s: %s",
                        issue_number,
                        e.returncode,
                        (e.stderr or e.stdout or "")[:300],
                    )
                    return False

                # First turn produced no commit → force-engage once on the
                # same session before giving up (#846). Stays on the same PR +
                # branch (no new PR, no new branch). Nested two-step keeps the
                # head-advance check and the retry decision separate for
                # readability.
                if not self._head_advanced(  # noqa: SIM102
                    worktree_path, pre_agent_sha, issue_number
                ):
                    if not self._retry_no_commit_once(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        worktree_path=worktree_path,
                        pr_head_branch=pr_head_branch,
                        pre_agent_sha=pre_agent_sha,
                        session_id=session_id,
                    ):
                        return False
                try:
                    push_current_branch_with_lease_on_divergence(
                        worktree_path,
                        branch=pr_head_branch,
                        push_ref=f"HEAD:{pr_head_branch}",
                    )
                    logger.info("Issue #%s: pushed CI fixes for PR #%s", issue_number, pr_number)
                    return True
                except Exception as push_err:
                    logger.error(
                        "Issue #%s: git push failed after CI fix: %s", issue_number, push_err
                    )
                    return False

            # drive-green runs its OWN session (Session 3, AGENT_CI_DRIVER),
            # independent of the implementer's transcript. The first fix call
            # creates it via --session-id; later calls resume it. The codex
            # path above instead resumes the raw ``session_id`` it was handed.
            repo_slug = get_repo_slug(self.repo_root)
            try:
                stdout, _ = invoke_claude_with_session(
                    repo=repo_slug,
                    issue=issue_number,
                    agent=AGENT_CI_DRIVER,
                    prompt=prompt,
                    model=implementer_model(),
                    cwd=worktree_path,
                    timeout=ci_driver_claude_timeout(),
                    output_format="json",
                    allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                    extra_args=["--dangerously-skip-permissions"],
                    input_via_stdin=True,
                )
                claude_result = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=stdout, stderr=""
                )
            except subprocess.CalledProcessError as exc:
                claude_result = subprocess.CompletedProcess(
                    args=exc.cmd,
                    returncode=exc.returncode,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                )

            if claude_result.returncode == 0:
                # Push the fixes
                # First turn produced no commit → force-engage once on the
                # same session before giving up (#846). Stays on the same PR +
                # branch (no new PR, no new branch). Nested two-step keeps the
                # head-advance check and the retry decision separate for
                # readability.
                if not self._head_advanced(  # noqa: SIM102
                    worktree_path, pre_agent_sha, issue_number
                ):
                    if not self._retry_no_commit_once(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        worktree_path=worktree_path,
                        pr_head_branch=pr_head_branch,
                        pre_agent_sha=pre_agent_sha,
                        session_id=session_id,
                    ):
                        return False
                try:
                    push_current_branch_with_lease_on_divergence(
                        worktree_path,
                        branch=pr_head_branch,
                        push_ref=f"HEAD:{pr_head_branch}",
                    )
                    logger.info("Issue #%s: pushed CI fixes for PR #%s", issue_number, pr_number)
                    return True
                except Exception as push_err:
                    logger.error(
                        "Issue #%s: git push failed after CI fix: %s", issue_number, push_err
                    )
                    return False

            logger.error(
                "Issue #%s: Claude CI fix session returned exit code %s: %s",
                issue_number,
                claude_result.returncode,
                claude_result.stderr[:300],
            )
            return False

        except subprocess.TimeoutExpired:
            logger.error(
                "Issue #%s: Claude CI fix session timed out for PR #%s", issue_number, pr_number
            )
            return False
        except Exception as e:
            logger.error(
                "Issue #%s: CI fix session failed for PR #%s: %s", issue_number, pr_number, e
            )
            return False

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
        """Capture drive-green learnings under AGENT_CI_DRIVER (Session 3).

        Runs after a PR reaches green and auto-merge is enabled, mirroring the
        implementer's post-PR ``/learn`` step but scoped to *this* drive: what
        made CI fail and how it was fixed. Resumes Session 3 (the
        ``AGENT_CI_DRIVER`` session the fix session created) so the learnings
        compound on the same transcript that did the work.

        This is best-effort. Any failure is logged at WARNING and swallowed so
        a flaky learnings step never flips a successful drive to failure.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.

        Returns:
            True if the learnings session completed, False otherwise.

        """
        # The Claude path resumes the deterministic AGENT_CI_DRIVER session via
        # invoke_claude_with_session. Codex drive-green sessions are not
        # persisted by this module, so there is no Session 3 to resume there.
        if is_codex(self.options.agent):
            logger.info(
                "Issue #%s: skipping drive-green learnings (codex has no persisted "
                "drive-green session to resume)",
                issue_number,
            )
            return False

        prompt = (
            "/skills-registry-commands:learn "
            f"You just drove PR {pr_ref(pr_number)} (issue {issue_ref(issue_number)}) "
            "to green CI. Capture concise learnings about what made CI fail and how "
            "you fixed it, scoped to this issue/PR. Commit the results and create a PR. "
            "IMPORTANT: Only push skills to ProjectMnemosyne. "
            "Do NOT create files under .claude-plugin/ in this repo."
        )
        try:
            repo_slug = get_repo_slug(self.repo_root)
            # Best-effort: try to resume in the original worktree (so the
            # AGENT_CI_DRIVER transcript is found by ``session_jsonl_path``).
            # In the post-merge code path (#840) the PR's head branch may be
            # gone from the remote, so ``_get_worktree_path`` could fail. In
            # that case fall back to ``repo_root`` cwd — the prompt is
            # self-contained, and a fresh ``--session-id`` create still
            # captures the lesson.
            try:
                cwd = self._get_worktree_path(issue_number, pr_number)
            except Exception as wt_err:
                logger.info(
                    "Issue #%s: no worktree available for /learn (%s); "
                    "falling back to repo root for a fresh session",
                    issue_number,
                    wt_err,
                )
                cwd = self.repo_root
            invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_CI_DRIVER,
                prompt=prompt,
                model=learn_model(),
                cwd=cwd,
                timeout=learn_claude_timeout(),
                output_format="text",
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                extra_args=["--dangerously-skip-permissions"],
                input_via_stdin=True,
            )
            logger.info("Issue #%s: drive-green learnings captured", issue_number)
            return True
        except Exception as e:  # broad: external claude process; non-blocking
            logger.warning(
                "Issue #%s: drive-green learnings failed (non-fatal): %s",
                issue_number,
                e,
            )
            return False

    def _run_drive_green_compact(self, issue_number: int, pr_number: int) -> bool:
        """Compact the AGENT_CI_DRIVER session transcript after /learn (#842).

        Mirrors the cwd-derivation of ``_run_drive_green_learnings``: try the
        worktree first so the deterministic JSONL probe in ``session_jsonl_path``
        finds the transcript, fall back to ``repo_root`` when the branch is
        already gone post-merge. Non-fatal.
        """
        if is_codex(self.options.agent):
            logger.info(
                "Issue #%s: skipping /compact (codex has no persisted "
                "drive-green session to resume)",
                issue_number,
            )
            return False
        try:
            cwd = self._get_worktree_path(issue_number, pr_number)
        except Exception as wt_err:
            logger.info(
                "Issue #%s: no worktree available for /compact (%s); using repo root",
                issue_number,
                wt_err,
            )
            cwd = self.repo_root
        repo_slug = get_repo_slug(self.repo_root)
        return compact_session(
            repo=repo_slug,
            issue=issue_number,
            agent=AGENT_CI_DRIVER,
            cwd=cwd,
        )

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


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments for the CI driver CLI."""
    parser = argparse.ArgumentParser(
        description="Drive PRs to green CI: fix failures and enable auto-merge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Drive CI for specific issues
  %(prog)s --issues 123 456 789

  # Drive specific PRs directly
  %(prog)s --prs 661 662 664 666

  # Dry run (no GitHub writes or git pushes)
  %(prog)s --issues 123 --dry-run

  # Run with more workers
  %(prog)s --issues 123 456 --max-workers 5

  # Verbose output
  %(prog)s --issues 123 -v
        """,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="*",
        default=[],
        help=(
            "Issue numbers whose PRs should be driven to green CI. Optional: "
            "when omitted, the driver still picks up open bot-authored PRs via "
            "--include-bot-prs (default on) (#848)."
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
    parser.add_argument(
        "--max-workers",
        type=int,
        default=3,
        choices=range(1, 33),
        metavar="N",
        help="Maximum number of parallel workers, 1-32 (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without any GitHub writes or git pushes",
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
        "--force-run",
        action="store_true",
        help=(
            "Bypass the final-loop-only gate. By default, the driver refuses to "
            "run unless HEPH_LOOP_INDEX == HEPH_TOTAL_LOOPS or both are unset; "
            "use --force-run to override (e.g. ad-hoc invocation outside the "
            "automation loop). Setting HEPH_CI_DRIVER_FORCE=1 has the same effect."
        ),
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

    return parser.parse_args()


def _final_loop_gate_passes(force: bool) -> tuple[bool, str]:
    """Return (allowed, reason) for the final-loop-only gate.

    The shell loop sets HEPH_LOOP_INDEX (1-based current loop) and
    HEPH_TOTAL_LOOPS so this module can refuse to run on non-final loops.
    Both unset means "not running under the loop" — allowed (CI debugging,
    one-shot invocations). Either set without the other is treated as a
    misconfiguration and the gate fails closed.
    """
    if force or os.environ.get("HEPH_CI_DRIVER_FORCE") == "1":
        return True, "force flag set"

    idx_raw = os.environ.get("HEPH_LOOP_INDEX")
    total_raw = os.environ.get("HEPH_TOTAL_LOOPS")
    if idx_raw is None and total_raw is None:
        return True, "no loop env set (standalone invocation)"
    if idx_raw is None or total_raw is None:
        return False, (
            "only one of HEPH_LOOP_INDEX/HEPH_TOTAL_LOOPS is set "
            f"(idx={idx_raw!r}, total={total_raw!r}); fail-closed"
        )
    try:
        idx = int(idx_raw)
        total = int(total_raw)
    except ValueError:
        return False, (f"non-integer loop env: idx={idx_raw!r} total={total_raw!r}")
    if idx != total:
        return False, f"loop {idx}/{total} — drive-green is final-loop-only"
    return True, f"final loop {idx}/{total}"


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

    allowed, reason = _final_loop_gate_passes(force=args.force_run)
    if not allowed:
        log.error(
            "ci_driver refused to run: %s. Pass --force-run (or set "
            "HEPH_CI_DRIVER_FORCE=1) to override.",
            reason,
        )
        if args.json:
            emit_json_status(2, message=f"gate refused: {reason}")
        return 2
    log.debug("ci_driver gate passed: %s", reason)

    log.info("Starting CI driver for issues: %s, direct PRs: %s", args.issues, args.prs)

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
