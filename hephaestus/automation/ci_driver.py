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
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    add_agent_argument,
    is_codex,
    resume_codex_session,
    run_codex_session,
    session_agent_matches,
)
from hephaestus.cli.utils import add_json_arg, emit_json_status

from ._review_utils import find_pr_for_issue
from .advise_runner import run_advise
from .claude_invoke import invoke_claude_with_session
from .claude_models import advise_model, implementer_model, learn_model
from .claude_timeouts import ci_driver_claude_timeout, learn_claude_timeout
from .git_utils import (
    get_repo_info,
    get_repo_root,
    get_repo_slug,
    issue_ref,
    pr_ref,
    push_current_branch_with_lease_on_divergence,
    run,
    sync_worktree_to_remote_branch,
)
from .github_api import _gh_call, gh_issue_json, gh_pr_checks
from .models import CIDriverOptions, WorkerResult
from .prompts import get_advise_prompt
from .session_naming import AGENT_ADVISE, AGENT_CI_DRIVER, current_trunk_githash
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


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

        if not self.options.issues:
            logger.warning("No issues to process")
            return {}

        # Pre-discover PRs — only submit workers for issues that have an open PR.
        # This prevents Claude from being launched for issues with no PR at all.
        pr_map = self._discover_prs(self.options.issues)
        if not pr_map:
            logger.warning("No open PRs found for the specified issues — nothing to drive")
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
            normalised.append(
                {
                    "number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "headRefName": (pr.get("head") or {}).get("ref", ""),
                    "autoMergeRequest": pr.get("auto_merge"),
                }
            )
        return normalised

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:
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
        _ci_poll_max_wait: int = int(os.environ.get("HEPH_CI_POLL_MAX_WAIT", "600"))

        try:
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
                merge_ok = self._enable_auto_merge(pr_number)
                if merge_ok:
                    # PR is green and auto-merge is enabled — capture drive-green
                    # learnings under AGENT_CI_DRIVER (Session 3). Non-fatal:
                    # a failed learnings step must not flip the drive to failure.
                    self.status_tracker.update_slot(
                        acquired_slot, f"{issue_ref(issue_number)}: capturing learnings"
                    )
                    self._run_drive_green_learnings(issue_number, pr_number)
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
                    timeout=180,
                    sandbox="read-only",
                )
                return (result.stdout or "").strip()
            githash = current_trunk_githash(self.repo_root)
            repo_slug = get_repo_slug(self.repo_root)
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_ADVISE,
                githash=githash,
                prompt=prompt,
                model=advise_model(),
                cwd=self.repo_root,
                timeout=180,
                output_format="text",
            )
            return (stdout or "").strip()

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=_invoke,
            build_prompt=get_advise_prompt,
        )

    def _attempt_ci_fixes(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
    ) -> WorkerResult | None:
        """Attempt CI fix iterations for a failing PR.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            acquired_slot: Worker slot ID for status tracking.

        Returns:
            WorkerResult on success or dry-run, None if all iterations failed.

        """
        # Advise-first (#30): pull prior learnings once before the fix loop, so
        # we only spend an advise call on PRs that actually need fixing. Fed
        # into every fix-session prompt below.
        advise_findings = ""
        if self.options.enable_advise:
            self.status_tracker.update_slot(acquired_slot, f"{issue_ref(issue_number)}: advising")
            advise_findings = self._run_advise(issue_number)

        for iteration in range(self.options.max_fix_iterations):
            self.status_tracker.update_slot(
                acquired_slot,
                f"{issue_ref(issue_number)}: fetching CI logs (attempt {iteration + 1})",
            )
            ci_logs = self._get_failing_ci_logs(pr_number)
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

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Load the Claude session ID from the implementer's saved state.

        Args:
            issue_number: GitHub issue number.

        Returns:
            Session ID string, or None if not found.

        """
        state_file = self.state_dir / f"state-{issue_number}.json"
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
        """Invoke Claude to fix CI failures, then push the result.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            worktree_path: Path to the checked-out worktree.
            ci_logs: Combined CI failure log text.
            session_id: Optional Claude session ID to resume.
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
        prompt = (
            f"{advise_block}"
            f"Fix the CI failures for PR {pr_ref(pr_number)} (issue {issue_ref(issue_number)}).\n\n"
            f"Working directory: {worktree_path}\n"
            f"Current branch (DO NOT change): {pr_head_branch}\n\n"
            f"CI failure logs:\n{ci_logs}\n\n"
            "Fix the code to make the CI checks pass. After fixing:\n"
            "1. Run: pixi run python -m pytest tests/ -v\n"
            "2. Run: pre-commit run --all-files\n"
            "3. Commit changes (do NOT push) on the current branch — DO NOT run "
            "`git checkout -b`, `git switch -c`, or any other command that creates "
            "or switches to a different branch\n\n"
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

                if not self._head_advanced(worktree_path, pre_agent_sha, issue_number):
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
            githash = current_trunk_githash(self.repo_root)
            repo_slug = get_repo_slug(self.repo_root)
            try:
                stdout, _ = invoke_claude_with_session(
                    repo=repo_slug,
                    issue=issue_number,
                    agent=AGENT_CI_DRIVER,
                    githash=githash,
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
                if not self._head_advanced(worktree_path, pre_agent_sha, issue_number):
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

    def _enable_auto_merge(self, pr_number: int) -> bool:
        """Enable auto-merge for the given PR using squash strategy.

        First attempts ``gh pr merge --auto --squash``. This repo is
        squash-only — rebase merges are disabled by branch protection, so the
        primary path MUST use ``--squash``. On failure, if
        ``options.force_merge_on_stall`` is set, falls back to a direct
        squash merge (``gh pr merge --squash --delete-branch``). If both
        strategies fail, logs an ERROR and returns False.

        Args:
            pr_number: GitHub PR number.

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
            githash = current_trunk_githash(self.repo_root)
            repo_slug = get_repo_slug(self.repo_root)
            # Resume from the SAME worktree the fix session used: the Session 3
            # transcript is probed by cwd (session_jsonl_path), so a different
            # cwd would silently start a cold session instead of resuming.
            worktree_path = self._get_worktree_path(issue_number, pr_number)
            invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_CI_DRIVER,
                githash=githash,
                prompt=prompt,
                model=learn_model(),
                cwd=worktree_path,
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
        nargs="+",
        required=True,
        help="Issue numbers whose PRs should be driven to green CI",
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


def main() -> int:
    """Execute the CI driver workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt.

    """
    args = _parse_args()
    _setup_logging(args.verbose)

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

    log.info("Starting CI driver for issues: %s", args.issues)

    try:
        options = CIDriverOptions(
            issues=args.issues,
            agent=args.agent,
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            enable_ui=not args.no_ui and not args.json,
            verbose=args.verbose,
        )

        driver = CIDriver(options)
        results = driver.run()

        failed = [num for num, result in results.items() if not result.success]
        # The repo is "done" only when there are zero open PRs left — even
        # an empty ``failed`` list is not sufficient because auto-merge may
        # still be pending, PRs from outside the input set may remain, etc.
        # (#838). ``driver.open_prs_remaining`` is populated by ``run()``.
        remaining_pr_numbers = [pr.get("number") for pr in driver.open_prs_remaining]
        if failed or remaining_pr_numbers:
            if failed:
                log.error("CI drive failed for %s issue(s): %s", len(failed), failed)
            if remaining_pr_numbers:
                log.error(
                    "Repo not done: %s open PR(s) remain: %s",
                    len(remaining_pr_numbers),
                    remaining_pr_numbers,
                )
            if args.json:
                emit_json_status(
                    1,
                    issues=args.issues,
                    failed=failed,
                    open_prs_remaining=remaining_pr_numbers,
                )
            return 1

        log.info("CI driver complete")
        if args.json:
            emit_json_status(0, issues=args.issues, failed=[], open_prs_remaining=[])
        return 0

    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        if args.json:
            emit_json_status(130, message="interrupted")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
