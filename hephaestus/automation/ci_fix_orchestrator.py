"""CI fix orchestration collaborator: mechanical rebase, agent sessions, push.

Extracted from :class:`~hephaestus.automation.ci_driver.CIDriver` as a narrow
SRP collaborator (#1289). Owns all logic that invokes the coding agent, rebases
branches, and pushes CI fixes.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    is_codex,
    resume_codex_session,
    run_codex_session,
    session_agent_matches,
)
from hephaestus.automation.advise_runner import run_advise
from hephaestus.automation.claude_invoke import invoke_claude_with_session
from hephaestus.automation.claude_models import advise_model, implementer_model
from hephaestus.automation.claude_timeouts import (
    advise_claude_timeout,
    ci_driver_claude_timeout,
    ci_poll_max_wait,
)
from hephaestus.automation.git_utils import (
    get_repo_slug,
    issue_ref,
    pr_ref,
    push_current_branch_with_lease_on_divergence,
    rebase_worktree_onto,
    run,
    sync_worktree_to_remote_branch,
)
from hephaestus.automation.github_api import _gh_call, gh_issue_json, gh_pr_checks
from hephaestus.automation.models import CIDriverOptions, WorkerResult
from hephaestus.automation.prompts import get_advise_prompt_builder

from .session_naming import AGENT_ADVISE, AGENT_CI_DRIVER

logger = logging.getLogger(__name__)


class CIFixOrchestrator:
    """Orchestrates coding-agent CI-fix sessions, mechanical rebases, and pushes.

    Args:
        options: CI driver configuration options.
        repo_root: Path to the repository root.
        state_dir: Path to the driver's per-run state directory.
        status_tracker: Object exposing ``update_slot(slot, message)``.

    Collaborator delegates wired by CIDriver after construction (to avoid
    circular imports):

    - ``_get_pr_branch_fn``: ``(pr_number) -> str``
    - ``_get_worktree_path_fn``: ``(issue_number, pr_number) -> Path``
    - ``_is_bot_pr_mode_fn``: ``(issue_number, pr_number) -> bool``
    - ``_pr_has_implementation_go_fn``: ``(pr_number) -> bool``
    - ``_enable_auto_merge_fn``: ``(pr_number, is_bot_pr) -> bool``
    - ``_gh_pr_state_fn``: ``(pr_number) -> dict | None``
    - ``_arm_drive_green_fn``: ``(pr_number, pr_head_branch, pr_head_sha) -> None``
    - ``_wait_for_pr_terminal_fn``: ``(issue_number, pr_number) -> str``
    - ``_reply_and_resolve_bot_threads_fn``: ``(pr_number) -> int``
    - ``_format_review_threads_block_fn``: ``(pr_number) -> str``
    - ``_failing_required_check_names_fn``: ``(pr_number) -> list[str]``
    - ``_tracked_worktree_changes_fn``: ``(worktree_path, issue_number) -> list[str]``

    """

    def __init__(
        self,
        *,
        options: CIDriverOptions,
        repo_root: Path,
        state_dir: Path,
        status_tracker: Any,
    ) -> None:
        """Initialize the orchestrator; wire cross-collaborator slots after construction."""
        self.options = options
        self.repo_root = repo_root
        self.state_dir = state_dir
        self.status_tracker = status_tracker

        # Wired post-construction by CIDriver
        self._get_pr_branch_fn: Any = None
        self._get_worktree_path_fn: Any = None
        self._is_bot_pr_mode_fn: Any = None
        self._pr_has_implementation_go_fn: Any = None
        self._enable_auto_merge_fn: Any = None
        self._gh_pr_state_fn: Any = None
        self._arm_drive_green_fn: Any = None
        self._wait_for_pr_terminal_fn: Any = None
        self._reply_and_resolve_bot_threads_fn: Any = None
        self._format_review_threads_block_fn: Any = None
        self._failing_required_check_names_fn: Any = None
        self._tracked_worktree_changes_fn: Any = None

    # ------------------------------------------------------------------
    # Mechanical rebase
    # ------------------------------------------------------------------

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
        if merge_state not in ("BEHIND", "DIRTY", "CONFLICTING"):
            return False

        pr_head_branch = str(state.get("headRefName") or "") or self._get_pr_branch_fn(pr_number)
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
            worktree_path = self._get_worktree_path_fn(issue_number, pr_number)
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

    # ------------------------------------------------------------------
    # Advise step
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Post-fix re-arm
    # ------------------------------------------------------------------

    def _recheck_and_arm_after_fix(
        self, issue_number: int, pr_number: int, acquired_slot: int
    ) -> WorkerResult | None:
        """After a CI fix is pushed, re-poll checks and arm if now green.

        Returns:
            A terminal ``WorkerResult`` if the PR armed (and we waited on it), or
            ``None`` if CI is still pending / not green yet.

        """
        if self.options.dry_run:
            return None

        max_wait = ci_poll_max_wait()
        elapsed = 0
        attempt = 0
        checks: list[dict[str, Any]] = []
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
            import time

            time.sleep(sleep_secs)
            elapsed += sleep_secs
            attempt += 1

        required = [c for c in checks if c.get("required", False)] or checks
        if not all(c.get("conclusion") in ("success", "skipped", "neutral") for c in required):
            return None

        if not self._pr_has_implementation_go_fn(pr_number):
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
        merge_ok = self._enable_auto_merge_fn(
            pr_number, is_bot_pr=self._is_bot_pr_mode_fn(issue_number, pr_number)
        )
        if not merge_ok:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                pr_number=pr_number,
                error=f"auto-merge failed for PR {pr_ref(pr_number)}",
            )
        gh_state = self._gh_pr_state_fn(pr_number)
        pr_head_sha = (gh_state or {}).get("headRefOid", "") or ""
        pr_head_branch = self._get_pr_branch_fn(pr_number)
        self._arm_drive_green_fn(pr_number, pr_head_branch, pr_head_sha)
        self._wait_for_pr_terminal_fn(issue_number, pr_number)
        return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

    # ------------------------------------------------------------------
    # Dirty-PR resolution
    # ------------------------------------------------------------------

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

        if self._attempt_mechanical_rebase(issue_number, pr_number, acquired_slot):
            rearmed = self._recheck_and_arm_after_fix(issue_number, pr_number, acquired_slot)
            if rearmed is not None:
                return rearmed
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

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

    # ------------------------------------------------------------------
    # CI fix iterations (top-level fix loop)
    # ------------------------------------------------------------------

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
                fix-session prompt.

        Returns:
            WorkerResult on success or dry-run, None if all iterations failed.

        """
        advise_findings = ""
        if self.options.enable_advise and not self._is_bot_pr_mode_fn(issue_number, pr_number):
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
            worktree_path = self._get_worktree_path_fn(issue_number, pr_number)
            pr_head_branch = self._get_pr_branch_fn(pr_number)

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
                self._reply_and_resolve_bot_threads_fn(pr_number)
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            logger.warning("Issue #%s: CI fix attempt %s failed", issue_number, iteration + 1)

        return None

    # ------------------------------------------------------------------
    # CI log fetching (mirrored from CICheckInspector for use in sessions)
    # ------------------------------------------------------------------

    def _get_failing_ci_logs(self, pr_number: int) -> str:
        """Fetch combined failure logs for recent failed CI runs on a PR."""
        try:
            branch = self._get_pr_branch_fn(pr_number)
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
    # Session-id loading
    # ------------------------------------------------------------------

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Load the agent session ID from the implementer's saved state."""
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

    # ------------------------------------------------------------------
    # No-commit retry helpers
    # ------------------------------------------------------------------

    def _tracked_worktree_changes(self, worktree_path: Path, issue_number: int) -> list[str]:
        """Return tracked dirty status lines for a post-agent worktree."""
        status = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "status", "--porcelain"],
            "failed to inspect worktree status for no-commit retry",
        )
        if status is None:
            return []
        return [line for line in status.splitlines() if line.strip() and not line.startswith("?? ")]

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
        """Build the retry prompt when the agent returned without committing (#846)."""
        failing_block = "\n".join(f"- {n}" for n in failing_check_names) or "- (unknown)"
        dirty_lines = dirty_tracked_changes or []
        dirty_block = "\n".join(f"- {line}" for line in dirty_lines)
        if dirty_block:
            dirty_block = (
                "\n\nThe local worktree also contains uncommitted tracked changes "
                "from the previous turn. Review this existing diff first and either "
                f"commit it after verification or amend it before committing:\n\n{dirty_block}\n"
            )
        remote_block = (
            "The required CI checks below are STILL failing on the remote"
            if failing_check_names
            else "The remote checks may be green, but the PR still needs a committed "
            "repair before the driver can push"
        )
        return (
            f"{review_threads_block}"
            f"## Force-Engagement Retry — Previous Turn Produced No Commit\n\n"
            f"You just returned from a CI-fix session for PR {pr_ref(pr_number)} "
            f"(issue {issue_ref(issue_number)}) WITHOUT producing a new commit on "
            f"branch `{pr_head_branch}`. {remote_block}:\n\n"
            f"{failing_block}\n\n"
            f"{dirty_block}"
            f"Returning no commit when required checks are still red is itself a "
            f"bug; returning no commit after editing tracked files is also a bug. "
            f"Fix the code so the failing checks pass and the PR can merge. If no "
            f"code fix is possible, DO NOT commit a 'blocker' file: a new "
            f"Markdown/docs file will itself fail the repo's lint gates (e.g. "
            f"markdownlint) and turn one blocker into two. Instead leave the tree "
            f"unchanged and report the blocker via the `BLOCKED:` line below — do "
            f"NOT commit any file to document it.\n\n"
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
        """Persist a marker for the next ecosystem run (#846)."""
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
        """Re-invoke the agent up to ``max_retries`` times after a no-commit turn (#846).

        Returns:
            True if the retry produced a new commit (caller should push).
            False if CI is green now, the retry returned no commit again, or
            any error path fired.

        """
        failing: list[str] = []
        for retry in range(1, max_retries + 1):
            failing = self._failing_required_check_names_fn(pr_number)
            dirty_tracked_changes = self._tracked_worktree_changes_fn(worktree_path, issue_number)
            if not failing and not dirty_tracked_changes:
                logger.info(
                    "Issue #%s: no-commit turn but PR #%s has no failing required checks "
                    "and no tracked worktree changes; skipping force-engagement retry",
                    issue_number,
                    pr_number,
                )
                return False

            review_threads_block = self._format_review_threads_block_fn(pr_number)
            retry_prompt = self._force_engagement_prompt(
                issue_number=issue_number,
                pr_number=pr_number,
                worktree_path=worktree_path,
                pr_head_branch=pr_head_branch,
                failing_check_names=failing,
                dirty_tracked_changes=dirty_tracked_changes,
                review_threads_block=review_threads_block,
            )

            retry_reason = ", ".join(failing) if failing else "tracked worktree changes"
            logger.warning(
                "Issue #%s: no-commit on CI fix turn; re-invoking with "
                "force-engagement prompt (retry %s/%s, reason: %s)",
                issue_number,
                retry,
                max_retries,
                retry_reason,
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
        """Return True iff HEAD has moved past ``pre_agent_sha`` after the agent ran."""
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

    def _git_stdout_for_push_guard(
        self,
        worktree_path: Path,
        issue_number: int,
        argv: list[str],
        failure_message: str,
    ) -> str | None:
        """Run a git inspection command for the CI pre-push guard."""
        try:
            result = run(argv, cwd=worktree_path, capture_output=True, check=False)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: %s: %s",
                issue_number,
                failure_message,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return None
        if result.returncode != 0:
            logger.error(
                "Issue #%s: %s: %s",
                issue_number,
                failure_message,
                (result.stderr or result.stdout or "")[:300],
            )
            return None
        return result.stdout or ""

    def _ci_fix_head_is_pushable(
        self,
        worktree_path: Path,
        issue_number: int,
        *,
        base_ref: str = "origin/main",
    ) -> bool:
        """Return True when the post-agent worktree is safe to push."""
        unmerged = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "diff", "--name-only", "--diff-filter=U"],
            "failed to inspect merge state before push",
        )
        if unmerged is None:
            return False
        unmerged_paths = [line for line in unmerged.splitlines() if line.strip()]
        if unmerged_paths:
            logger.error(
                "Issue #%s: refusing to push CI fix with unresolved merge paths: %s",
                issue_number,
                ", ".join(unmerged_paths[:10]),
            )
            return False

        status = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "status", "--porcelain"],
            "failed to inspect worktree status before push",
        )
        if status is None:
            return False
        tracked_dirty = [
            line for line in status.splitlines() if line.strip() and not line.startswith("?? ")
        ]
        if tracked_dirty:
            logger.error(
                "Issue #%s: refusing to push CI fix with uncommitted tracked changes: %s",
                issue_number,
                ", ".join(tracked_dirty[:10]),
            )
            return False

        ahead = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            f"failed to inspect HEAD ahead of {base_ref} before push",
        )
        if ahead is None:
            return False
        try:
            ahead_count = int(ahead.strip() or "0")
        except ValueError:
            logger.error(
                "Issue #%s: refusing to push CI fix with invalid ahead count: %r",
                issue_number,
                ahead,
            )
            return False
        if ahead_count <= 0:
            logger.error(
                "Issue #%s: refusing to push CI fix because HEAD has no commits ahead of %s",
                issue_number,
                base_ref,
            )
            return False
        return True

    def _run_ci_fix_session(  # noqa: C901
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
            advise_findings: Prior learnings from the advise step.
            pr_head_branch: The PR's head-branch name on the remote.

        Returns:
            True if the fix session succeeded and the branch was pushed.

        """
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
        review_threads_block = self._format_review_threads_block_fn(pr_number)
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

                if not self._head_advanced(worktree_path, pre_agent_sha, issue_number):  # noqa: SIM102
                    if not self._retry_no_commit_once(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        worktree_path=worktree_path,
                        pr_head_branch=pr_head_branch,
                        pre_agent_sha=pre_agent_sha,
                        session_id=session_id,
                    ):
                        return False
                if not self._ci_fix_head_is_pushable(worktree_path, issue_number):
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
                if not self._head_advanced(worktree_path, pre_agent_sha, issue_number):  # noqa: SIM102
                    if not self._retry_no_commit_once(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        worktree_path=worktree_path,
                        pr_head_branch=pr_head_branch,
                        pre_agent_sha=pre_agent_sha,
                        session_id=session_id,
                    ):
                        return False
                if not self._ci_fix_head_is_pushable(worktree_path, issue_number):
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
