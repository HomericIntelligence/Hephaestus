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
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    reject_pi_unsupported_surface,
    resume_agent_session,
    run_agent_session,
    uses_direct_agent_runner,
)

from .ci_fix_push_guard import CIFixPushGuard
from .ci_fix_sessions import CIFixSessions
from .claude_invoke import invoke_claude_with_session
from .claude_models import implementer_model
from .git_utils import (  # noqa: F401
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

# Pre-push CI-fix test gate (#2122): re-run the failing tests parsed from the CI
# logs before force-pushing so the mesh can never push a branch that still fails
# the exact test it claims to fix (root cause of PR #2056's stranding).
_FAILED_TEST_LINE_RE = re.compile(r"(?:^|\s)(?:FAILED|ERROR)\s+(tests/[\w./-]+\.py(?:::[\w./-]+)*)")
_AFFECTED_TESTS_TIMEOUT_SECONDS = 900


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


class CIFixOrchestrator(CIFixSessions, CIFixPushGuard):
    """Compatibility façade over CI-fix session and push collaborators."""

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
        reject_pi_unsupported_surface(
            options.agent,
            "legacy CI-fix raw session resume is N/A; Pi never falls back to a fresh session",
        )
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
    reject_pi_unsupported_surface(
        options.agent,
        "legacy CI-fix raw session resume is N/A; Pi never falls back to a fresh session",
    )
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
