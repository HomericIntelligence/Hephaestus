"""Pure/parse/session core of the address-review flow (unit-covered, no live class).

Epic #1809's final omit-reduction wave (#1823) split the address-review module
into two layers:

* This module holds the **shared address core** — the untraced parser
  (:func:`_parse_addressed_block`), its diagnostic helper
  (:func:`_log_address_parse_error`), the agent-invoking fix session
  (:func:`run_address_fix_session`), and the hallucination-guarded thread
  resolver (:func:`resolve_addressed_threads`). These are consumed directly by
  the pipeline ``pr_review`` stage collaborators (``_review_phase`` /
  ``review_thread_resolver``) and by the standalone
  :class:`~hephaestus.automation.address_review.AddressReviewer`. Every symbol
  here is reachable with mocked subprocess/agent seams, so the module carries
  direct unit coverage and is **not** on the ``[tool.coverage.run].omit``
  allowlist.

* :mod:`hephaestus.automation.address_review` remains the standalone reviewer
  around the live worktree/agent orchestration. It re-exports the cores below
  (``name as name``) so long-pinned patch sites keep resolving.

The cores are intentionally free of the ``AddressReviewer``/``BaseReviewer``
scaffolding: they take everything they need as explicit keyword arguments, so
the in-loop implementer address step (Stage 2, #28) and the standalone reviewer
share exactly one invocation body (DRY).
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_session,
    uses_direct_agent_runner,
)
from hephaestus.io.utils import write_secure

from . import _review_utils
from .agent_config import DEFAULT_AGENT_TIMEOUT
from .claude_invoke import invoke_claude_with_session
from .claude_models import implementer_model
from .comment_difficulty import classify_comments, format_todo_line
from .git_utils import get_repo_slug, pr_ref
from .github_api import gh_pr_resolve_thread
from .prompts import get_address_review_prompt
from .session_naming import AGENT_IMPLEMENTER

logger = logging.getLogger(__name__)

#: Default returned by :func:`_parse_addressed_block` when no parseable
#: ``json`` block is present in the agent output.
_ADDRESS_PARSE_DEFAULT: dict[str, Any] = {"addressed": [], "replies": {}}


def _parse_addressed_block(text: str) -> dict[str, Any]:
    """Extract the last ```json``` block as an ``{"addressed", "replies"}`` dict.

    Trace-free parser shared by the in-loop address step (#28). The standalone
    :class:`AddressReviewer` path wraps the same parser with a diagnostic
    trace-file writer; callers that don't need the trace use this directly.

    Args:
        text: Claude's full response text.

    Returns:
        Parsed dict with ``"addressed"`` and ``"replies"`` keys, or defaults
        if no parseable ``json`` block is present.

    """
    return _review_utils.parse_json_block(text, default=_ADDRESS_PARSE_DEFAULT)


def _log_address_parse_error(
    issue_number: int,
    reason: str,
    trace_path: Path | None,
    trace_error: OSError | None,
) -> None:
    if trace_error is not None:
        logger.warning(
            "Issue #%d: address-review JSON parse failed and trace write also failed: %s",
            issue_number,
            trace_error,
        )
    elif trace_path is not None:
        logger.warning(
            "Issue #%d: address-review JSON parse failed (%s); trace at %s",
            issue_number,
            reason,
            trace_path,
        )


def run_address_fix_session(
    *,
    issue_number: int,
    pr_number: int,
    worktree_path: Path,
    threads: list[dict[str, Any]],
    agent: str,
    repo_root: Path,
    parse_fn: Callable[[str], dict[str, Any]],
    log_file: Path,
    dry_run: bool = False,
    task_block: str = "",
    task_review_block: str = "",
    diff_text: str = "",
    unaddressed_findings: list[dict[str, Any]] | None = None,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
    advise_timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> dict[str, Any]:
    """Run the address-review fix session and return the agent's parsed result.

    Shared core of :meth:`AddressReviewer._run_fix_session` and the in-loop
    implementer address step (Stage 2, #28). Classifies each comment's fix
    difficulty (#1083), builds the address-review prompt (which fans out one
    sub-agent per COMMENT at the model tier matching its difficulty, with
    same-file comments serialized), runs the implementer agent, and returns the
    parsed ``{"addressed", "replies"}`` dict.

    The Claude path resumes the implementer's deterministic
    :data:`AGENT_IMPLEMENTER` session so fixes land in the same long-lived
    Session 2 transcript. Codex starts a fresh session (it has no
    deterministic-UUID resume).

    Args:
        issue_number: GitHub issue number.
        pr_number: GitHub PR number.
        worktree_path: Worktree containing the PR branch.
        threads: Unresolved thread dicts (``id``/``path``/``line``/``body``).
        agent: Selected implementation agent (``"claude"`` / ``"codex"``).
        repo_root: Repo root used for session-naming githash + slug.
        parse_fn: Callable ``(text) -> dict`` used to parse the agent's output.
            The standalone path passes its trace-writing closure; the in-loop
            path passes :func:`_parse_addressed_block`.
        log_file: Path to write the raw session log to.
        dry_run: When True, skip the agent call and return empty result.
        task_block: Optional task (issue) text for the prompt's context section.
            Supplied on the existing-PR review path so a fresh (non-resumed)
            session can read the task and continue the work.
        task_review_block: Optional plan-review verdict text for the context.
        diff_text: Optional current implementation diff for the context.
        unaddressed_findings: Optional still-unresolved threads from a prior
            no-commit turn (#1554); injected as a "Make sure to handle <finding>"
            directive to re-ground a resumed session on what it failed to fix.

    Returns:
        Parsed dict with ``"addressed"`` and ``"replies"`` keys.

    """
    if dry_run:
        logger.info("[DRY RUN] Would run fix session for PR #%s", pr_number)
        return {"addressed": [], "replies": {}}

    threads_json = json.dumps(
        [
            {
                "thread_id": t["id"],
                "path": t["path"],
                "line": t.get("line"),
                "body": t["body"],
            }
            for t in threads
        ]
    )

    # #1083: classify each comment's fix difficulty (separate cheap sub-agent),
    # then render the difficulty-annotated todo list that drives one-sub-agent-
    # per-comment dispatch at the matching model tier. Classification degrades to
    # "medium" on any failure, so this never blocks the fix session.
    difficulties = classify_comments(
        threads=threads,
        agent=agent,
        issue_number=issue_number,
        worktree_path=worktree_path,
        repo_root=repo_root,
        state_dir=log_file.parent,
        advise_timeout=advise_timeout,
    )
    todo_block = "\n".join(
        format_todo_line(t, difficulties.get(t["id"], "medium")) for t in threads
    )

    prompt = get_address_review_prompt(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=str(worktree_path),
        threads_json=threads_json,
        todo_block=todo_block,
        task_block=task_block,
        task_review_block=task_review_block,
        diff_text=diff_text,
        unaddressed_findings=unaddressed_findings,
    )

    prompt_file = worktree_path / f".claude-address-review-{issue_number}.md"
    write_secure(prompt_file, prompt)

    try:
        if uses_direct_agent_runner(agent):
            direct_result = run_agent_session(
                agent=agent,
                prompt=prompt,
                cwd=worktree_path,
                timeout=timeout,
                model=direct_agent_model(agent, "HEPH_IMPLEMENTER_MODEL"),
                sandbox="workspace-write",
            )
            log = direct_result.stdout
            if direct_result.session_id:
                log = f"SESSION_ID: {direct_result.session_id}\n\n{log}"
            write_secure(log_file, log)
            parsed = parse_fn(direct_result.stdout)
            logger.info(
                "Fix session complete for PR #%s; addressed %s thread(s)",
                pr_number,
                len(parsed.get("addressed", [])),
            )
            return parsed

        repo_slug = get_repo_slug(repo_root)
        stdout, _ = invoke_claude_with_session(
            repo=repo_slug,
            issue=issue_number,
            agent=AGENT_IMPLEMENTER,
            prompt=prompt,
            model=implementer_model(),
            cwd=worktree_path,
            timeout=timeout,
            output_format="json",
            permission_mode="dontAsk",
            # Task: the session acts as a coordinator that dispatches one
            # sub-agent per review COMMENT, at the model tier matching the
            # comment's classified difficulty (#1083), serializing same-file
            # comments. Skill: each sub-agent runs /hephaestus:advise before
            # fixing. See prompts/address_review.py.
            allowed_tools="Read,Write,Edit,Glob,Grep,Bash,Task,Skill",
            input_via_stdin=True,
        )
        write_secure(log_file, stdout or "")

        # Extract response text from Claude's JSON wrapper
        try:
            data = json.loads(stdout or "{}")
            response_text: str = data.get("result", stdout or "")
        except (json.JSONDecodeError, AttributeError):
            response_text = stdout or ""

        parsed = parse_fn(response_text)
        logger.info(
            "Fix session complete for PR #%s; addressed %s thread(s)",
            pr_number,
            len(parsed.get("addressed", [])),
        )
        return parsed

    except subprocess.CalledProcessError as e:
        stdout = e.stdout or ""
        stderr = e.stderr or ""
        error_output = f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
        write_secure(log_file, error_output)
        raise RuntimeError(
            f"Fix session failed for PR {pr_ref(pr_number)}: {e.stderr or e.stdout}"
        ) from e
    except subprocess.TimeoutExpired as e:
        write_secure(log_file, f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
        raise RuntimeError(f"Fix session timed out for PR {pr_ref(pr_number)}") from e
    finally:
        # Narrow exception: a missing prompt file is benign cleanup,
        # but ENOSPC / permission errors are signal we want surfaced.
        try:
            prompt_file.unlink()
        except FileNotFoundError:
            # Already gone — benign; nothing to clean up.
            pass
        except OSError as exc:
            logger.warning("Could not unlink prompt file %s: %s", prompt_file, exc)


def resolve_addressed_threads(
    addressed: list[str],
    replies: dict[str, str],
    presented_thread_ids: set[str],
    *,
    dry_run: bool = False,
) -> None:
    """Resolve the review threads the agent explicitly fixed (with hallucination guard).

    Shared core of :meth:`AddressReviewer._resolve_addressed_threads` and the
    in-loop address step (#28). Only resolves threads listed in ``addressed``
    AND present in ``presented_thread_ids`` — the agent response is untrusted
    input, so a hallucinated or cross-PR thread ID must never reach
    :func:`gh_pr_resolve_thread`. Membership against the set actually presented
    to the agent is the trust boundary (#661).

    Args:
        addressed: Thread-id strings Claude reported as fixed.
        replies: Mapping of thread-id to a one-line reply describing the fix. The
            mapping is retained for the agent-output contract but intentionally
            not posted; resolving quietly avoids adding duplicate review noise.
        presented_thread_ids: Thread IDs we presented to Claude (the unresolved
            set on this PR at fix time).
        dry_run: Forwarded to :func:`gh_pr_resolve_thread`.

    """
    for thread_id in addressed:
        if thread_id not in presented_thread_ids:
            logger.warning(
                "Skipping resolve of unknown thread_id %r — not in the "
                "unresolved-set presented to Claude (likely hallucinated)",
                thread_id,
            )
            continue
        try:
            gh_pr_resolve_thread(thread_id, dry_run=dry_run)
        except Exception as e:
            logger.warning("Could not resolve thread %s: %s", thread_id, e)
