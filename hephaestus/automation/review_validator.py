"""Validate that prior PR review comments were actually addressed.

The in-loop review → address cycle (see
:meth:`hephaestus.automation.implementer_phase_runner.ImplementationPhaseRunner._run_impl_review_loop`)
used to resolve review threads on the implementer's *self-report* — the
implementer claimed it addressed a thread and the orchestrator resolved it,
even when no commit was produced (#1083). This module is now the single owner
of thread resolution, and it is evidence-based: a FRESH read-only sub-agent
compares each prior comment against the current diff and partitions them:

- **Addressed** — the diff genuinely resolves the comment. The validator
  resolves the thread in place (:func:`gh_pr_resolve_thread`).
- **Not addressed** — re-opened by posting a NEW inline review thread (GitHub
  has no "unresolve" mutation, and the unresolved-thread lister filters
  resolved threads out — so an already-resolved thread cannot be reopened in
  place). The new thread cites the original comment and explains what is still
  missing, then the loop treats the validation as NOGO so the address step runs
  again.

The implementer's address step no longer resolves anything; it only applies the
fix, commits, and pushes. A clean worktree (no real fix) therefore leaves the
diff unchanged, the validator judges the thread NOT addressed, and it stays
open — closing the "resolved without implementing" hole.

This respects the #375 own-threads-only guarantee (the validator posts and
resolves only its own / the bot's threads) and never mutates human threads.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import is_codex, run_codex_text

from ._review_utils import parse_json_block
from .claude_invoke import invoke_claude_with_session
from .claude_models import reviewer_model
from .claude_timeouts import pr_reviewer_claude_timeout
from .git_utils import get_repo_root, get_repo_slug, pr_ref
from .github_api import gh_pr_resolve_thread, gh_pr_review_post
from .prompts import get_review_validation_prompt
from .protocol import WONT_FIX_MARKER
from .session_naming import AGENT_PR_REVIEWER, reviewer_agent

logger = logging.getLogger(__name__)

# Number of source lines on EACH side of a comment's line to scan for a
# documenting comment/docstring when deciding whether a recurring re-open is
# an accepted by-design decision (#1329). Kept small and local so the heuristic
# only honours documentation that sits AT the cited code, not a stray comment
# elsewhere in the file.
_SOURCE_DOC_WINDOW = 6

# Phrases that, when present in a code comment/docstring near the cited line,
# signal the reviewer's suggestion was intentionally NOT taken (#1329). The
# match is case-insensitive and deliberately conservative — a recurring re-open
# is suppressed only when the SOURCE itself documents the design decision.
_BY_DESIGN_PHRASES = (
    "by design",
    "intentional",
    "intentionally",
    "on purpose",
    "deliberate",
    "deliberately",
    "wont-fix",
    "won't fix",
    "wont fix",
    "do not change",
    "do not remove",
    "keep this",
    "noqa",
    "nosec",
    "type: ignore",
)


def _thread_key(*, path: Any, line: Any, body: Any) -> str:
    """Build a stable cross-round identity for a prior review thread (#1329).

    Keying on ``path:line`` plus a normalized body lets the loop recognize when
    the SAME finding is being re-opened round after round (the GraphQL
    ``thread_id`` changes every time the validator posts a fresh re-open thread,
    so it cannot identify recurrence on its own). Whitespace is collapsed and
    the body lower-cased so cosmetic differences between a reviewer's original
    text and the validator's echo do not defeat the match.

    Args:
        path: File path the comment targets (may be empty for PR-level).
        line: Line number (int or None).
        body: The comment / original_body text.

    Returns:
        A normalized ``"path:line:body"`` key string.

    """
    norm_body = re.sub(r"\s+", " ", str(body or "")).strip().lower()
    line_part = "" if line is None else str(line)
    return f"{path or ''!s}:{line_part}:{norm_body}"


def _source_documents_decision(worktree_path: Path, path: str, line: Any) -> bool:
    """Return True when the source at *path*:*line* documents a by-design choice.

    The "documented in source" heuristic for #1329: when a recurring re-open
    keeps flagging the same spot, read the current source in the worktree around
    the cited line and look for a code comment / docstring that justifies why
    the reviewer's suggestion was intentionally not taken. If such a marker sits
    within :data:`_SOURCE_DOC_WINDOW` lines of the cited line, the decision is
    treated as accepted-by-design and the comment is NOT re-added.

    Deliberately simple and conservative: only an explicit by-design phrase
    (see :data:`_BY_DESIGN_PHRASES`) counts, and only when it sits next to the
    cited code — a generic comment elsewhere in the file does not qualify. Any
    read error (missing file, bad path, non-int line) returns False so a genuine
    unaddressed finding is never silently suppressed.

    Args:
        worktree_path: Worktree root the cited path is relative to.
        path: File path the comment targets.
        line: 1-based line number the comment pointed at.

    Returns:
        True when a by-design marker is found at/near the cited line.

    """
    if not path or not isinstance(line, int) or line < 1:
        return False
    try:
        # Guard against path traversal / absolute paths escaping the worktree.
        source = (worktree_path / path).resolve()
        if not source.is_relative_to(worktree_path.resolve()):
            return False
        text = source.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    lines = text.splitlines()
    if not lines:
        return False
    lo = max(0, line - 1 - _SOURCE_DOC_WINDOW)
    hi = min(len(lines), line + _SOURCE_DOC_WINDOW)
    window = "\n".join(lines[lo:hi]).lower()
    return any(phrase in window for phrase in _BY_DESIGN_PHRASES)


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
) -> tuple[list[str], bool, set[str]]:
    """Re-open prior review comments the current diff does not address.

    Runs a fresh read-only sub-agent that compares each ``prior_threads`` comment
    against ``diff_text``. For each comment judged NOT addressed, posts a NEW
    inline review thread on the same path/line citing the original comment, then
    reports the validation as not-clean so the caller drives another address
    iteration.

    Convergence (#1329): an unaddressed finding that was ALREADY re-opened in a
    prior round (same :func:`_thread_key`) is treated as RECURRING. A recurring
    finding is accepted-by-design and NOT re-added when either (a) the validator
    marked it won't-fix, or (b) the current source at its ``path``:``line``
    documents the design decision (:func:`_source_documents_decision`). Only a
    genuinely unaddressed AND undocumented finding still re-opens — converting
    the formerly unwinnable re-open loop into convergence so the review loop can
    reach GO.

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
        prior_reopened_keys: Stable keys (:func:`_thread_key`) of findings the
            validator re-opened in EARLIER rounds, threaded forward by the loop
            so recurrence can be detected. ``None`` on the first round.

    Returns:
        ``(reopened_thread_ids, is_clean, reopened_keys)``. ``is_clean`` is True
        when nothing was re-opened (every prior comment is addressed, dismissed
        as documented-by-design, or there was nothing to validate); False when
        at least one comment was re-opened. ``reopened_keys`` is the set of
        stable keys re-opened across this and all prior rounds — the caller
        passes it back in as ``prior_reopened_keys`` next round. As a side
        effect, every prior thread the validator confirms addressed is resolved
        in place (#1083).

    """
    seen_keys: set[str] = set(prior_reopened_keys or set())
    if not prior_threads:
        return [], True, seen_keys
    if dry_run:
        logger.info("[DRY RUN] Would validate prior comments on PR #%s", pr_number)
        return [], True, seen_keys

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
    )

    # Won't-fix dismissals (#1163): resolve each thread the agent judged
    # intentional-by-design with a durable WONT_FIX_MARKER reply. A marked thread
    # is skipped forever (see _filter_wont_fix_threads at the fetch boundary), so
    # an intentional-design finding cannot stack duplicate re-open threads.
    wont_fix_ids = {
        str(item.get("thread_id"))
        for item in wont_fix
        if isinstance(item, dict) and item.get("thread_id")
    }
    _dismiss_wont_fix_prior_threads(prior_threads, wont_fix, wont_fix_ids)

    # Resolve the threads the validator confirms addressed (#1083). A prior
    # thread is "addressed" when its thread_id is NOT among the unaddressed items
    # the sub-agent flagged AND not a won't-fix dismissal. Matching on the GraphQL
    # thread_id (not (path, line)) is required so two threads on the same line
    # resolve independently and a path-normalization mismatch can't silently
    # resolve an unaddressed thread (#1085). The sub-agent echoes back the
    # thread_id we provided.
    unaddressed_ids = {
        str(item.get("thread_id"))
        for item in unaddressed
        if isinstance(item, dict) and item.get("thread_id")
    }
    _resolve_addressed_prior_threads(prior_threads, unaddressed_ids | wont_fix_ids)

    if not unaddressed:
        return [], True, seen_keys

    comments: list[dict[str, Any]] = []
    pathless: list[dict[str, Any]] = []
    new_keys: set[str] = set()
    for item in unaddressed:
        path = item.get("path") or ""
        line = item.get("line")
        original = (item.get("original_body") or "").strip()
        # Stable cross-round identity (#1329): a finding re-opened last round
        # carries the SAME key even though its GraphQL thread_id was renewed.
        key = _thread_key(path=path, line=line, body=original or item.get("detail"))
        recurring = key in seen_keys

        if recurring and _source_documents_decision(worktree_path, path, line):
            # The same finding was re-opened before AND the current source at
            # that location documents why the reviewer's suggestion was
            # intentionally not taken — accept the design decision and stop
            # re-adding it, so a documented by-design choice no longer makes the
            # loop unwinnable (#1329). The key stays in ``seen_keys`` (carried
            # forward) so any later round keeps recognising it as
            # recurring-and-documented and keeps suppressing it.
            logger.info(
                "PR %s R%s: recurring finding at %s:%s is documented as by-design "
                "in source — not re-adding (accepted design decision)",
                pr_ref(pr_number),
                iteration,
                path or "(PR-level)",
                line if isinstance(line, int) else "?",
            )
            continue

        detail = (item.get("detail") or "").strip() or "prior review comment not addressed"
        prefix = (
            "Still unaddressed (recurring; not documented as by-design)"
            if recurring
            else "prior review comment not addressed"
        )
        body = f"Re-opening: {prefix} — {detail}"
        if original:
            quoted = "\n".join(f"> {ln}" for ln in original.splitlines())
            body = f"{body}\n\n{quoted}"

        new_keys.add(key)
        if not path:
            # PR-level (pathless) findings cannot be inline threads. Rather than
            # silently dropping them (#1329), surface them at PR level in the
            # review summary so the loop still treats the pass as not-clean and
            # the finding stays visible.
            pathless.append({"body": body})
            continue

        comment: dict[str, Any] = {"path": path, "body": body, "side": "RIGHT"}
        if isinstance(line, int):
            comment["line"] = line
        comments.append(comment)

    if not comments and not pathless:
        # Everything unaddressed was a documented-by-design recurrence → clean.
        return [], True, seen_keys

    summary_parts = []
    if comments:
        summary_parts.append(
            f"Re-opening {len(comments)} prior review comment(s) the current diff does not address."
        )
    if pathless:
        # Append the PR-level findings to the summary so they are not lost.
        bullets = "\n".join(f"- {p['body']}" for p in pathless)
        summary_parts.append(
            f"{len(pathless)} unaddressed PR-level review comment(s) remain:\n{bullets}"
        )

    thread_ids = gh_pr_review_post(
        pr_number=pr_number,
        comments=comments,
        summary="\n\n".join(summary_parts),
        dry_run=False,
        # #1083: if the line already carries a bot comment, edit it in place
        # rather than stacking a duplicate re-open thread.
        dedupe_existing=True,
    )
    logger.info(
        "PR %s R%s: re-opened %s inline + %s PR-level unaddressed review comment(s)",
        pr_ref(pr_number),
        iteration,
        len(thread_ids),
        len(pathless),
    )
    return thread_ids, False, seen_keys | new_keys


def _dismiss_wont_fix_prior_threads(
    prior_threads: list[dict[str, Any]],
    wont_fix: list[dict[str, Any]],
    wont_fix_ids: set[str],
) -> list[str]:
    """Resolve each won't-fix thread with a durable ``WONT_FIX_MARKER`` reply.

    The reply records WHY the finding is intentional-by-design so the dismissal
    is auditable in the PR UI and recognizable on later runs (the fetch boundary
    skips any thread whose comments carry the marker, so it is never
    re-validated or re-opened). Only threads the loop itself owns are touched
    (#375); a missing/blank reason still resolves with the bare marker.

    Returns the list of dismissed thread IDs (for logging/tests).
    """
    reason_by_id = {
        str(item.get("thread_id")): str(item.get("reason") or "").strip()
        for item in wont_fix
        if isinstance(item, dict) and item.get("thread_id")
    }
    dismissed: list[str] = []
    for thread in prior_threads:
        thread_id = thread.get("id")
        if not thread_id or str(thread_id) not in wont_fix_ids:
            continue
        reason = reason_by_id.get(str(thread_id), "")
        reply = WONT_FIX_MARKER if not reason else f"{WONT_FIX_MARKER} — {reason}"
        try:
            gh_pr_resolve_thread(thread_id, reply_body=reply, dry_run=False)
            dismissed.append(thread_id)
            logger.info(
                "Dismissed won't-fix review thread %s (intentional design): %s",
                thread_id,
                reason or "(no reason given)",
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning("Failed to dismiss won't-fix thread %s: %s", thread_id, exc)
    return dismissed


def _resolve_addressed_prior_threads(
    prior_threads: list[dict[str, Any]],
    unaddressed_ids: set[str],
) -> list[str]:
    """Resolve every prior thread the validator did not flag as unaddressed.

    A thread is considered addressed when its ``id`` is absent from
    *unaddressed_ids* (the set of thread_ids the validation sub-agent flagged).
    Matching on ``id`` rather than ``(path, line)`` means two threads on the same
    line resolve independently and a path mismatch cannot misfire (#1085). Only
    threads carrying a real ``id`` are resolved — the #375 own-threads guard,
    since ``prior_threads`` is always the set the loop itself posted/snapshotted.

    Returns the list of resolved thread IDs (useful for logging/tests).
    """
    resolved: list[str] = []
    for thread in prior_threads:
        thread_id = thread.get("id")
        if not thread_id:
            continue
        if str(thread_id) in unaddressed_ids:
            continue  # still open — re-opened above
        try:
            gh_pr_resolve_thread(thread_id, dry_run=False)
            resolved.append(thread_id)
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning("Failed to resolve addressed thread %s: %s", thread_id, exc)
    return resolved
