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
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from hephaestus.automation.prompts.catalog import PromptCatalog
from hephaestus.io.utils import write_secure

from .ci_fix_contract import _CIFixHost
from .git_utils import issue_ref, pr_ref

logger = logging.getLogger(__name__)

# Pre-push CI-fix test gate (#2122): re-run the failing tests parsed from the CI
# logs before force-pushing so the mesh can never push a branch that still fails
# the exact test it claims to fix (root cause of PR #2056's stranding).
_FAILED_TEST_LINE_RE = re.compile(r"(?:^|\s)(?:FAILED|ERROR)\s+(tests/[\w./-]+\.py(?:::[\w./-]+)*)")
# pytest exit code 5 = "no tests ran": the failing test may have been deleted by
# the fix/rebase itself (exactly the #2056 remedy) — not a gate failure.
_PYTEST_NO_TESTS_RAN = 5


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
rebase_worktree_onto = _LegacyCallable("rebase_worktree_onto")
sync_worktree_to_remote_branch = _LegacyCallable("sync_worktree_to_remote_branch")
_gh_call = _LegacyCallable("_gh_call")
_invoke_agent_session = _LegacyCallable("_invoke_agent_session")
_retry_no_commit_once = _LegacyCallable("_retry_no_commit_once")
_attempt_mechanical_rebase = _LegacyCallable("_attempt_mechanical_rebase")


class CIFixSessions(_CIFixHost):
    """Own agent-session prompts, retries, and mechanical rebase sessions."""

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
            dirty_block = PromptCatalog.current().render(
                "ci/dirty_worktree_block.j2", dirty_block=dirty_block
            )
        remote_block = (
            PromptCatalog.current().render("ci/remote_checks_failing.j2").strip()
            if failing_check_names
            else PromptCatalog.current().render("ci/remote_repair_needed.j2").strip()
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
                        "recorded_at": datetime.now(UTC).isoformat(),
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
        return cast(
            subprocess.CompletedProcess[str],
            _invoke_agent_session(
                self,
                prompt=prompt,
                session_id=session_id,
                worktree_path=worktree_path,
                issue_number=issue_number,
                pr_number=pr_number,
            ),
        )

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
        return cast(
            bool,
            _retry_no_commit_once(
                self,
                issue_number=issue_number,
                pr_number=pr_number,
                worktree_path=worktree_path,
                pr_head_branch=pr_head_branch,
                pre_agent_sha=pre_agent_sha,
                session_id=session_id,
                max_retries=max_retries,
            ),
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
            result = cast(
                subprocess.CompletedProcess[str],
                run(["git", "rev-parse", "HEAD"], cwd=worktree_path),
            )
            return result.stdout.strip()
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
            ci_logs=ci_logs,
        )

    def attempt_mechanical_rebase(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
    ) -> bool:
        """Rebase a behind/conflicting PR onto its base branch with no agent (#871)."""
        return cast(
            bool,
            _attempt_mechanical_rebase(self, issue_number, pr_number, acquired_slot),
        )
