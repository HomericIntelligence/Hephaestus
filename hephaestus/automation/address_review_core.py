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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

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


@dataclass(frozen=True)
class _AddressFixSessionOutput:
    """Normalized response and persisted log text from an address-fix session."""

    response_text: str
    log_text: str


def _build_address_fix_prompt(
    *,
    issue_number: int,
    pr_number: int,
    worktree_path: Path,
    threads: list[dict[str, Any]],
    agent: str,
    repo_root: Path,
    state_dir: Path,
    advise_timeout: int,
    task_block: str,
    task_review_block: str,
    diff_text: str,
    unaddressed_findings: list[dict[str, Any]] | None,
) -> str:
    """Build the difficulty-annotated prompt for one address-fix session."""
    threads_json = json.dumps(
        [
            {
                "thread_id": thread["id"],
                "path": thread["path"],
                "line": thread.get("line"),
                "body": thread["body"],
            }
            for thread in threads
        ]
    )
    difficulties = classify_comments(
        threads=threads,
        agent=agent,
        issue_number=issue_number,
        worktree_path=worktree_path,
        repo_root=repo_root,
        state_dir=state_dir,
        advise_timeout=advise_timeout,
    )
    todo_block = "\n".join(
        format_todo_line(thread, difficulties.get(thread["id"], "medium")) for thread in threads
    )
    return get_address_review_prompt(
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


@contextmanager
def _address_fix_prompt_file(
    worktree_path: Path,
    issue_number: int,
    prompt: str,
) -> Iterator[Path]:
    """Create an address-fix prompt file and remove it after the session."""
    prompt_file = worktree_path / f".claude-address-review-{issue_number}.md"
    write_secure(prompt_file, prompt)
    try:
        yield prompt_file
    finally:
        try:
            prompt_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not unlink prompt file %s: %s", prompt_file, exc)


def _invoke_address_fix_session(
    *,
    issue_number: int,
    worktree_path: Path,
    agent: str,
    repo_root: Path,
    prompt: str,
    timeout: int,
) -> _AddressFixSessionOutput:
    """Run the selected provider and normalize its response and log output."""
    if uses_direct_agent_runner(agent):
        result = run_agent_session(
            agent=agent,
            prompt=prompt,
            cwd=worktree_path,
            timeout=timeout,
            model=direct_agent_model(agent, "HEPH_IMPLEMENTER_MODEL"),
            sandbox="workspace-write",
        )
        log_text = result.stdout
        if result.session_id:
            log_text = f"SESSION_ID: {result.session_id}\n\n{log_text}"
        return _AddressFixSessionOutput(
            response_text=result.stdout,
            log_text=log_text,
        )

    stdout, _ = invoke_claude_with_session(
        repo=get_repo_slug(repo_root),
        issue=issue_number,
        agent=AGENT_IMPLEMENTER,
        prompt=prompt,
        model=implementer_model(),
        cwd=worktree_path,
        timeout=timeout,
        output_format="json",
        permission_mode="dontAsk",
        # Task: the session dispatches one sub-agent per review comment at its
        # classified model tier. Skill: each sub-agent runs /hephaestus:advise.
        allowed_tools="Read,Write,Edit,Glob,Grep,Bash,Task,Skill",
        input_via_stdin=True,
    )
    raw_stdout = stdout or ""
    try:
        data = json.loads(raw_stdout or "{}")
        response_text: str = data.get("result", raw_stdout)
    except (json.JSONDecodeError, AttributeError):
        response_text = raw_stdout
    return _AddressFixSessionOutput(
        response_text=response_text,
        log_text=raw_stdout,
    )


def _persist_address_fix_log(log_file: Path, text: str) -> None:
    """Persist an address-fix session log with the project's secure writer."""
    write_secure(log_file, text)


def _parse_address_fix_session_output(
    output: _AddressFixSessionOutput,
    *,
    parse_fn: Callable[[str], dict[str, Any]],
    pr_number: int,
) -> dict[str, Any]:
    """Parse the provider response and record the completed thread count."""
    parsed = parse_fn(output.response_text)
    logger.info(
        "Fix session complete for PR #%s; addressed %s thread(s)",
        pr_number,
        len(parsed.get("addressed", [])),
    )
    return parsed


def _raise_address_fix_error(
    error: subprocess.CalledProcessError | subprocess.TimeoutExpired,
    *,
    log_file: Path,
    pr_number: int,
) -> NoReturn:
    """Persist a provider failure and translate it to the public error contract."""
    if isinstance(error, subprocess.CalledProcessError):
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        _persist_address_fix_log(
            log_file,
            f"EXIT CODE: {error.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}",
        )
        raise RuntimeError(
            f"Fix session failed for PR {pr_ref(pr_number)}: {error.stderr or error.stdout}"
        ) from error

    _persist_address_fix_log(
        log_file,
        f"TIMEOUT after {error.timeout}s\n\nOutput:\n{error.output or ''}",
    )
    raise RuntimeError(f"Fix session timed out for PR {pr_ref(pr_number)}") from error


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

    The public signature and observable provider, persistence, parsing, and
    cleanup behavior are retained while focused private helpers own each step.
    """
    if dry_run:
        logger.info("[DRY RUN] Would run fix session for PR #%s", pr_number)
        return {"addressed": [], "replies": {}}

    prompt = _build_address_fix_prompt(
        issue_number=issue_number,
        pr_number=pr_number,
        worktree_path=worktree_path,
        threads=threads,
        agent=agent,
        repo_root=repo_root,
        state_dir=log_file.parent,
        advise_timeout=advise_timeout,
        task_block=task_block,
        task_review_block=task_review_block,
        diff_text=diff_text,
        unaddressed_findings=unaddressed_findings,
    )

    with _address_fix_prompt_file(worktree_path, issue_number, prompt):
        try:
            output = _invoke_address_fix_session(
                issue_number=issue_number,
                worktree_path=worktree_path,
                agent=agent,
                repo_root=repo_root,
                prompt=prompt,
                timeout=timeout,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            _raise_address_fix_error(
                error,
                log_file=log_file,
                pr_number=pr_number,
            )

        _persist_address_fix_log(log_file, output.log_text)
        return _parse_address_fix_session_output(
            output,
            parse_fn=parse_fn,
            pr_number=pr_number,
        )


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
