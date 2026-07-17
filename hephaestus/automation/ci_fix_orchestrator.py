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
from hephaestus.automation.prompts.catalog import PromptCatalog
from hephaestus.io.utils import write_secure

from .claude_invoke import invoke_claude_with_session
from .claude_models import implementer_model
from .git_utils import (
    commit_if_changes,
    ensure_branch_commit_metadata,
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
_UNMERGED_STATUS_CODES: frozenset[str] = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _porcelain_path(line: str) -> str:
    """Return the target path from one ``git status --porcelain`` line."""
    status = line[:2]
    filename_part = line[3:] if len(line) > 3 else ""
    if status.startswith("R") and " -> " in filename_part:
        filename_part = filename_part.split(" -> ", 1)[1]
    if filename_part.startswith('"') and filename_part.endswith('"'):
        filename_part = filename_part[1:-1]
    return filename_part


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
        return PromptCatalog.current().render(
            "ci/force_engagement.j2",
            review_threads_block=review_threads_block,
            pr_ref=pr_ref(pr_number),
            issue_ref=issue_ref(issue_number),
            pr_head_branch=pr_head_branch,
            remote_block=remote_block,
            failing_block=failing_block,
            dirty_block=dirty_block,
            worktree_path=worktree_path,
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
            write_secure(
                marker,
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
                + "\n",
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

        ``CalledProcessError`` is converted to a non-zero result; timeout
        still propagates so callers can log it distinctly.
        """
        return _invoke_agent_session(
            self,
            prompt=prompt,
            session_id=session_id,
            worktree_path=worktree_path,
            issue_number=issue_number,
            pr_number=pr_number,
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
        pr_base_branch: str = "main",
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
        base_ref = f"origin/{pr_base_branch}"
        if not self._ci_fix_head_is_pushable(worktree_path, issue_number, base_ref=base_ref):
            if not self._ci_fix_residual_commit_is_safe(
                worktree_path=worktree_path,
                issue_number=issue_number,
                base_ref=base_ref,
            ):
                return False
            if not self._commit_residual_ci_fix_changes(
                worktree_path=worktree_path,
                issue_number=issue_number,
            ):
                return False
            if not self._ci_fix_head_is_pushable(worktree_path, issue_number, base_ref=base_ref):
                return False
        try:
            ensure_branch_commit_metadata(worktree_path, base_branch=pr_base_branch)
        except Exception as metadata_err:
            logger.error(
                "Issue #%s: failed to enforce signed+DCO commit metadata before push: %s",
                issue_number,
                metadata_err,
            )
            return False
        if not self._ci_fix_head_is_pushable(worktree_path, issue_number, base_ref=base_ref):
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

    def _ci_fix_residual_commit_is_safe(
        self,
        *,
        worktree_path: Path,
        issue_number: int,
        base_ref: str = "origin/main",
    ) -> bool:
        """Return True when dirty tracked CI-fix leftovers may be committed."""
        unmerged = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "diff", "--name-only", "--diff-filter=U"],
            "failed to inspect merge state before residual commit",
        )
        if unmerged is None:
            return False
        unmerged_paths = [line for line in unmerged.splitlines() if line.strip()]
        if unmerged_paths:
            logger.error(
                "Issue #%s: refusing to commit CI-fix residuals with unresolved merge paths: %s",
                issue_number,
                ", ".join(unmerged_paths[:10]),
            )
            return False

        ahead = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            f"failed to inspect HEAD ahead of {base_ref} before residual commit",
        )
        if ahead is None:
            return False
        try:
            ahead_count = int(ahead.strip() or "0")
        except ValueError:
            logger.error(
                "Issue #%s: refusing to commit CI-fix residuals with invalid ahead count: %r",
                issue_number,
                ahead,
            )
            return False
        if ahead_count <= 0:
            logger.error(
                "Issue #%s: refusing to commit CI-fix residuals because HEAD has no commits "
                "ahead of %s",
                issue_number,
                base_ref,
            )
            return False
        return True

    def _commit_residual_ci_fix_changes(self, *, worktree_path: Path, issue_number: int) -> bool:
        """Commit resolved tracked leftovers from a CI-fix agent turn.

        A CI-fix session can advance HEAD and still leave tracked files dirty
        (for example ``MM`` after resolving conflicts). Those resolved changes
        are part of the fix and must be signed/DCO committed before push. True
        unmerged paths stay blocked: porcelain status codes containing ``U`` are
        unresolved conflict state, not committable residual work.
        """
        dirty_changes = self._tracked_worktree_changes(worktree_path, issue_number)
        if not dirty_changes:
            return False
        unmerged = [line for line in dirty_changes if line[:2] in _UNMERGED_STATUS_CODES]
        if unmerged:
            logger.error(
                "Issue #%s: refusing to commit CI-fix residuals with unresolved merge status: %s",
                issue_number,
                ", ".join(unmerged[:10]),
            )
            return False
        tracked_paths = tuple(
            path for line in dirty_changes if line[:2] != "??" and (path := _porcelain_path(line))
        )
        if not tracked_paths:
            logger.error(
                "Issue #%s: CI-fix residuals contained no tracked files to commit",
                issue_number,
            )
            return False
        try:
            return commit_if_changes(
                issue_number,
                worktree_path,
                self._options().agent,
                committed_log_message="Committed CI-fix residual changes for issue #%s",
                allowed_paths=tracked_paths,
            )
        except Exception as exc:
            logger.error("Issue #%s: failed to commit CI-fix residuals: %s", issue_number, exc)
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
        """Re-invoke the agent after a no-commit turn, then record repeat failures."""
        return _retry_no_commit_once(
            self,
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            pr_head_branch=pr_head_branch,
            pre_agent_sha=pre_agent_sha,
            session_id=session_id,
            max_retries=max_retries,
        )

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
        return PromptCatalog.current().render(
            "ci/fix.j2",
            advise_block=advise_block,
            review_threads_block=review_threads_block,
            pr_ref=pr_ref(pr_number),
            issue_ref=issue_ref(issue_number),
            worktree_path=worktree_path,
            pr_head_branch=pr_head_branch,
            failing_checks_block=failing_checks_block,
            ci_logs=ci_logs,
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
        pr_base_branch: str = "main",
    ) -> bool:
        """Invoke the selected coding agent to fix CI failures, then push the result.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            worktree_path: Path to the checked-out worktree.
            ci_logs: Combined CI failure log text.
            session_id: Optional agent session ID to resume.
            advise_findings: Prior learnings prepended to the prompt.
            pr_head_branch: Remote PR head branch the push targets, even if
                the agent switches branches mid-session (#832).
            pr_base_branch: PR base branch used for commit-metadata repair.

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
            pr_base_branch=pr_base_branch,
            session_id=session_id,
        )

    def attempt_mechanical_rebase(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
    ) -> bool:
        """Rebase a behind/conflicting PR onto its base branch with no agent (#871)."""
        return _attempt_mechanical_rebase(self, issue_number, pr_number, acquired_slot)

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


def _completed_process_from_error(
    exc: subprocess.CalledProcessError,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=exc.cmd,
        returncode=exc.returncode,
        stdout=exc.stdout or "",
        stderr=exc.stderr or "",
    )


def _completed_process_from_agent_result(result: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=result.stdout,
        stderr=result.stderr or "",
    )


def _run_fresh_direct_agent(
    options: Any,
    *,
    prompt: str,
    worktree_path: Path,
) -> subprocess.CompletedProcess[str]:
    try:
        result = run_agent_session(
            agent=options.agent,
            prompt=prompt,
            cwd=worktree_path,
            timeout=options.agent_timeout,
            model=direct_agent_model(options.agent, "HEPH_IMPLEMENTER_MODEL"),
            sandbox="workspace-write",
        )
    except subprocess.CalledProcessError as exc:
        return _completed_process_from_error(exc)
    return _completed_process_from_agent_result(result)


def _invoke_direct_agent_session(
    orchestrator: CIFixOrchestrator,
    *,
    prompt: str,
    session_id: str | None,
    worktree_path: Path,
    issue_number: int,
    pr_number: int,
) -> subprocess.CompletedProcess[str]:
    options = orchestrator._options()
    if not session_id:
        return _run_fresh_direct_agent(options, prompt=prompt, worktree_path=worktree_path)
    try:
        result = resume_agent_session(
            agent=options.agent,
            session_id=session_id,
            prompt=prompt,
            cwd=worktree_path,
            timeout=options.agent_timeout,
            model=direct_agent_model(options.agent, "HEPH_IMPLEMENTER_MODEL"),
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Issue #%s: %s resume session %r failed for PR #%s; falling back to fresh session: %s",
            issue_number,
            options.agent,
            session_id,
            pr_number,
            (exc.stderr or exc.stdout or "")[:300],
        )
        return _run_fresh_direct_agent(options, prompt=prompt, worktree_path=worktree_path)
    return _completed_process_from_agent_result(result)


def _invoke_claude_agent_session(
    orchestrator: CIFixOrchestrator,
    *,
    prompt: str,
    worktree_path: Path,
    issue_number: int,
) -> subprocess.CompletedProcess[str]:
    options = orchestrator._options()
    repo_slug = get_repo_slug(orchestrator._repo_root())
    try:
        stdout, _ = invoke_claude_with_session(
            repo=repo_slug,
            issue=issue_number,
            agent=AGENT_CI_DRIVER,
            prompt=prompt,
            model=implementer_model(),
            cwd=worktree_path,
            timeout=options.agent_timeout,
            output_format="json",
            allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
            input_via_stdin=True,
        )
    except subprocess.CalledProcessError as exc:
        return _completed_process_from_error(exc)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _invoke_agent_session(
    orchestrator: CIFixOrchestrator,
    *,
    prompt: str,
    session_id: str | None,
    worktree_path: Path,
    issue_number: int,
    pr_number: int,
) -> subprocess.CompletedProcess[str]:
    options = orchestrator._options()
    if uses_direct_agent_runner(options.agent):
        return _invoke_direct_agent_session(
            orchestrator,
            prompt=prompt,
            session_id=session_id,
            worktree_path=worktree_path,
            issue_number=issue_number,
            pr_number=pr_number,
        )
    return _invoke_claude_agent_session(
        orchestrator,
        prompt=prompt,
        worktree_path=worktree_path,
        issue_number=issue_number,
    )


def _retry_no_commit_once(
    orchestrator: CIFixOrchestrator,
    *,
    issue_number: int,
    pr_number: int,
    worktree_path: Path,
    pr_head_branch: str,
    pre_agent_sha: str,
    session_id: str | None,
    max_retries: int,
) -> bool:
    failing: list[str] = []
    for retry in range(1, max_retries + 1):
        failing = orchestrator._failing_required_check_names(pr_number)
        dirty_changes = orchestrator._tracked_worktree_changes(worktree_path, issue_number)
        if not failing and not dirty_changes:
            logger.info(
                "Issue #%s: no-commit turn but PR #%s has no failing required checks "
                "and no tracked worktree changes; skipping force-engagement retry",
                issue_number,
                pr_number,
            )
            return False

        retry_prompt = orchestrator.force_engagement_prompt(
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            pr_head_branch=pr_head_branch,
            failing_check_names=failing,
            dirty_tracked_changes=dirty_changes,
            review_threads_block=orchestrator._format_review_threads_block(pr_number),
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
            retry_result = orchestrator.invoke_agent_session(
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
        if orchestrator._head_advanced(worktree_path, pre_agent_sha, issue_number):
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
    orchestrator.record_repeated_no_commit(
        issue_number=issue_number,
        pr_number=pr_number,
        pr_head_branch=pr_head_branch,
        failing_check_names=failing,
    )
    return False


def _fetch_rebase_state(issue_number: int, pr_number: int) -> dict[str, Any] | None:
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
        return dict(json.loads(result.stdout or "{}"))
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.warning(
            "Issue #%s: could not fetch PR #%s merge state for rebase; "
            "skipping mechanical rebase: %s",
            issue_number,
            pr_number,
            exc,
        )
        return None


def _rebase_state_is_actionable(
    orchestrator: CIFixOrchestrator,
    *,
    issue_number: int,
    pr_number: int,
    merge_state: str,
) -> bool:
    rebase_states = ("BEHIND", "DIRTY", "CONFLICTING")
    if merge_state != "BLOCKED":
        return merge_state in rebase_states
    failing_checks = orchestrator._failing_required_check_names(pr_number)
    if not failing_checks:
        return False
    logger.info(
        "Issue #%s: PR #%s is BLOCKED with failing required checks (%s); "
        "attempting mechanical rebase before CI-fix agent",
        issue_number,
        pr_number,
        ", ".join(failing_checks),
    )
    return True


def _attempt_mechanical_rebase(
    orchestrator: CIFixOrchestrator,
    issue_number: int,
    pr_number: int,
    acquired_slot: int,
) -> bool:
    state = _fetch_rebase_state(issue_number, pr_number)
    if state is None:
        return False
    merge_state = str(state.get("mergeStateStatus") or "").upper()
    if not _rebase_state_is_actionable(
        orchestrator,
        issue_number=issue_number,
        pr_number=pr_number,
        merge_state=merge_state,
    ):
        return False

    pr_head_branch = str(state.get("headRefName") or "") or orchestrator._get_pr_branch(pr_number)
    base_branch = str(state.get("baseRefName") or "main") or "main"
    if not pr_head_branch:
        logger.warning(
            "Issue #%s: PR #%s has no resolvable head branch; skipping rebase",
            issue_number,
            pr_number,
        )
        return False
    orchestrator._status().update_slot(
        acquired_slot,
        f"{issue_ref(issue_number)}: mechanical rebase onto {base_branch}",
    )
    try:
        worktree_path = orchestrator._get_worktree_path(issue_number, pr_number)
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
