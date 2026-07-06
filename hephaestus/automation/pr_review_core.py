"""Pure/parse/context core of the PR-review flow (unit-covered, no live class).

Epic #1809's final omit-reduction wave (#1823) split the PR-review module into
two layers:

* This module holds the **shared review core** — the pure context assembler
  (:func:`gather_impl_review_context`), the agent-invoking analysis session
  (:func:`run_pr_review_analysis`), and the in-loop review+post orchestration
  (:func:`review_pr_inline`). These are consumed directly by the pipeline
  ``pr_review`` stage collaborators (``_review_phase`` / ``review_validator``)
  and by the standalone :class:`~hephaestus.automation.pr_reviewer.PRReviewer`.
  Every symbol here is reachable with mocked subprocess/agent seams, so the
  module carries direct unit coverage and is **not** on the
  ``[tool.coverage.run].omit`` allowlist.

* :mod:`hephaestus.automation.pr_reviewer` remains the console-script wrapper
  (``hephaestus-review-prs``) around the live worktree/agent orchestration. It
  re-exports the three cores below (``name as name``) so long-pinned patch
  sites — ``hephaestus.automation.pr_reviewer.run_pr_review_analysis`` etc. —
  keep resolving.

The cores are intentionally free of the ``PRReviewer``/``BaseReviewer``
scaffolding: they take everything they need as explicit keyword arguments, so
the in-loop implementer review step (Stage 2, #28) and the standalone reviewer
share exactly one invocation body (DRY).
"""

from __future__ import annotations

import contextlib
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

from . import _review_utils
from ._review_utils import log_file_path
from .agent_config import DEFAULT_AGENT_TIMEOUT
from .claude_invoke import invoke_claude_with_session, raise_for_error_envelope
from .claude_models import reviewer_model
from .git_utils import get_repo_root, get_repo_slug, pr_ref
from .github_api import gh_pr_review_post
from .prompts import get_pr_review_analysis_prompt
from .session_naming import AGENT_PR_REVIEWER, reviewer_agent

logger = logging.getLogger(__name__)


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
    timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> dict[str, Any]:
    """Run a read-only reviewer session and return its parsed analysis.

    Shared core of the standalone ``PRReviewer._run_analysis_session`` and the
    in-loop implementer review step (Stage 2, #28). Builds the PR-review
    analysis prompt, invokes the selected reviewer agent (Claude or Codex), and
    returns a dict with ``comments`` (inline findings), ``summary`` (the JSON
    summary, posted as the review body), and ``review_text`` (the full reviewer
    prose/stdout). The ``Verdict:`` line lives in the prose, not the summary, so
    callers derive the verdict from ``review_text`` via
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
        Parsed analysis dict with ``"comments"``, ``"summary"``, and
        ``"review_text"`` (verdict-bearing prose) keys.

    """
    if dry_run:
        logger.info("[DRY RUN] Would run analysis session for PR #%s", pr_number)
        review_text = "[DRY RUN] analysis skipped"
        return {"comments": [], "summary": review_text, "review_text": review_text}

    prompt = get_pr_review_analysis_prompt(
        pr_number=pr_number,
        issue_number=issue_number,
        pr_diff=context.get("pr_diff", ""),
        issue_body=context.get("issue_body", ""),
        ci_status=context.get("ci_status", ""),
        pr_description=context.get("pr_description", ""),
        advise_findings=context.get("advise_findings", ""),
        # #1083: nitpicks are suppressed unless --nitpick threaded the flag into
        # the review context.
        include_nitpicks=bool(context.get("include_nitpicks", False)),
    )

    prompt_file = worktree_path / f".claude-pr-review-{issue_number}.md"
    write_secure(prompt_file, prompt)

    log_file = log_file_path(state_dir, "pr-review-analysis", issue_number)

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
            review_text = result.stdout or ""
            parsed = _review_utils.parse_json_block(review_text)
            parsed["review_text"] = review_text
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
            timeout=timeout,
            output_format="json",
            permission_mode="dontAsk",
            allowed_tools="Read,Glob,Grep",
            # Pipe the prompt via stdin, not argv: the PR-review prompt embeds the
            # full diff and can be tens of KB, which overflows ARG_MAX and raises
            # `[Errno 7] Argument list too long` when passed as a positional arg.
            # Matches the plan reviewer / address-review / ci_driver invocations.
            input_via_stdin=True,
        )
        write_secure(log_file, stdout or "")

        # The CLI can exit 0 with an ``is_error: true`` envelope carrying a 429
        # quota cap; without this guard the cap message would be parsed as
        # review text and silently produce a bogus verdict (#1528 follow-up).
        # Raises ClaudeUsageCapError (a RuntimeError) so the review-phase handler
        # waits for reset before recording ERROR.
        raise_for_error_envelope(stdout or "")

        # Extract the response text from Claude's JSON wrapper
        try:
            data = json.loads(stdout or "{}")
            response_text: str = data.get("result", stdout or "")
        except (json.JSONDecodeError, AttributeError):
            response_text = stdout or ""

        parsed = _review_utils.parse_json_block(response_text)
        # The Verdict:/Grade: line lives in the reviewer prose, not the JSON
        # summary block. Surface it so callers parse the real verdict.
        parsed["review_text"] = response_text
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
        write_secure(log_file, error_output)
        raise RuntimeError(
            f"Analysis session failed for PR {pr_ref(pr_number)}: {e.stderr or e.stdout}"
        ) from e
    except subprocess.TimeoutExpired as e:
        write_secure(log_file, f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
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
    advise_findings: str = "",
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
        advise_findings: Prior ProjectMnemosyne findings from the advise step.
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
        "advise_findings": advise_findings,
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
    timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> tuple[str, list[str]]:
    """Review an impl PR in-loop: run analysis, post inline threads, return verdict.

    This is the in-loop equivalent of ``PRReviewer._review_pr`` used by the
    Stage 2 implementer session (#28). It runs a FRESH reviewer session per
    iteration (``reviewer_agent(AGENT_PR_REVIEWER, iteration)``) so the reviewer
    never inherits its own prior verdict, posts the analysis findings as inline
    PR review threads via :func:`gh_pr_review_post`, and returns the reviewer's
    VERDICT-BEARING PROSE (carrying the ``Verdict:`` line) plus the IDs of the
    threads it created.

    The verdict (``Verdict: GO|NOGO``) lives in the reviewer prose, NOT in the
    JSON ``summary`` field — so this returns ``review_text`` (the prose), which
    the caller feeds to :func:`parse_review_verdict`. The (verdict-free) JSON
    ``summary`` is still what gets POSTED to GitHub as the review body. Returning
    ``summary`` here instead would make every well-formed ``Verdict: NOGO`` parse
    as AMBIGUOUS.

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
        ``(review_text, posted_thread_ids)`` where ``review_text`` is the
        verdict-bearing reviewer prose. On dry-run, returns a verdict-bearing
        placeholder and an empty list.

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
        timeout=timeout,
    )
    comments: list[dict[str, Any]] = analysis.get("comments", [])
    summary: str = analysis.get("summary", "")
    # The verdict lives in the prose; fall back to summary only if review_text is
    # somehow absent (keeps the loop functioning rather than KeyError-ing).
    review_text: str = analysis.get("review_text") or summary

    if dry_run:
        logger.info(
            "[DRY RUN] Would post %s inline comment(s) on PR %s",
            len(comments),
            pr_ref(pr_number),
        )
        return review_text, []

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
    return review_text, thread_ids
