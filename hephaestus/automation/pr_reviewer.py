"""Read-only PR review automation using the selected coding agent.

Provides:
- Parallel PR analysis across multiple issues
- Read-only two-phase workflow: analysis then inline comment posting
- Git worktree isolation per PR (for code reading only)
- State persistence and UI monitoring

This module does NOT commit, push, or fix code. Fixing is handled by
address_review.py, which the implementer runs as an in-loop step of the
implement stage (it is no longer a separate pipeline phase). This module is
also exposed as the standalone ``hephaestus-review-prs`` console script for
manual, out-of-band PR review.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import is_codex, resolve_agent, run_codex_text
from hephaestus.cli.utils import add_json_arg, emit_json_status

from ._review_utils import (
    build_review_parser,
    find_pr_for_issue,
    instance_log,
    parse_json_block,
    setup_review_logging,
)
from ._reviewer_base import BaseReviewer
from .claude_invoke import invoke_claude_with_session
from .claude_models import reviewer_model
from .claude_timeouts import pr_reviewer_claude_timeout

# Re-exports honor BaseReviewer's test-seam contract (#710); see
# BaseReviewer._PATCHABLE_DEPENDENCIES.
from .curses_ui import CursesUI, ThreadLogManager  # noqa: F401
from .git_utils import get_repo_info, get_repo_root, get_repo_slug, issue_ref, pr_ref
from .github_api import _gh_call, fetch_issue_info, gh_pr_review_post
from .models import ReviewerOptions, ReviewPhase, ReviewState, WorkerResult
from .prompts import get_pr_review_analysis_prompt
from .session_naming import AGENT_PR_REVIEWER, reviewer_agent
from .status_tracker import StatusTracker  # noqa: F401
from .worktree_manager import WorktreeManager  # noqa: F401

logger = logging.getLogger(__name__)


_SIGNING_GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      commits(first: 100) {
        nodes {
          commit {
            oid
            signature { isValid signer { login } }
          }
        }
      }
    }
  }
}
"""


def _fetch_signing_state(pr_number: int) -> list[dict[str, Any]]:
    """Fetch per-commit signing state for *pr_number* via the GitHub GraphQL API.

    The REST projection of ``gh pr view --json commits`` does NOT expose the
    ``signature`` subfield, so we go to GraphQL. Each returned element is a
    dict ``{"oid", "signature_valid", "signer"}`` matching the schema the
    reviewer prompt expects. A null GraphQL ``signature`` (unsigned commit)
    is coerced to ``signature_valid=False`` rather than dropped.

    Failures are returned as an empty list; the reviewer treats an empty
    signing-state as a policy NOGO, so the caller still surfaces the
    violation rather than silently passing.
    """
    try:
        owner, name = get_repo_info()
        result = _gh_call(
            [
                "api",
                "graphql",
                "-f",
                f"query={_SIGNING_GRAPHQL_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"pr={pr_number}",
            ],
        )
        data = json.loads(result.stdout or "{}")
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("commits", {})
            .get("nodes", [])
        )
    except Exception as exc:
        logger.warning("PR #%d: failed to fetch signing state via GraphQL: %s", pr_number, exc)
        return []

    out: list[dict[str, Any]] = []
    for node in nodes:
        commit = node.get("commit") or {}
        signature = commit.get("signature") or {}
        out.append(
            {
                "oid": commit.get("oid", ""),
                "signature_valid": bool(signature.get("isValid", False)),
                "signer": (signature.get("signer") or {}).get("login"),
            }
        )
    return out


def _parse_json_block(text: str) -> dict[str, Any]:
    """Extract the last ```json ... ``` block from an agent response.

    Thin wrapper around :func:`_review_utils.parse_json_block` kept for
    backward compatibility with existing callers and tests.

    Args:
        text: Agent response text

    Returns:
        Parsed dict with keys "comments" and "summary", or defaults if not found

    """
    return parse_json_block(text)


def run_pr_review_analysis(
    *,
    pr_number: int,
    issue_number: int,
    worktree_path: Path,
    context: dict[str, Any],
    agent: str,
    review_agent: str = AGENT_PR_REVIEWER,
    state_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a read-only reviewer session and return its parsed analysis.

    Shared core of the standalone ``PRReviewer._run_analysis_session`` and the
    in-loop implementer review step (Stage 2, #28). Builds the PR-review
    analysis prompt, invokes the selected reviewer agent (Claude or Codex), and
    returns the parsed ``{"comments", "summary"}`` dict. The reviewer prompt's
    strict rubric emits a ``Grade:`` / ``Verdict:`` line inside ``summary`` so
    callers can derive a verdict via
    :func:`~hephaestus.automation.claude_invoke.parse_review_verdict`.

    Args:
        pr_number: GitHub PR number being reviewed.
        issue_number: Linked GitHub issue number.
        worktree_path: Worktree CWD for the reviewer session (read-only usage).
        context: PR context dict (see :meth:`PRReviewer._gather_pr_context`).
        agent: Selected implementation agent (``"claude"`` or ``"codex"``);
            determines the runtime used to invoke the reviewer.
        review_agent: Session-naming agent token for the Claude path. Defaults
            to :data:`AGENT_PR_REVIEWER`; the in-loop caller passes a fresh
            per-iteration token (``reviewer_agent(AGENT_PR_REVIEWER, i)``).
        state_dir: Directory for the reviewer log file.
        dry_run: When True, skip the agent call and return a placeholder dict.

    Returns:
        Parsed analysis dict with ``"comments"`` and ``"summary"`` keys.

    """
    if dry_run:
        logger.info("[DRY RUN] Would run analysis session for PR #%s", pr_number)
        return {"comments": [], "summary": "[DRY RUN] analysis skipped"}

    prompt = get_pr_review_analysis_prompt(
        pr_number=pr_number,
        issue_number=issue_number,
        pr_diff=context.get("pr_diff", ""),
        issue_body=context.get("issue_body", ""),
        ci_status=context.get("ci_status", ""),
        pr_description=context.get("pr_description", ""),
        auto_merge_enabled=bool(context.get("auto_merge_enabled", False)),
        commits_signing_state=context.get("commits_signing_state") or [],
        # #1083: nitpicks are suppressed unless --nitpick threaded the flag into
        # the review context.
        include_nitpicks=bool(context.get("include_nitpicks", False)),
    )

    prompt_file = worktree_path / f".claude-pr-review-{issue_number}.md"
    prompt_file.write_text(prompt)

    log_file = state_dir / f"pr-review-analysis-{issue_number}.log"

    try:
        if is_codex(agent):
            result = run_codex_text(
                prompt,
                cwd=worktree_path,
                timeout=pr_reviewer_claude_timeout(),
                sandbox="read-only",
            )
            log_file.write_text(result.stdout or "")
            parsed = _parse_json_block(result.stdout or "")
            logger.info(
                "Analysis complete for PR #%s; found %s inline comment(s)",
                pr_number,
                len(parsed.get("comments", [])),
            )
            return parsed

        repo_root = get_repo_root()
        repo_slug = get_repo_slug(repo_root)
        stdout, _ = invoke_claude_with_session(
            repo=repo_slug,
            issue=issue_number,
            agent=review_agent,
            prompt=prompt,
            model=reviewer_model(),
            cwd=worktree_path,
            timeout=pr_reviewer_claude_timeout(),
            output_format="json",
            permission_mode="dontAsk",
            allowed_tools="Read,Glob,Grep",
            # Pipe the prompt via stdin, not argv: the PR-review prompt embeds the
            # full diff and can be tens of KB, which overflows ARG_MAX and raises
            # `[Errno 7] Argument list too long` when passed as a positional arg.
            # Matches the plan reviewer / address-review / ci_driver invocations.
            input_via_stdin=True,
        )
        log_file.write_text(stdout or "")

        # Extract the response text from Claude's JSON wrapper
        try:
            data = json.loads(stdout or "{}")
            response_text: str = data.get("result", stdout or "")
        except (json.JSONDecodeError, AttributeError):
            response_text = stdout or ""

        parsed = _parse_json_block(response_text)
        logger.info(
            "Analysis complete for PR #%s; found %s inline comment(s)",
            pr_number,
            len(parsed.get("comments", [])),
        )
        return parsed

    except subprocess.CalledProcessError as e:
        stdout = e.stdout or ""
        stderr = e.stderr or ""
        error_output = f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
        log_file.write_text(error_output)
        raise RuntimeError(
            f"Analysis session failed for PR {pr_ref(pr_number)}: {e.stderr or e.stdout}"
        ) from e
    except subprocess.TimeoutExpired as e:
        log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
        raise RuntimeError(f"Analysis session timed out for PR {pr_ref(pr_number)}") from e
    finally:
        with contextlib.suppress(Exception):
            prompt_file.unlink()


def gather_impl_review_context(
    *,
    pr_number: int,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
    plan_review_text: str,
    diff_text: str,
    include_nitpicks: bool = False,
) -> dict[str, Any]:
    """Assemble the PR-review context for an in-loop implementer review.

    Folds the implementer-loop inputs (TASK = issue title+body, PLAN,
    PLAN_REVIEW, and the impl diff) into the dict shape
    :func:`run_pr_review_analysis` expects. The PLAN and PLAN_REVIEW comment
    bodies are surfaced inside the ``issue_body`` field so the reviewer sees
    the full design context the implementer worked from (Stage 2, #28).

    Args:
        pr_number: GitHub PR number under review.
        issue_number: Linked GitHub issue number.
        issue_title: Issue title (the TASK summary).
        issue_body: Full issue body (the TASK detail).
        plan_text: The implementation PLAN comment body (or "" if absent).
        plan_review_text: The PLAN_REVIEW comment body (or "" if absent).
        diff_text: ``gh pr diff`` / cumulative branch diff for the impl.
        include_nitpicks: Forwarded into the context so the reviewer prompt
            emits nitpick-severity comments only when ``--nitpick`` is set
            (#1083).

    Returns:
        Context dict consumable by :func:`run_pr_review_analysis`.

    """
    task_block = f"**Issue Title:** {issue_title}\n\n{issue_body}".strip()
    plan_block = plan_text.strip() or "_(no plan comment found)_"
    plan_review_block = plan_review_text.strip() or "_(no plan-review comment found)_"
    composed_body = (
        f"{task_block}\n\n"
        f"---\n\n## PLAN\n\n{plan_block}\n\n"
        f"---\n\n## PLAN_REVIEW\n\n{plan_review_block}"
    )
    return {
        "pr_diff": diff_text or "",
        "issue_body": composed_body,
        "ci_status": "",
        "review_comments": "",
        "pr_description": "",
        "auto_merge_enabled": True,
        "commits_signing_state": [],
        "include_nitpicks": include_nitpicks,
    }


def review_pr_inline(
    *,
    pr_number: int,
    issue_number: int,
    worktree_path: Path,
    context: dict[str, Any],
    agent: str,
    iteration: int,
    state_dir: Path,
    dry_run: bool = False,
) -> tuple[str, list[str]]:
    """Review an impl PR in-loop: run analysis, post inline threads, return verdict.

    This is the in-loop equivalent of ``PRReviewer._review_pr`` used by the
    Stage 2 implementer session (#28). It runs a FRESH reviewer session per
    iteration (``reviewer_agent(AGENT_PR_REVIEWER, iteration)``) so the reviewer
    never inherits its own prior verdict, posts the analysis findings as inline
    PR review threads via :func:`gh_pr_review_post`, and returns the reviewer's
    summary text (carrying the ``Grade:`` / ``Verdict:`` line) plus the IDs of
    the threads it created.

    Args:
        pr_number: GitHub PR number to review.
        issue_number: Linked GitHub issue number.
        worktree_path: Worktree CWD for the reviewer session.
        context: PR context dict (see :func:`gather_impl_review_context`).
        agent: Selected implementation agent (``"claude"`` / ``"codex"``).
        iteration: Zero-based review-loop iteration (selects the fresh token).
        state_dir: Directory for the reviewer log file.
        dry_run: When True, skip the agent call and posting.

    Returns:
        ``(summary_text, posted_thread_ids)``. On dry-run, returns the
        placeholder summary and an empty list.

    """
    review_token = reviewer_agent(AGENT_PR_REVIEWER, iteration)
    analysis = run_pr_review_analysis(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        context=context,
        agent=agent,
        review_agent=review_token,
        state_dir=state_dir,
        dry_run=dry_run,
    )
    comments: list[dict[str, Any]] = analysis.get("comments", [])
    summary: str = analysis.get("summary", "")

    if dry_run:
        logger.info(
            "[DRY RUN] Would post %s inline comment(s) on PR %s",
            len(comments),
            pr_ref(pr_number),
        )
        return summary, []

    thread_ids = gh_pr_review_post(
        pr_number=pr_number,
        comments=comments,
        summary=summary,
        dry_run=False,
        # #1083: a later review iteration commenting on a line an earlier
        # iteration already flagged edits that comment instead of duplicating.
        dedupe_existing=True,
    )
    logger.info(
        "In-loop review R%s posted %s thread(s) on PR %s",
        iteration,
        len(thread_ids),
        pr_ref(pr_number),
    )
    return summary, thread_ids


class PRReviewer(BaseReviewer):
    """Posts inline review comments on open PRs linked to specified issues.

    Features:
    - Parallel PR analysis in isolated git worktrees (read-only)
    - Two-phase workflow: analysis session then inline comment posting
    - State persistence for observability
    - Real-time curses UI for status monitoring

    This class does NOT commit, push, or fix code.

    Inherits shared scaffolding (``__init__``, ``_log``, ``_fail``,
    ``_save_state``) from :class:`BaseReviewer`.
    """

    options: ReviewerOptions

    def __init__(self, options: ReviewerOptions):
        """Initialize PR reviewer.

        Args:
            options: Reviewer configuration options

        """
        super().__init__(options)

    def _log(self, level: str, msg: str, thread_id: int | None = None) -> None:
        """Log to both standard logger and UI thread buffer.

        Overrides :meth:`BaseReviewer._log` so the stdlib log record
        attributes to this module rather than ``_reviewer_base``.
        """
        instance_log(self.log_manager, level, msg, thread_id, caller_logger=logger)

    def run(self) -> dict[int, WorkerResult]:
        """Run the PR reviewer.

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        logger.info("Starting PR review for issues: %s", self.options.issues)

        # Discover PRs for all issues
        pr_map = self._discover_prs(self.options.issues)

        if not pr_map:
            logger.warning("No open PRs found for the specified issues")
            return {}

        logger.info("Found %s PR(s) to review: %s", len(pr_map), pr_map)

        # Start UI if enabled
        if not self.options.dry_run and self.options.enable_ui:
            self.ui = CursesUI(self.status_tracker, self.log_manager)
            self.ui.start()

        try:
            results = self._review_all(pr_map)
            return results
        finally:
            if self.ui:
                self.ui.stop()
            if not self.options.dry_run:
                self.worktree_manager.cleanup_all()

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:
        """Find open PRs linked to the given issue numbers.

        First tries branch name lookup ({issue}-auto-impl), then falls back
        to searching the PR body for the issue reference.

        Args:
            issue_numbers: List of issue numbers to find PRs for

        Returns:
            Mapping of issue_number -> pr_number for found PRs

        """
        pr_map: dict[int, int] = {}

        for issue_num in issue_numbers:
            pr_number = self._find_pr_for_issue(issue_num)
            if pr_number is not None:
                pr_map[issue_num] = pr_number
            else:
                logger.warning("No open PR found for issue #%s", issue_num)

        return pr_map

    def _find_pr_for_issue(self, issue_number: int) -> int | None:
        """Find the open PR for a single issue.

        Delegates to :func:`_review_utils.find_pr_for_issue` (two-strategy
        variant: branch-name lookup then body search).

        Args:
            issue_number: GitHub issue number

        Returns:
            PR number if found, None otherwise

        """
        return find_pr_for_issue(issue_number)

    def _gather_pr_context(
        self,
        pr_number: int,
        issue_number: int,
        worktree_path: Path,
    ) -> dict[str, Any]:
        """Gather all context needed for PR analysis.

        Fetches diff, CI status, existing comments, issue body, and policy
        state (auto-merge enabled? every commit signed?).

        Args:
            pr_number: GitHub PR number
            issue_number: Linked GitHub issue number
            worktree_path: Path to worktree (for cwd)

        Returns:
            Dictionary with keys: pr_diff, issue_body, ci_status,
            review_comments, pr_description, auto_merge_enabled,
            commits_signing_state.

        """
        context: dict[str, Any] = {
            "pr_diff": "",
            "issue_body": "",
            "ci_status": "",
            "review_comments": "",
            "pr_description": "",
            "auto_merge_enabled": False,
            "commits_signing_state": [],
        }

        # Fetch PR diff. This is the only field we treat as load-bearing —
        # an empty diff would let Claude emit "LGTM" against nothing. Failure
        # propagates so the worker is recorded as failed rather than silently
        # passing review.
        result = _gh_call(["pr", "diff", str(pr_number)], check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to fetch PR diff for #{pr_number}: "
                f"exit={result.returncode} stderr={(result.stderr or '')[:200]!r}"
            )
        context["pr_diff"] = (result.stdout or "")[:8000]  # Cap to avoid huge diffs
        if not context["pr_diff"].strip():
            raise RuntimeError(
                f"PR {pr_ref(pr_number)} returned an empty diff — refusing to review"
            )

        # Fetch PR description, reviews/comments, and policy state. Best-effort
        # for everything except policy state — but the reviewer prompt treats
        # an empty signing-state list as a NOGO, so a failure here surfaces
        # as a policy violation rather than silently passing.
        #
        # Note: `gh pr view --json commits` returns commit OIDs but NOT the
        # `signature` subfield. Per-commit signing state must come from the
        # GraphQL API (see ``_fetch_signing_state``); auto-merge and body
        # still come from the REST projection here.
        try:
            result = _gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "body,reviews,comments,autoMergeRequest",
                ],
            )
            pr_data = json.loads(result.stdout or "{}")
            context["pr_description"] = pr_data.get("body", "")

            # Policy state: auto-merge.
            context["auto_merge_enabled"] = pr_data.get("autoMergeRequest") is not None
            # Policy state: per-commit signing via GraphQL.
            context["commits_signing_state"] = _fetch_signing_state(pr_number)

            # Aggregate review comments
            review_parts: list[str] = []
            for review in pr_data.get("reviews", []):
                state = review.get("state", "")
                author = review.get("author", {}).get("login", "unknown")
                body = review.get("body", "")
                if body:
                    review_parts.append(f"[{state}] @{author}: {body}")
            for comment in pr_data.get("comments", []):
                author = comment.get("author", {}).get("login", "unknown")
                body = comment.get("body", "")
                if body:
                    review_parts.append(f"@{author}: {body}")
            context["review_comments"] = "\n".join(review_parts)
        except Exception as exc:
            logger.warning(
                "PR #%d: failed to gather description/comments/policy state: %s — "
                "review will proceed; missing policy state will trigger a NOGO verdict",
                pr_number,
                exc,
            )

        # Fetch CI check status (best-effort).
        try:
            result = _gh_call(
                ["pr", "checks", str(pr_number), "--json", "name,state,bucket"],
                check=False,
            )
            checks = json.loads(result.stdout or "[]")
            status_lines = [
                f"{c.get('name', '?')}: {c.get('bucket') or c.get('state', '?')}" for c in checks
            ]
            context["ci_status"] = "\n".join(status_lines)
        except Exception as exc:
            logger.warning(
                "PR #%d: failed to gather CI status: %s — review will proceed without it",
                pr_number,
                exc,
            )

        # Fetch issue body (best-effort).
        try:
            issue = fetch_issue_info(issue_number)
            context["issue_body"] = issue.body
        except Exception as exc:
            logger.warning(
                "Issue #%d: failed to fetch body for PR #%d review: %s",
                issue_number,
                pr_number,
                exc,
            )

        return context

    def _run_analysis_session(
        self,
        pr_number: int,
        issue_number: int,
        worktree_path: Path,
        context: dict[str, Any],
        slot_id: int | None = None,
    ) -> dict[str, Any]:
        """Run the read-only Claude analysis session to generate inline review comments.

        Args:
            pr_number: GitHub PR number
            issue_number: Linked issue number
            worktree_path: Path to worktree
            context: PR context from _gather_pr_context
            slot_id: Worker slot ID for status updates

        Returns:
            Parsed analysis result dict with keys "comments" and "summary"

        """
        return run_pr_review_analysis(
            pr_number=pr_number,
            issue_number=issue_number,
            worktree_path=worktree_path,
            context=context,
            agent=self.options.agent,
            review_agent=AGENT_PR_REVIEWER,
            state_dir=self.state_dir,
            dry_run=self.options.dry_run,
        )

    def _get_or_create_state(self, issue_number: int, pr_number: int) -> ReviewState:
        """Get or create review state for an issue.

        Checks the in-memory cache first, then falls back to the on-disk
        state file so that a second invocation of the reviewer on the same
        PR will find the previously-persisted COMPLETED state and skip
        re-posting comments (#374).

        A malformed or unreadable state file is treated as if it does not
        exist — the reviewer starts fresh and overwrites the bad file.

        Args:
            issue_number: GitHub issue number
            pr_number: GitHub PR number

        Returns:
            Existing or new ReviewState

        """
        with self.state_lock:
            if issue_number not in self.states:
                loaded = self._load_review_state_from_disk(issue_number)
                if loaded is not None:
                    self.states[issue_number] = loaded
                    logger.debug(
                        "Loaded review state for issue #%d from disk (phase=%s)",
                        issue_number,
                        loaded.phase,
                    )
                else:
                    self.states[issue_number] = ReviewState(
                        issue_number=issue_number,
                        pr_number=pr_number,
                    )
            return self.states[issue_number]

    def _review_pr(self, issue_number: int, pr_number: int) -> WorkerResult:
        """Analyze and post inline review comments for a single PR.

        Flow: ANALYZING -> POSTING -> COMPLETED (or FAILED at any step)

        Args:
            issue_number: GitHub issue number
            pr_number: GitHub PR number

        Returns:
            WorkerResult

        """
        slot_id = self.status_tracker.acquire_slot()
        if slot_id is None:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error="Failed to acquire worker slot",
            )

        thread_id = threading.get_ident()

        try:
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: PR {pr_ref(pr_number)} Creating worktree"
            )
            self._log(
                "info",
                f"Starting review of PR {pr_ref(pr_number)} for issue {issue_ref(issue_number)}",
                thread_id,
            )

            state = self._get_or_create_state(issue_number, pr_number)

            # Idempotency guard: skip if this PR was already fully reviewed (#374)
            if state.phase == ReviewPhase.COMPLETED:
                self._log(
                    "info",
                    f"PR {pr_ref(pr_number)} for issue {issue_ref(issue_number)} already reviewed "
                    "(state.phase=COMPLETED) — skipping to avoid duplicate comments",
                    thread_id,
                )
                self.status_tracker.update_slot(
                    slot_id, f"{issue_ref(issue_number)}: already reviewed, skipped"
                )
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    pr_number=pr_number,
                )

            # Create worktree on the PR branch (read-only usage)
            branch_name = f"{issue_number}-auto-impl"
            worktree_path = self.worktree_manager.create_worktree(issue_number, branch_name)

            with self.state_lock:
                state.worktree_path = str(worktree_path)
                state.branch_name = branch_name
            self._save_state(state)

            # Gather context
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: PR {pr_ref(pr_number)} Gathering context"
            )
            context = self._gather_pr_context(pr_number, issue_number, worktree_path)

            # Phase: ANALYZING — run Claude read-only analysis
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: PR {pr_ref(pr_number)} Analyzing"
            )
            with self.state_lock:
                state.phase = ReviewPhase.ANALYZING
            self._save_state(state)

            analysis = self._run_analysis_session(
                pr_number, issue_number, worktree_path, context, slot_id
            )

            comments: list[dict[str, Any]] = analysis.get("comments", [])
            summary: str = analysis.get("summary", "")

            # Phase: POSTING — post inline review comments to GitHub
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: PR {pr_ref(pr_number)} Posting"
            )
            with self.state_lock:
                state.phase = ReviewPhase.POSTING
            self._save_state(state)

            if self.options.dry_run:
                pref = pr_ref(pr_number)
                self._log(
                    "info",
                    f"[DRY RUN] Would post {len(comments)} inline comment(s) on PR {pref}",
                    thread_id,
                )
                thread_ids: list[str] = []
            else:
                thread_ids = gh_pr_review_post(
                    pr_number=pr_number,
                    comments=comments,
                    summary=summary,
                    dry_run=False,
                    # #1083: edit an existing comment on a line instead of
                    # duplicating it on re-review.
                    dedupe_existing=True,
                )
                self._log(
                    "info",
                    f"Posted {len(thread_ids)} review thread(s) on PR {pr_ref(pr_number)}",
                    thread_id,
                )

            with self.state_lock:
                state.posted_thread_ids = thread_ids
                state.phase = ReviewPhase.COMPLETED
                state.completed_at = datetime.now(timezone.utc)
            self._save_state(state)

            self._log(
                "info",
                f"PR {pr_ref(pr_number)} review complete for issue {issue_ref(issue_number)}",
                thread_id,
            )

            return WorkerResult(
                issue_number=issue_number,
                success=True,
                pr_number=pr_number,
                branch_name=branch_name,
                worktree_path=str(worktree_path),
            )

        except subprocess.TimeoutExpired as e:
            error_msg = f"Timeout: {' '.join(str(c) for c in e.cmd[:3])} exceeded {e.timeout}s"
            self._log("error", error_msg, thread_id)
            return self._fail(issue_number, error_msg, slot_id)

        except subprocess.CalledProcessError as e:
            error_msg = (
                f"Command failed (exit {e.returncode}): {' '.join(str(c) for c in e.cmd[:3])}"
            )
            self._log("error", error_msg, thread_id)
            return self._fail(issue_number, error_msg, slot_id)

        except RuntimeError as e:
            self._log("error", f"Runtime error: {e}", thread_id)
            return self._fail(issue_number, str(e)[:80], slot_id)

        except Exception as e:
            self._log("error", f"Unexpected {type(e).__name__}: {e}", thread_id)
            return self._fail(issue_number, str(e)[:80], slot_id)

        finally:
            time.sleep(1)
            self.status_tracker.release_slot(slot_id)

    def _review_all(self, pr_map: dict[int, int]) -> dict[int, WorkerResult]:
        """Review all PRs in parallel.

        Args:
            pr_map: Mapping of issue_number -> pr_number

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}

            # Submit all PRs upfront (no dependency ordering needed for review)
            for issue_num, pr_num in pr_map.items():
                future = executor.submit(self._review_pr, issue_num, pr_num)
                futures[future] = issue_num

            while futures:
                try:
                    done, _pending = wait(futures.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
                except Exception:
                    time.sleep(0.1)
                    continue

                for future in done:
                    issue_num = futures.pop(future)
                    try:
                        result = future.result()
                        results[issue_num] = result
                        if result.success:
                            logger.info("Issue #%s PR review completed", issue_num)
                        else:
                            logger.error("Issue #%s PR review failed: %s", issue_num, result.error)
                    except Exception as e:
                        logger.error("Issue #%s raised exception: %s", issue_num, e)
                        results[issue_num] = WorkerResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

        self._print_summary(results)
        return results

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print review summary.

        Args:
            results: Mapping of issue number to WorkerResult

        """
        total = len(results)
        successful = sum(1 for r in results.values() if r.success)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("PR Review Summary")
        logger.info("=" * 60)
        logger.info("Total PRs: %s", total)
        logger.info("Successful: %s", successful)
        logger.info("Failed: %s", failed)

        if failed > 0:
            logger.info("\nFailed issues:")
            for issue_num, result in results.items():
                if not result.success:
                    logger.info("  #%s: %s", issue_num, result.error)


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments for the reviewer CLI."""
    parser = build_review_parser(
        description=(
            "Analyze open PRs linked to GitHub issues using Claude Code or Codex "
            "and post inline review comments (read-only — does not fix code)"
        ),
        epilog="""
Examples:
  # Review PRs for specific issues
  %(prog)s --issues 595 596

  # Review with dry run
  %(prog)s --issues 595 --dry-run

  # Review with more workers
  %(prog)s --issues 595 596 --max-workers 5
        """,
        issues_help="Issue numbers whose linked PRs should be reviewed",
        dry_run_help="Show what would be done without actually posting any review comments",
    )
    add_json_arg(parser)
    return parser.parse_args()


def main() -> int:
    """Execute the PR review workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt

    """
    args = _parse_args()
    setup_review_logging(args.verbose)
    agent = resolve_agent(args.agent)

    log = logging.getLogger(__name__)
    log.info("Starting PR review for issues: %s", args.issues)

    from hephaestus.automation.models import ReviewerOptions
    from hephaestus.utils.terminal import terminal_guard

    options = ReviewerOptions(
        issues=args.issues,
        agent=agent,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        enable_ui=not args.no_ui and not args.json,
    )

    with terminal_guard():
        try:
            reviewer = PRReviewer(options)
            results = reviewer.run()

            failed = [num for num, result in results.items() if not result.success]
            if failed:
                log.error("Failed to review %s PR(s) for issue(s): %s", len(failed), failed)
                if args.json:
                    emit_json_status(1, issues=args.issues, failed=failed)
                return 1

            log.info("PR review complete")
            if args.json:
                emit_json_status(0, issues=args.issues, failed=[])
            return 0
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            if args.json:
                emit_json_status(130, message="interrupted")
            return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
