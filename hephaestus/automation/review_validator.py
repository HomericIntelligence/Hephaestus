"""Validate that prior PR review comments were actually addressed.

The in-loop review → address cycle (now owned by the pipeline PR-review stage,
``pipeline/stages/pr_review.py``)
used to resolve review threads on the implementer's *self-report* — the
implementer claimed it addressed a thread and the orchestrator resolved it,
even when no commit was produced (#1083). A fresh read-only sub-agent still
compares each prior comment against the current diff, but validation is now
report-only: GitHub offers no conditional thread-resolution mutation, so the
open thread remains a human merge gate.

- **Addressed** — the diff genuinely resolves the comment, but a human must
  verify and close the existing GitHub thread.
- **Not addressed** — reported to the caller so it can request another code
  change without mutating the thread.

The implementer's address step no longer resolves anything; it only applies the
fix, commits, and pushes. A clean worktree (no real fix) therefore leaves the
diff unchanged, the validator judges the thread NOT addressed, and it stays
open — closing the "resolved without implementing" hole.

This respects the #375 own-threads-only guarantee: validator output is never
used to mutate a review thread.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.io.utils import write_secure

from ._review_utils import log_file_path, parse_json_block
from .agent_config import DEFAULT_AGENT_TIMEOUT
from .claude_invoke import invoke_claude_with_session, raise_for_error_envelope
from .claude_models import reviewer_model
from .git_utils import get_repo_root, get_repo_slug
from .prompts import get_review_validation_prompt
from .session_naming import AGENT_PR_REVIEWER, reviewer_agent

logger = logging.getLogger(__name__)


def _run_validation_session(
    *,
    pr_number: int,
    issue_number: int,
    worktree_path: Path,
    prior_comments_json: str,
    diff_text: str,
    agent: str,
    review_agent: str,
    state_dir: Path,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the read-only validation sub-agent; return ``(unaddressed, wont_fix)``.

    Mirrors :func:`pr_reviewer.run_pr_review_analysis`'s invocation shape (a
    fresh read-only reviewer session, ``allowed_tools="Read,Glob,Grep"``). On any
    agent failure this returns ``([], [])`` — a failed validation must not block
    the loop, fabricate re-opens, or fabricate won't-fix dismissals.
    """
    prompt = get_review_validation_prompt(
        pr_number=pr_number,
        issue_number=issue_number,
        prior_comments_json=prior_comments_json,
        diff_text=diff_text,
    )
    log_file = log_file_path(state_dir, "review-validation", issue_number)
    try:
        if uses_direct_agent_runner(agent):
            result = run_agent_text(
                agent=agent,
                prompt=prompt,
                cwd=worktree_path,
                timeout=timeout,
                model=direct_agent_model(agent, "HEPH_REVIEWER_MODEL"),
                sandbox="read-only",
            )
            write_secure(log_file, result.stdout or "")
            parsed = parse_json_block(result.stdout or "")
        else:
            repo_slug = get_repo_slug(get_repo_root())
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=review_agent,
                prompt=prompt,
                model=reviewer_model(),
                cwd=worktree_path,
                timeout=timeout,
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Glob,Grep",
                input_via_stdin=True,
            )
            write_secure(log_file, stdout or "")
            # Fail loudly on an ``is_error: true`` envelope (e.g. a 429 cap)
            # instead of validating against the cap message as if it were a
            # real review (#1528 follow-up).
            raise_for_error_envelope(stdout or "")
            try:
                data = json.loads(stdout or "{}")
                response_text: str = data.get("result", stdout or "")
            except (json.JSONDecodeError, AttributeError):
                response_text = stdout or ""
            parsed = parse_json_block(response_text)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(
            "PR #%s: review-validation session failed (%s); skipping re-open pass",
            pr_number,
            exc,
        )
        return [], []

    unaddressed = parsed.get("unaddressed", [])
    if not isinstance(unaddressed, list):
        unaddressed = []
    wont_fix = parsed.get("wont_fix", [])
    if not isinstance(wont_fix, list):
        wont_fix = []
    # Keep only well-formed dict entries.
    return (
        [u for u in unaddressed if isinstance(u, dict)],
        [w for w in wont_fix if isinstance(w, dict)],
    )


def validate_prior_comments_addressed(
    *,
    pr_number: int,
    issue_number: int,
    worktree_path: Path,
    prior_threads: list[dict[str, Any]],
    diff_text: str,
    agent: str,
    iteration: int,
    state_dir: Path,
    dry_run: bool = False,
    prior_reopened_keys: set[str] | None = None,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> tuple[list[str], bool, set[str]]:
    """Validate prior comments without mutating their GitHub threads.

    The read-only agent compares each ``prior_threads`` comment against the
    current diff. Any open prior thread remains a human-resolution gate even
    when the agent reports that its finding is addressed; there is no
    conditional GitHub mutation that could safely close it.

    Args:
        pr_number / issue_number / worktree_path / prior_threads / diff_text /
        agent / iteration / state_dir / timeout: see the validation pipeline.
        dry_run: skip the agent call and posting.
        prior_reopened_keys: Legacy caller state retained without changing it.

    Returns:
        ``(thread_ids, is_clean, legacy_keys)``. ``is_clean`` is
        false whenever prior threads exist: their GitHub resolution remains an
        explicit human merge gate. ``reopened_keys`` is retained for the
        historical caller contract.

    """
    seen_keys: set[str] = set(prior_reopened_keys or set())
    if not prior_threads:
        return [], True, seen_keys
    if dry_run:
        logger.info("[DRY RUN] Would validate prior comments on PR #%s", pr_number)
        return [], True, seen_keys

    unaddressed = _run_validation_and_reconcile(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        prior_threads=prior_threads,
        diff_text=diff_text,
        agent=agent,
        iteration=iteration,
        state_dir=state_dir,
        timeout=timeout,
    )
    if unaddressed:
        logger.info(
            "PR %s R%s: validator reported %s unaddressed review thread(s); "
            "leaving all thread resolution to a human",
            pr_number,
            iteration,
            len(unaddressed),
        )
    return [], False, seen_keys


def _run_validation_and_reconcile(
    *,
    pr_number: int,
    issue_number: int,
    worktree_path: Path,
    prior_threads: list[dict[str, Any]],
    diff_text: str,
    agent: str,
    iteration: int,
    state_dir: Path,
    timeout: int,
) -> list[dict[str, Any]]:
    """Run the validation sub-agent and return its unaddressed report.

    Serializes ``prior_threads`` to JSON and runs the read-only validation
    session. The result is intentionally report-only: no validation outcome is
    allowed to close or alter a GitHub review thread.

    Returns:
        The list of unaddressed finding dicts the sub-agent flagged.

    """
    prior_comments_json = json.dumps(
        [
            {
                "thread_id": t.get("id", ""),
                "path": t.get("path", ""),
                "line": t.get("line"),
                "body": t.get("body", ""),
            }
            for t in prior_threads
        ]
    )

    unaddressed, wont_fix = _run_validation_session(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        prior_comments_json=prior_comments_json,
        diff_text=diff_text,
        agent=agent,
        review_agent=reviewer_agent(AGENT_PR_REVIEWER, iteration),
        state_dir=state_dir,
        timeout=timeout,
    )

    del wont_fix
    return unaddressed
