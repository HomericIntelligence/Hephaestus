"""Pure/parse/context core of the PR-review flow (unit-covered, no live class).

Epic #1809's final omit-reduction wave (#1823) split the PR-review module into
two layers:

* This module holds the **shared review core** — the pure context assembler
  (:func:`gather_impl_review_context`) and agent-invoking analysis session
  (:func:`run_pr_review_analysis`) used by the pipeline. GitHub review-thread
  mutation belongs exclusively to the pipeline adapter.

* :mod:`hephaestus.automation.pr_reviewer` is the console-script wrapper
  (``hephaestus-review-prs``) around the pipeline.

The core is intentionally free of reviewer-class scaffolding: it takes
everything it needs as explicit keyword arguments, so
the in-loop implementer review step (Stage 2, #28) and the standalone reviewer
share exactly one invocation body (DRY).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.github.client import PromptTooLongError
from hephaestus.io.utils import write_secure

from ._review_utils import log_file_path
from .agent_config import DEFAULT_AGENT_TIMEOUT
from .claude_invoke import invoke_claude_with_session, raise_for_error_envelope
from .claude_models import reviewer_model
from .git_utils import get_repo_root, get_repo_slug, pr_ref
from .prompts import get_pr_review_analysis_prompt
from .review_audit import ReviewAudit, parse_review_audit
from .session_naming import AGENT_PR_REVIEWER

logger = logging.getLogger(__name__)

#: Fixed non-diff prompt overhead (rubric + terse-output directive + fence
#: markers), measured via get_pr_review_analysis_prompt(pr_diff="", ...) with
#: representative issue/advise text — empirically ~16,061 chars with 4KB of
#: issue+advise content, so ~12,000 chars is the rubric/directive/fencing
#: floor before any issue/PLAN/PLAN_REVIEW/advise text is added (#1847).
_PROMPT_FIXED_OVERHEAD_CHARS = 12_000

#: Default total diff budget (chars). Grounded against PR #1846, the issue's
#: motivating example: ~4,800 diff lines measured at this repo's actual diff
#: density (`git diff HEAD~15..HEAD` -> 29,431 lines / 1,263,927 chars ~= 43
#: chars/line) is ~=206,000 chars. 350,000 leaves >100K chars of headroom over
#: that case even after non-diff overhead is subtracted (see
#: budget_diff_for_prompt's composed_body_chars param), so the DEFAULT pass --
#: not only the aggressive retry -- covers the reported bug. At ~4 chars/token,
#: 350K chars ~= 87.5K tokens, well under a 200K-token context window shared
#: with the fixed overhead and generation budget.
DEFAULT_DIFF_BUDGET_CHARS = 350_000

#: Aggressive fallback budget used for the single retry after the CLI reports
#: "Prompt is too long" (#1847 suggested fix #2). ~60,000 chars ~= 15K tokens.
AGGRESSIVE_DIFF_BUDGET_CHARS = 60_000

_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git a/.* b/(.*)$", re.MULTILINE)


def budget_diff_for_prompt(diff_text: str, *, max_chars: int, composed_body_chars: int = 0) -> str:
    """Bound a unified diff to fit the prompt, trimming whole files, not lines.

    Splits ``diff_text`` on ``diff --git`` file boundaries and DROPS the
    largest files first (issue's "largest-file-first trimming"), keeping the
    smaller, more-reviewable diffs, until the remainder fits the *effective*
    budget: ``max_chars`` minus the fixed prompt overhead
    (:data:`_PROMPT_FIXED_OVERHEAD_CHARS`) minus ``composed_body_chars`` (the
    already-assembled issue/PLAN/PLAN_REVIEW text competing for the same
    prompt), so a large PLAN correctly shrinks the diff allowance instead of
    the floors stacking silently (#1847).

    Any text before the first ``diff --git`` header (e.g. a
    ``[... diff truncated ...]`` marker from an earlier flat-truncation pass)
    is preserved verbatim and prepended to the output, uncounted against the
    per-file budget -- it is typically empty or a short marker, not diff body.

    Args:
        diff_text: Raw unified diff (``git diff origin/main...HEAD`` output).
        max_chars: Nominal total diff budget before overhead is subtracted.
        composed_body_chars: Length of the non-diff prompt body (issue title +
            body + PLAN + PLAN_REVIEW) that will share the same prompt.

    Returns:
        The (possibly truncated) diff, with a trailing skipped-files index
        when any file was dropped. Returns ``diff_text`` unchanged when it
        already fits the effective budget.

    """
    effective_budget = max(0, max_chars - _PROMPT_FIXED_OVERHEAD_CHARS - composed_body_chars)
    if len(diff_text) <= effective_budget:
        return diff_text

    headers = list(_DIFF_FILE_HEADER_RE.finditer(diff_text))
    if not headers:
        return (
            diff_text[:effective_budget]
            + f"\n\n[... diff truncated at {effective_budget} chars ...]\n"
        )

    preamble = diff_text[: headers[0].start()]
    chunks: list[tuple[str, str]] = []
    for i, match in enumerate(headers):
        start = match.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(diff_text)
        path = match.group(1)  # the b/-side path only, not "a/x b/x"
        chunks.append((path, diff_text[start:end]))

    remaining = max(0, effective_budget - len(preamble))
    # Smallest-first greedy keep == largest-first trim: the same partition,
    # stated the way the issue phrases it ("trim the largest files first").
    order = sorted(range(len(chunks)), key=lambda i: len(chunks[i][1]))
    kept_idx: set[int] = set()
    for i in order:
        size = len(chunks[i][1])
        if size <= remaining:
            kept_idx.add(i)
            remaining -= size

    kept = [chunks[i][1] for i in range(len(chunks)) if i in kept_idx]
    skipped = [
        (chunks[i][0], chunks[i][1].count("\n")) for i in range(len(chunks)) if i not in kept_idx
    ]

    result = preamble + "".join(kept)
    if skipped:
        index_lines = "\n".join(
            f"- {path} ({lines} diff lines, omitted)" for path, lines in skipped
        )
        result += (
            f"\n\n[... {len(skipped)} largest file(s) omitted (largest-file-first "
            f"trimming) to fit the diff budget ...]\n{index_lines}\n"
        )
    return result


def _invoke_and_parse_review_session(
    *,
    active_prompt: str,
    agent: str,
    review_agent: str,
    pr_number: int,
    issue_number: int,
    worktree_path: Path,
    prompt_file: Path,
    log_file: Path,
    timeout: int,
) -> dict[str, Any]:
    """Invoke one reviewer session and parse its response.

    Split out of :func:`run_pr_review_analysis` so the retry-on-too-long path
    can call it twice without duplicating the invoke/parse body (#1847). Kept
    at module scope (not nested) so the ``invoke_claude_with_session`` call
    site's AST-visible enclosing-function name stays ``run_pr_review_analysis``
    for the ``#1482`` dontAsk-policy-documentation test/AGENTS.md row — a
    nested closure would rename that scope and desync the doc.
    """
    write_secure(prompt_file, active_prompt)

    if uses_direct_agent_runner(agent):
        result = run_agent_text(
            agent=agent,
            prompt=active_prompt,
            cwd=worktree_path,
            timeout=timeout,
            execution_request=ExecutionRequest(
                AgentRole.PR_REVIEWER, AgentOperation.PR_REVIEW, SessionLifecycle.ONE_SHOT
            ),
            model=direct_agent_model(agent, model_value=reviewer_model()),
            sandbox="read-only",
        )
        write_secure(log_file, result.stdout or "")
        review_text = result.stdout or ""
        audit = parse_review_audit(review_text)
        return {
            "audit": audit,
            "comments": [dict(comment) for comment in audit.findings],
            "summary": audit.summary,
            "review_text": audit.raw_feedback,
        }

    repo_root = get_repo_root()
    repo_slug = get_repo_slug(repo_root)
    stdout, _ = invoke_claude_with_session(
        repo=repo_slug,
        issue=issue_number,
        agent=review_agent,
        prompt=active_prompt,
        model=reviewer_model(),
        cwd=worktree_path,
        timeout=timeout,
        output_format="json",
        permission_mode="dontAsk",
        # The normal $athena:pr-review skill is read-only, but its declared
        # workflow uses local Bash helpers and review subagents.
        allowed_tools="Read,Glob,Grep,Bash,Skill,Agent,WebFetch",
        # Pipe the prompt via stdin, not argv: the PR-review prompt embeds the
        # full diff and can be tens of KB, which overflows ARG_MAX and raises
        # `[Errno 7] Argument list too long` when passed as a positional arg.
        # Matches the plan reviewer / address-review / ci_driver invocations.
        input_via_stdin=True,
    )
    write_secure(log_file, stdout or "")

    # The CLI can exit 0 with an ``is_error: true`` envelope carrying a 429
    # quota cap or an oversized prompt; without this guard the error message
    # would be parsed as review text and silently produce a bogus verdict
    # (#1528 follow-up). Raises PromptTooLongError / ClaudeUsageCapError
    # (both RuntimeError subclasses) so callers can react distinctly.
    raise_for_error_envelope(stdout or "")

    # Extract the response text from Claude's JSON wrapper
    try:
        data = json.loads(stdout or "{}")
        response_text: str = data.get("result", stdout or "")
    except (json.JSONDecodeError, AttributeError):
        response_text = stdout or ""

    audit = parse_review_audit(response_text)
    return {
        "audit": audit,
        "comments": [dict(comment) for comment in audit.findings],
        "summary": audit.summary,
        "review_text": audit.raw_feedback,
    }


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

    Shared core of the pipeline's read-only review analysis and the in-loop
    implementer review step (Stage 2, #28). Builds the PR-review analysis
    prompt, invokes the selected reviewer agent (Claude or Codex), and returns
    a dict with a structural ``audit`` value, normalized inline findings, an
    informational summary, and bounded supplemental feedback. Review prose
    never carries authorization.

    Args:
        pr_number: GitHub PR number being reviewed.
        issue_number: Linked GitHub issue number.
        worktree_path: Worktree CWD for the reviewer session (read-only usage).
        context: Pipeline-supplied PR review context.
        agent: Selected implementation agent (``"claude"`` or ``"codex"``);
            determines the runtime used to invoke the reviewer.
        review_agent: Session-naming agent token for the Claude path. Defaults
            to :data:`AGENT_PR_REVIEWER`; the in-loop caller passes a fresh
            per-iteration token (``reviewer_agent(AGENT_PR_REVIEWER, i)``).
        state_dir: Directory for the reviewer log file.
        dry_run: When True, skip the agent call and return a placeholder dict.

    Returns:
        Parsed analysis dict with ``"audit"``, ``"comments"``, ``"summary"``,
        and ``"review_text"`` (bounded supplemental feedback) keys.

    """
    if dry_run:
        logger.info("[DRY RUN] Would run analysis session for PR #%s", pr_number)
        audit = ReviewAudit(
            grade=None,
            summary="[DRY RUN] analysis skipped",
            findings=(),
            raw_feedback="[DRY RUN] analysis skipped",
            valid=False,
        )
        return {
            "audit": audit,
            "comments": [],
            "summary": audit.summary,
            "review_text": audit.raw_feedback,
        }

    prompt_file = worktree_path / f".claude-pr-review-{issue_number}.md"
    log_file = log_file_path(state_dir, "pr-review-analysis", issue_number)

    def _build_prompt(diff_override: str | None = None) -> str:
        diff_text = diff_override if diff_override is not None else context.get("pr_diff", "")
        return get_pr_review_analysis_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            pr_diff=diff_text,
            issue_body=context.get("issue_body", ""),
            pr_description=context.get("pr_description", ""),
            advise_findings=context.get("advise_findings", ""),
            # #1083: nitpicks are suppressed unless --nitpick threaded the flag
            # into the review context.
            include_nitpicks=bool(context.get("include_nitpicks", False)),
        )

    def _invoke_and_parse(active_prompt: str) -> dict[str, Any]:
        return _invoke_and_parse_review_session(
            active_prompt=active_prompt,
            agent=agent,
            review_agent=review_agent,
            pr_number=pr_number,
            issue_number=issue_number,
            worktree_path=worktree_path,
            prompt_file=prompt_file,
            log_file=log_file,
            timeout=timeout,
        )

    prompt = _build_prompt()

    try:
        try:
            parsed = _invoke_and_parse(prompt)
        except PromptTooLongError:
            aggressive_diff = budget_diff_for_prompt(
                context.get("pr_diff", ""),
                max_chars=AGGRESSIVE_DIFF_BUDGET_CHARS,
                composed_body_chars=len(context.get("issue_body", ""))
                + len(context.get("advise_findings", "")),
            )
            retry_prompt = _build_prompt(aggressive_diff)
            logger.warning(
                "PR #%s: prompt too long at default budget (%s chars); retrying once "
                "with aggressive diff budget (%s chars -> %s chars, reason=prompt_too_long)",
                pr_number,
                len(prompt),
                AGGRESSIVE_DIFF_BUDGET_CHARS,
                len(retry_prompt),
            )
            parsed = _invoke_and_parse(retry_prompt)

        logger.info(
            "Analysis complete for PR #%s; found %s inline comment(s)",
            pr_number,
            len(parsed.get("comments", [])),
        )
        return parsed

    except PromptTooLongError as e:
        raise RuntimeError(
            f"reason=prompt_too_long: PR review prompt exceeds model context even at "
            f"aggressive budget for {pr_ref(pr_number)}"
        ) from e
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
    max_diff_chars: int = DEFAULT_DIFF_BUDGET_CHARS,
) -> dict[str, Any]:
    """Assemble the PR-review context for an in-loop implementer review.

    Folds the implementer-loop inputs (TASK = issue title+body, PLAN,
    PLAN_REVIEW, and the impl diff) into the dict shape
    :func:`run_pr_review_analysis` expects. The PLAN and PLAN_REVIEW comment
    bodies are surfaced inside the ``issue_body`` field so the reviewer sees
    the full design context the implementer worked from (Stage 2, #28).

    The diff is bounded to ``max_diff_chars`` via :func:`budget_diff_for_prompt`
    so large PRs don't blow the model's context window (#1847); the composed
    TASK/PLAN/PLAN_REVIEW body and advise findings count against the same
    budget, so a large PLAN correctly shrinks the diff allowance.

    Args:
        pr_number: GitHub PR number under review.
        issue_number: Linked GitHub issue number.
        issue_title: Issue title (the TASK summary).
        issue_body: Full issue body (the TASK detail).
        plan_text: The implementation PLAN comment body (or "" if absent).
        plan_review_text: The PLAN_REVIEW comment body (or "" if absent).
        diff_text: ``gh pr diff`` / cumulative branch diff for the impl.
        advise_findings: Prior Mnemosyne findings from the advise step.
        include_nitpicks: Forwarded into the context so the reviewer prompt
            emits nitpick-severity comments only when ``--nitpick`` is set
            (#1083).
        max_diff_chars: Total diff budget in chars before the diff is
            truncated file-by-file (#1847). Defaults to
            :data:`DEFAULT_DIFF_BUDGET_CHARS`.

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
        "pr_diff": budget_diff_for_prompt(
            diff_text or "",
            max_chars=max_diff_chars,
            composed_body_chars=len(composed_body) + len(advise_findings),
        ),
        "issue_body": composed_body,
        "review_comments": "",
        "pr_description": "",
        "advise_findings": advise_findings,
        "include_nitpicks": include_nitpicks,
    }
