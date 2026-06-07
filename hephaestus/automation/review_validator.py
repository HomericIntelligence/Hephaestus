"""Validate that prior PR review comments were actually addressed.

The in-loop review → address cycle (see
:meth:`hephaestus.automation.implementer_phase_runner.ImplementationPhaseRunner._run_impl_review_loop`)
resolves review threads on the implementer's *self-report*. This module adds an
independent check: after the implementer claims it addressed the previous
iteration's comments, a FRESH read-only sub-agent compares each prior comment
against the current diff and re-opens any the diff does not actually resolve.

Re-opening is done by posting a NEW inline review thread (GitHub has no
"unresolve" mutation, and the unresolved-thread lister filters resolved threads
out — so an already-resolved thread cannot be reopened in place). The new thread
cites the original comment and explains what is still missing, then the loop
treats the validation as NOGO so the address step runs again.

This respects the #375 own-threads-only guarantee (the validator posts its own
threads via :func:`gh_pr_review_post`) and never mutates human threads.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import is_codex, run_codex_text

from ._review_utils import parse_json_block
from .claude_invoke import invoke_claude_with_session
from .claude_models import reviewer_model
from .claude_timeouts import pr_reviewer_claude_timeout
from .git_utils import get_repo_root, get_repo_slug, pr_ref
from .github_api import gh_pr_review_post
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
) -> list[dict[str, Any]]:
    """Run the read-only validation sub-agent and return the ``unaddressed`` list.

    Mirrors :func:`pr_reviewer.run_pr_review_analysis`'s invocation shape (a
    fresh read-only reviewer session, ``allowed_tools="Read,Glob,Grep"``). On any
    agent failure this returns an empty list — a failed validation must not block
    the loop or fabricate re-opens.
    """
    prompt = get_review_validation_prompt(
        pr_number=pr_number,
        issue_number=issue_number,
        prior_comments_json=prior_comments_json,
        diff_text=diff_text,
    )
    log_file = state_dir / f"review-validation-{issue_number}.log"
    try:
        if is_codex(agent):
            result = run_codex_text(
                prompt,
                cwd=worktree_path,
                timeout=pr_reviewer_claude_timeout(),
                sandbox="read-only",
            )
            log_file.write_text(result.stdout or "")
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
                timeout=pr_reviewer_claude_timeout(),
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Glob,Grep",
                input_via_stdin=True,
            )
            log_file.write_text(stdout or "")
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
        return []

    unaddressed = parsed.get("unaddressed", [])
    if not isinstance(unaddressed, list):
        return []
    # Keep only well-formed dict entries.
    return [u for u in unaddressed if isinstance(u, dict)]


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
) -> tuple[list[str], bool]:
    """Re-open prior review comments the current diff does not address.

    Runs a fresh read-only sub-agent that compares each ``prior_threads`` comment
    against ``diff_text``. For each comment judged NOT addressed, posts a NEW
    inline review thread on the same path/line citing the original comment, then
    reports the validation as not-clean so the caller drives another address
    iteration.

    Args:
        pr_number: GitHub PR number.
        issue_number: Linked GitHub issue number.
        worktree_path: Worktree CWD for the read-only sub-agent.
        prior_threads: The previous iteration's posted threads, each a dict with
            ``path`` / ``line`` / ``body``.
        diff_text: Current cumulative PR diff to validate against.
        agent: Selected implementation agent (``"claude"`` / ``"codex"``).
        iteration: Zero-based review-loop iteration (selects a fresh token).
        state_dir: Directory for the validation log file.
        dry_run: When True, skip the agent call and posting.

    Returns:
        ``(reopened_thread_ids, is_clean)``. ``is_clean`` is True when nothing
        was re-opened (every prior comment is addressed, or there was nothing to
        validate); False when at least one comment was re-opened.

    """
    if not prior_threads:
        return [], True
    if dry_run:
        logger.info("[DRY RUN] Would validate prior comments on PR #%s", pr_number)
        return [], True

    prior_comments_json = json.dumps(
        [
            {
                "path": t.get("path", ""),
                "line": t.get("line"),
                "body": t.get("body", ""),
            }
            for t in prior_threads
        ]
    )

    unaddressed = _run_validation_session(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        prior_comments_json=prior_comments_json,
        diff_text=diff_text,
        agent=agent,
        review_agent=reviewer_agent(AGENT_PR_REVIEWER, iteration),
        state_dir=state_dir,
    )
    if not unaddressed:
        return [], True

    comments: list[dict[str, Any]] = []
    for item in unaddressed:
        path = item.get("path") or ""
        if not path:
            # Inline threads require a path; skip PR-level re-opens (the reviewer
            # will resurface those on its next pass).
            continue
        original = (item.get("original_body") or "").strip()
        detail = (item.get("detail") or "").strip() or "prior review comment not addressed"
        body = f"Re-opening: prior review comment not addressed — {detail}"
        if original:
            quoted = "\n".join(f"> {ln}" for ln in original.splitlines())
            body = f"{body}\n\n{quoted}"
        comment: dict[str, Any] = {"path": path, "body": body, "side": "RIGHT"}
        line = item.get("line")
        if isinstance(line, int):
            comment["line"] = line
        comments.append(comment)

    if not comments:
        return [], True

    thread_ids = gh_pr_review_post(
        pr_number=pr_number,
        comments=comments,
        summary=(
            f"Re-opening {len(comments)} prior review comment(s) the current diff does not address."
        ),
        dry_run=False,
    )
    logger.info(
        "PR %s R%s: re-opened %s unaddressed review comment(s)",
        pr_ref(pr_number),
        iteration,
        len(thread_ids),
    )
    return thread_ids, False
