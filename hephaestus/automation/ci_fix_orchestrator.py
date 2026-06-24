"""CI fix session orchestrator extracted from CIDriver (refs #1179, #1289).

Owns the methods that run coding-agent sessions to fix CI failures:
- agent session dispatch (codex/claude) and the post-agent push contract (#846)
- force-engagement prompts and no-commit retry logic (#846)
- the main CI fix session (prompt build + invoke + push)
- mechanical rebase (no-agent fast path, #871)

Receives narrow ``Callable`` providers for the shared state and back-called
CIDriver methods instead of the full ``CIDriver`` to satisfy DIP and avoid
bidirectional coupling (refs #1179 MAJOR finding 2). The construction site wraps
the injected callables in lambdas so ``patch.object`` on the CIDriver method
continues to intercept through the indirection in tests.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    resume_agent_session,
    run_agent_session,
    uses_direct_agent_runner,
)

from .claude_invoke import invoke_claude_with_session
from .claude_models import implementer_model
from .claude_timeouts import ci_driver_claude_timeout
from .git_utils import (
    get_repo_slug,
    issue_ref,
    pr_ref,
    push_current_branch_with_lease_on_divergence,
    rebase_worktree_onto,
    run,
    sync_worktree_to_remote_branch,
)
from .github_api import _gh_call
from .session_naming import AGENT_CI_DRIVER

logger = logging.getLogger(__name__)

_ACTIONABLE_UNTRACKED_PREFIXES: tuple[str, ...] = (
    ".github/",
    "docs/",
    "hephaestus/",
    "scripts/",
    "skills/",
    "tests/",
)
_ACTIONABLE_UNTRACKED_SUFFIXES: tuple[str, ...] = (
    ".cfg",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
)
_IGNORED_UNTRACKED_PATHS: frozenset[str] = frozenset(
    {
        ".coverage",
        "coverage.xml",
        "uv.lock",
    }
)
_IGNORED_UNTRACKED_PREFIXES: tuple[str, ...] = (
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tox/",
    "build/",
    "dist/",
    "htmlcov/",
)


class CIFixOrchestrator:
    """Orchestrates CI fix sessions using narrow-callable injection.

    Receives all back-called CIDriver methods as callables instead of the
    full CIDriver to satisfy DIP and avoid bidirectional coupling
    (refs #1179 MAJOR finding 2).
    """

    def __init__(
        self,
        *,
        options_provider: Callable[[], Any],
        repo_root_provider: Callable[[], Path],
        state_dir_provider: Callable[[], Path],
        status_tracker_provider: Callable[[], Any],
        get_pr_branch: Callable[[int], str],
        get_worktree_path: Callable[[int, int], Path],
        format_review_threads_block: Callable[[int], str],
        failing_required_check_names: Callable[[int], list[str]],
    ) -> None:
        """Initialise the orchestrator with narrow provider callables.

        The post-agent push-safety guard (``_head_advanced``,
        ``_ci_fix_head_is_pushable``, ``_tracked_worktree_changes``, and the
        shared ``_git_stdout_for_push_guard`` helper) lives on this class — it is
        part of the CI-fix push contract — so it is NOT injected (#1357).

        Args:
            options_provider: Returns the current CIDriverOptions.
            repo_root_provider: Returns the repo root Path.
            state_dir_provider: Returns the state directory Path.
            status_tracker_provider: Returns the current StatusTracker.
            get_pr_branch: Returns the head branch name for a PR number.
            get_worktree_path: Resolves the worktree path for (issue, pr).
            format_review_threads_block: Builds the review-threads prompt block.
            failing_required_check_names: Returns names of failing required checks.

        """
        self._options = options_provider
        self._repo_root = repo_root_provider
        self._state_dir = state_dir_provider
        self._status = status_tracker_provider
        self._get_pr_branch = get_pr_branch
        self._get_worktree_path = get_worktree_path
        self._format_review_threads_block = format_review_threads_block
        self._failing_required_check_names = failing_required_check_names

    def force_engagement_prompt(
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
        """Build the retry prompt when the agent returned without committing (#846).

        The retry must engage the agent enough to either (a) produce a real
        fix or (b) explicitly say why CI cannot pass / merge. The prompt names
        the failing checks and/or dirty tracked files verbatim, re-emphasises
        the existing PR/branch invariant, and re-emphasises signed commits — a
        no-commit retry is a contract violation that the agent has to address
        head-on.
        """
        failing_block = "\n".join(f"- {n}" for n in failing_check_names) or "- (unknown)"
        dirty_lines = dirty_tracked_changes or []
        dirty_block = "\n".join(f"- {line}" for line in dirty_lines)
        if dirty_block:
            dirty_block = (
                "\n\nThe local worktree also contains uncommitted tracked changes "
                "or relevant untracked files from the previous turn. Review this "
                "existing work first and either commit it after verification or "
                f"amend it before committing:\n\n{dirty_block}\n"
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

    def record_repeated_no_commit(
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
        marker = self._state_dir() / f"repeated-no-commit-{pr_number}.json"
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

    def invoke_agent_session(
        self,
        *,
        prompt: str,
        session_id: str | None,
        worktree_path: Path,
        issue_number: int,
        pr_number: int,
    ) -> subprocess.CompletedProcess[str]:
        """Dispatch a prompt to the configured agent (codex or claude).

        Returns a CompletedProcess whose returncode signals success or failure:
        - returncode == 0: agent ran successfully (stdout has output)
        - returncode != 0: agent failed (stderr has details)

        For codex, returncode is synthetic — AgentRunResult has no returncode
        field; success is "no CalledProcessError" (hephaestus/agents/runtime.py).
        For claude, returncode comes from the subprocess exit code.

        CalledProcessError is absorbed from all codex paths into a non-zero
        CompletedProcess. TimeoutExpired propagates to the caller so it can
        log a distinct timeout message.
        """
        options = self._options()
        if uses_direct_agent_runner(options.agent):
            if session_id:
                try:
                    result = resume_agent_session(
                        agent=options.agent,
                        session_id=session_id,
                        prompt=prompt,
                        cwd=worktree_path,
                        timeout=ci_driver_claude_timeout(),
                        model=direct_agent_model(options.agent, "HEPH_IMPLEMENTER_MODEL"),
                    )
                except subprocess.CalledProcessError as exc:
                    logger.warning(
                        "Issue #%s: %s resume session %r failed for PR #%s; "
                        "falling back to fresh session: %s",
                        issue_number,
                        options.agent,
                        session_id,
                        pr_number,
                        (exc.stderr or exc.stdout or "")[:300],
                    )
                    try:
                        result = run_agent_session(
                            agent=options.agent,
                            prompt=prompt,
                            cwd=worktree_path,
                            timeout=ci_driver_claude_timeout(),
                            model=direct_agent_model(options.agent, "HEPH_IMPLEMENTER_MODEL"),
                            sandbox="workspace-write",
                        )
                    except subprocess.CalledProcessError as fresh_exc:
                        return subprocess.CompletedProcess(
                            args=fresh_exc.cmd,
                            returncode=fresh_exc.returncode,
                            stdout=fresh_exc.stdout or "",
                            stderr=fresh_exc.stderr or "",
                        )
            else:
                try:
                    result = run_agent_session(
                        agent=options.agent,
                        prompt=prompt,
                        cwd=worktree_path,
                        timeout=ci_driver_claude_timeout(),
                        model=direct_agent_model(options.agent, "HEPH_IMPLEMENTER_MODEL"),
                        sandbox="workspace-write",
                    )
                except subprocess.CalledProcessError as exc:
                    return subprocess.CompletedProcess(
                        args=exc.cmd,
                        returncode=exc.returncode,
                        stdout=exc.stdout or "",
                        stderr=exc.stderr or "",
                    )
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=result.stdout, stderr=result.stderr or ""
            )

        repo_slug = get_repo_slug(self._repo_root())
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
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
        except subprocess.CalledProcessError as exc:
            return subprocess.CompletedProcess(
                args=exc.cmd,
                returncode=exc.returncode,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            )

    def push_ci_fix(
        self,
        *,
        worktree_path: Path,
        pre_agent_sha: str,
        issue_number: int,
        pr_number: int,
        pr_head_branch: str,
        session_id: str | None,
    ) -> bool:
        """Check head advancement, retry if needed, then push CI fixes.

        Shared post-agent contract for both codex and claude providers (#846).
        Returns True if fixes were pushed, False on any failure or no-commit.
        """
        if not self._head_advanced(worktree_path, pre_agent_sha, issue_number):  # noqa: SIM102
            if not self.retry_no_commit_once(
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
            logger.error("Issue #%s: git push failed after CI fix: %s", issue_number, push_err)
            return False

    def retry_no_commit_once(
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
            max_retries: Maximum number of force-engagement retries.

        Returns:
            True if the retry produced a new commit (caller should push).
            False if CI is green now, the retry returned no commit again, or
            any error path fired. On repeated-no-commit, writes a forensics
            marker to ``state_dir``.

        """
        failing: list[str] = []
        for retry in range(1, max_retries + 1):
            failing = self._failing_required_check_names(pr_number)
            dirty_tracked_changes = self._tracked_worktree_changes(worktree_path, issue_number)
            if not failing and not dirty_tracked_changes:
                logger.info(
                    "Issue #%s: no-commit turn but PR #%s has no failing required checks "
                    "and no tracked worktree changes; skipping force-engagement retry",
                    issue_number,
                    pr_number,
                )
                return False

            review_threads_block = self._format_review_threads_block(pr_number)
            retry_prompt = self.force_engagement_prompt(
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
                retry_result = self.invoke_agent_session(
                    prompt=retry_prompt,
                    session_id=session_id,
                    worktree_path=worktree_path,
                    issue_number=issue_number,
                    pr_number=pr_number,
                )
            except subprocess.TimeoutExpired:
                logger.error(
                    "Issue #%s: no-commit retry session timed out for PR #%s",
                    issue_number,
                    pr_number,
                )
                return False

            if retry_result.returncode != 0:
                logger.error(
                    "Issue #%s: no-commit retry session failed for PR #%s: %s",
                    issue_number,
                    pr_number,
                    (retry_result.stderr or "")[:300],
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
        self.record_repeated_no_commit(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            failing_check_names=failing,
        )
        return False

    def sync_worktree_and_snapshot_sha(
        self, issue_number: int, worktree_path: Path, pr_head_branch: str
    ) -> str | None:
        """Sync worktree to remote PR head and snapshot HEAD SHA.

        Returns the pre-agent SHA string, or ``None`` on any subprocess failure
        (caller should return ``False`` immediately).

        Syncing before the agent prevents the agent from committing on a stale
        base and failing the force-with-lease push (#832). Snapshotting the SHA
        lets the push helper detect sessions that return without committing (#836).
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
            return None
        try:
            return run(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to snapshot HEAD before CI fix session: %s",
                issue_number,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return None

    def build_ci_fix_prompt(
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        ci_logs: str,
        pr_head_branch: str,
        advise_findings: str,
    ) -> str:
        """Build the CI-fix agent prompt string."""
        advise_block = ""
        findings = advise_findings.strip()
        if findings and not findings.startswith("<!-- advise step skipped"):
            advise_block = f"## Prior Learnings from Team Knowledge Base\n\n{findings}\n\n---\n\n"
        failing_check_names = self._failing_required_check_names(pr_number)
        failing_checks_block = ""
        if failing_check_names:
            failing_lines = "\n".join(f"- {name}" for name in failing_check_names)
            aggregate_note = ""
            if "required-checks-gate" in failing_check_names:
                aggregate_note = (
                    "\n\n`required-checks-gate` is an aggregate fan-in check. "
                    "Fix the underlying failed job(s) named above and in the logs; "
                    "do not try to patch the aggregate gate unless its own code is "
                    "the direct failure."
                )
            failing_checks_block = (
                f"Failing checks reported by GitHub:\n{failing_lines}{aggregate_note}\n\n"
            )
        review_threads_block = self._format_review_threads_block(pr_number)
        return (
            f"{advise_block}{review_threads_block}"
            f"Fix the CI failures for PR {pr_ref(pr_number)} (issue {issue_ref(issue_number)}).\n\n"
            f"Working directory: {worktree_path}\n"
            f"Current branch (DO NOT change): {pr_head_branch}\n\n"
            f"{failing_checks_block}"
            f"CI failure logs:\n{ci_logs}\n\n"
            "Fix only the code, workflow, commit metadata, or PR metadata needed "
            "to make the listed CI checks pass; do not implement unrelated issue "
            "work. If the fix requires new files, add them to git explicitly. "
            "After fixing:\n"
            "1. Run: pixi run python -m pytest tests/ -v\n"
            "2. Run: pre-commit run --all-files\n"
            "3. Commit changes (do NOT push) on the current branch — DO NOT run "
            "`git checkout -b`, `git switch -c`, or any other command that creates "
            "or switches to a different branch\n"
            "4. Every commit MUST be cryptographically signed (`git commit -S`); "
            "NEVER use `--no-verify`.\n\n"
            f"Commit message: fix: Address CI failures for PR {pr_ref(pr_number)}\n"
        )

    def run_ci_fix_session(
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
        pre_agent_sha = self.sync_worktree_and_snapshot_sha(
            issue_number, worktree_path, pr_head_branch
        )
        if pre_agent_sha is None:
            return False

        prompt = self.build_ci_fix_prompt(
            issue_number,
            pr_number,
            worktree_path,
            ci_logs,
            pr_head_branch,
            advise_findings,
        )

        try:
            agent_result = self.invoke_agent_session(
                prompt=prompt,
                session_id=session_id,
                worktree_path=worktree_path,
                issue_number=issue_number,
                pr_number=pr_number,
            )
        except subprocess.TimeoutExpired:
            logger.error("Issue #%s: CI fix session timed out for PR #%s", issue_number, pr_number)
            return False
        except Exception as e:
            logger.error(
                "Issue #%s: CI fix session failed for PR #%s: %s", issue_number, pr_number, e
            )
            return False

        if agent_result.returncode != 0:
            logger.error(
                "Issue #%s: CI fix session returned exit code %s: %s",
                issue_number,
                agent_result.returncode,
                (agent_result.stderr or "")[:300],
            )
            return False

        logger.debug("Issue #%s: CI fix output: %s", issue_number, agent_result.stdout[:500])
        return self.push_ci_fix(
            worktree_path=worktree_path,
            pre_agent_sha=pre_agent_sha,
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            session_id=session_id,
        )

    def attempt_mechanical_rebase(
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
           ``CONFLICTING``. ``BLOCKED`` is also eligible only when required
           checks are failing, because GitHub can report failed-check PRs as
           branch-protection blocked even when a base rebase can bring in the
           fix.
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
        # Only behind-base / conflicting PRs need a rebase. CLEAN / UNSTABLE /
        # HAS_HOOKS PRs are already on top of their base — let the check-status
        # path handle them. BLOCKED is usually review-gated, but GitHub also
        # reports some failed required-check PRs as BLOCKED; those are still
        # eligible for the cheap rebase path before invoking an agent.
        rebase_states = ("BEHIND", "DIRTY", "CONFLICTING")
        if merge_state == "BLOCKED":
            failing_checks = self._failing_required_check_names(pr_number)
            if not failing_checks:
                return False
            logger.info(
                "Issue #%s: PR #%s is BLOCKED with failing required checks (%s); "
                "attempting mechanical rebase before CI-fix agent",
                issue_number,
                pr_number,
                ", ".join(failing_checks),
            )
        elif merge_state not in rebase_states:
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

        self._status().update_slot(
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

    def _tracked_worktree_changes(self, worktree_path: Path, issue_number: int) -> list[str]:
        """Return actionable dirty status lines for a post-agent worktree.

        Tracked edits are always actionable. Untracked local tool output such
        as ``uv.lock`` / caches is intentionally ignored, while new files under
        source, script, workflow, docs, skills, and tests paths are surfaced so
        a no-commit retry can tell the agent to add and sign-commit them.
        """
        status = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "status", "--porcelain"],
            "failed to inspect worktree status for no-commit retry",
        )
        if status is None:
            return []
        return [
            line
            for line in status.splitlines()
            if line.strip() and self._status_line_needs_no_commit_retry(line)
        ]

    @staticmethod
    def _status_line_needs_no_commit_retry(line: str) -> bool:
        """Return whether a porcelain status line is actionable retry context."""
        if not line.startswith("?? "):
            return True
        path = line[3:].strip()
        if not path:
            return False
        if path in _IGNORED_UNTRACKED_PATHS:
            return False
        if path.startswith(_IGNORED_UNTRACKED_PREFIXES):
            return False
        if path.startswith(_ACTIONABLE_UNTRACKED_PREFIXES):
            return True
        return "/" not in path and path.endswith(_ACTIONABLE_UNTRACKED_SUFFIXES)

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
        """Return True when the post-agent worktree is safe to push.

        ``_head_advanced`` only proves HEAD changed. A conflict-resolution agent
        can still leave the index unmerged, leave tracked files uncommitted, or
        accidentally detach at the base branch itself. None of those states may
        be pushed to the PR head.
        """
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
