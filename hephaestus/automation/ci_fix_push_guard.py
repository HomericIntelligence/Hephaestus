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

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .ci_fix_contract import _CIFixHost

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

# Pre-push CI-fix test gate (#2122): re-run the failing tests parsed from the CI
# logs before force-pushing so the mesh can never push a branch that still fails
# the exact test it claims to fix (root cause of PR #2056's stranding).
_FAILED_TEST_LINE_RE = re.compile(r"(?:^|\s)(?:FAILED|ERROR)\s+(tests/[\w./-]+\.py(?:::[\w./-]+)*)")
_AFFECTED_TESTS_TIMEOUT_SECONDS = 900
_PYTEST_FAILURE_REASONS: dict[int, str] = {
    1: "pytest exit code 1: tests failed",
    2: "pytest exit code 2: test execution interrupted",
    3: "pytest exit code 3: internal error",
    4: "pytest exit code 4: command-line usage error",
    5: "pytest exit code 5: no tests collected",
}


def extract_failing_pytest_node_ids(ci_logs: str) -> list[str]:
    """Parse failing pytest node IDs from CI failure logs (pure; unit-tested).

    Scans ``FAILED``/``ERROR`` lines for ``tests/...py[::...]`` node IDs,
    de-duplicating while preserving first-seen order. Parametrized IDs are
    truncated at ``[`` so the whole test function runs — a safe superset that
    survives param renames across a rebase and avoids "unknown node id" errors.

    Args:
        ci_logs: Combined CI failure log text.

    Returns:
        Ordered, de-duplicated list of pytest node IDs (params stripped).

    """
    seen: dict[str, None] = {}
    for match in _FAILED_TEST_LINE_RE.finditer(ci_logs):
        seen.setdefault(match.group(1).split("[", 1)[0])
    return list(seen)


def _porcelain_path(line: str) -> str:
    """Return the target path from one ``git status --porcelain`` line."""
    status = line[:2]
    filename_part = line[3:] if len(line) > 3 else ""
    if status.startswith("R") and " -> " in filename_part:
        filename_part = filename_part.split(" -> ", 1)[1]
    if filename_part.startswith('"') and filename_part.endswith('"'):
        filename_part = filename_part[1:-1]
    return filename_part


def _legacy(name: str) -> Any:
    """Resolve a compatibility hook from the façade at call time."""
    return getattr(sys.modules["hephaestus.automation.ci_fix_orchestrator"], name)


class _LegacyCallable:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _legacy(self.name)(*args, **kwargs)


run = _LegacyCallable("run")
commit_if_changes = _LegacyCallable("commit_if_changes")
ensure_branch_commit_metadata = _LegacyCallable("ensure_branch_commit_metadata")
push_current_branch_with_lease_on_divergence = _LegacyCallable(
    "push_current_branch_with_lease_on_divergence"
)


class CIFixPushGuard(_CIFixHost):
    """Own the CI-fix head, test, metadata, and push safety contract."""

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
        ci_logs: str = "",
    ) -> bool:
        """Check head advancement, retry if needed, then push CI fixes.

        Shared post-agent contract for both codex and claude providers (#846).
        Returns True if fixes were pushed, False on any failure or no-commit.

        ``ci_logs`` feeds the pre-push test gate (#2122): the failing pytest
        node IDs parsed from it are re-run in the worktree and the push is
        refused if they still fail.
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
        if not self._affected_tests_pass(worktree_path, issue_number, ci_logs):
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

    def _affected_tests_pass(self, worktree_path: Path, issue_number: int, ci_logs: str) -> bool:
        """Re-run the CI-failing tests locally before allowing a push (#2122).

        The gate is skipped before invocation only when the CI logs contain no
        runnable node IDs after missing files are filtered. Once pytest is
        invoked, only exit code 0 passes.

        Args:
            worktree_path: Worktree the fix branch is checked out in.
            issue_number: GitHub issue number (for logging).
            ci_logs: Combined CI failure log text.

        Returns:
            True if the affected tests pass or the gate does not apply, False
            for every nonzero pytest result or timeout.

        """
        node_ids = [
            n
            for n in extract_failing_pytest_node_ids(ci_logs)
            if (worktree_path / n.split("::", 1)[0]).exists()
        ]
        if not node_ids:
            logger.info(
                "Issue #%s: no runnable failing pytest node IDs in CI logs; "
                "skipping pre-push test gate",
                issue_number,
            )
            return True
        try:
            result = subprocess.run(
                ["uv", "run", "python", "-m", "pytest", "-q", "--no-header", *node_ids],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=_AFFECTED_TESTS_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("Issue #%s: pre-push test gate timed out; refusing to push", issue_number)
            return False
        if result.returncode != 0:
            reason = (
                f"pytest terminated by signal {-result.returncode}"
                if result.returncode < 0
                else _PYTEST_FAILURE_REASONS.get(
                    result.returncode,
                    f"pytest exited with unexpected code {result.returncode}",
                )
            )
            logger.error(
                "Issue #%s: pre-push test gate failed (%s); refusing to push: %s",
                issue_number,
                reason,
                (result.stdout or result.stderr or "")[-500:],
            )
            return False
        return True

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
            return cast(
                bool,
                commit_if_changes(
                    issue_number,
                    worktree_path,
                    self._options().agent,
                    committed_log_message="Committed CI-fix residual changes for issue #%s",
                    allowed_paths=tracked_paths,
                ),
            )
        except Exception as exc:
            logger.error("Issue #%s: failed to commit CI-fix residuals: %s", issue_number, exc)
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
