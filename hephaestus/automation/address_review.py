"""Address unresolved PR review threads using the selected coding agent.

Provides:
- Parallel processing of issues with unresolved review threads
- Session resume for the original implementer's agent session when supported
- Selective thread resolution based on the agent's reported fixes
- State persistence and UI monitoring

This module finds PRs with unresolved review threads, resumes the original
implementer's session when supported (or starts a fresh one), runs the selected
agent to fix the code, then resolves only the threads the agent explicitly
reports as addressed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    is_codex,
    resolve_agent,
    resume_codex_session,
    run_codex_session,
    session_agent_matches,
)
from hephaestus.cli.utils import add_json_arg, emit_json_status

from ._review_utils import (
    build_review_parser,
    find_pr_for_issue,
    instance_log,
    setup_review_logging,
)
from ._reviewer_base import BaseReviewer
from .claude_invoke import invoke_claude_with_session
from .claude_models import implementer_model
from .claude_timeouts import address_review_claude_timeout
from .comment_difficulty import classify_comments, format_todo_line

# Re-exports honor BaseReviewer's test-seam contract (#710); see
# BaseReviewer._PATCHABLE_DEPENDENCIES.  Tests patch these via
# ``patch("hephaestus.automation.address_review.<Name>")``;
# __all__ declares the intent so static-analysis tools do not flag them unused.
__all__ = ["StatusTracker", "ThreadLogManager", "WorktreeManager", "get_repo_root"]

from .curses_ui import CursesUI, ThreadLogManager
from .git_utils import (
    get_repo_root,
    get_repo_slug,
    issue_ref,
    pr_ref,
    run,
)
from .github_api import (
    gh_pr_list_unresolved_threads,
    gh_pr_resolve_thread,
)
from .models import AddressReviewOptions, ReviewPhase, ReviewState, WorkerResult
from .prompts import get_address_review_prompt
from .session_naming import AGENT_IMPLEMENTER
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


def _parse_addressed_block(text: str) -> dict[str, Any]:
    """Extract the last ```json``` block as an ``{"addressed", "replies"}`` dict.

    Trace-free parser shared by the in-loop address step (#28). The standalone
    :meth:`AddressReviewer._parse_json_block` wraps this with a diagnostic
    trace-file writer; callers that don't need the trace use this directly.

    Args:
        text: Claude's full response text.

    Returns:
        Parsed dict with ``"addressed"`` and ``"replies"`` keys, or defaults
        if no parseable ``json`` block is present.

    """
    matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not matches:
        return {"addressed": [], "replies": {}}
    try:
        return dict(json.loads(matches[-1]))
    except json.JSONDecodeError:
        return {"addressed": [], "replies": {}}


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
            The standalone path passes its trace-writing method; the in-loop
            path passes :func:`_parse_addressed_block`.
        log_file: Path to write the raw session log to.
        dry_run: When True, skip the agent call and return empty result.
        task_block: Optional task (issue) text for the prompt's context section.
            Supplied on the existing-PR review path so a fresh (non-resumed)
            session can read the task and continue the work.
        task_review_block: Optional plan-review verdict text for the context.
        diff_text: Optional current implementation diff for the context.

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
    )

    prompt_file = worktree_path / f".claude-address-review-{issue_number}.md"
    prompt_file.write_text(prompt)

    try:
        if is_codex(agent):
            codex_result = run_codex_session(
                prompt,
                cwd=worktree_path,
                timeout=address_review_claude_timeout(),
                sandbox="workspace-write",
            )
            log = codex_result.stdout
            if codex_result.session_id:
                log = f"SESSION_ID: {codex_result.session_id}\n\n{log}"
            log_file.write_text(log)
            parsed = parse_fn(codex_result.stdout)
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
            timeout=address_review_claude_timeout(),
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
        log_file.write_text(stdout or "")

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
        log_file.write_text(error_output)
        raise RuntimeError(
            f"Fix session failed for PR {pr_ref(pr_number)}: {e.stderr or e.stdout}"
        ) from e
    except subprocess.TimeoutExpired as e:
        log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
        raise RuntimeError(f"Fix session timed out for PR {pr_ref(pr_number)}") from e
    finally:
        # Narrow exception: a missing prompt file is benign cleanup,
        # but ENOSPC / permission errors are signal we want surfaced.
        try:
            prompt_file.unlink()
        except FileNotFoundError:
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


class AddressReviewer(BaseReviewer):
    """Addresses unresolved PR review threads using Claude Code or Codex.

    Features:
    - Parallel processing across multiple issues
    - Session resume from implementer's saved agent session when supported
    - Selective thread resolution (only resolves threads the agent explicitly fixed)
    - State persistence for observability
    - Real-time curses UI for status monitoring

    Inherits shared scaffolding (``__init__``, ``_log``, ``_fail``,
    ``_save_state``) from :class:`BaseReviewer`.
    """

    options: AddressReviewOptions

    def __init__(self, options: AddressReviewOptions) -> None:
        """Initialize address reviewer.

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
        """Run the address review workflow.

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        logger.info("Starting address review for issues: %s", self.options.issues)

        # Pre-discover PRs — only submit workers for issues that have an open PR.
        # This prevents Claude from being launched for issues with no PR at all.
        pr_map = self._discover_prs(self.options.issues)
        if not pr_map:
            logger.warning("No open PRs found for the specified issues — nothing to address")
            return {}

        logger.info("Found %s PR(s) to address: %s", len(pr_map), pr_map)

        # Start UI if enabled and not dry run
        if not self.options.dry_run and self.options.enable_ui:
            self.ui = CursesUI(self.status_tracker, self.log_manager)
            self.ui.start()

        try:
            results = self._address_all(pr_map)
            return results
        finally:
            if self.ui:
                self.ui.stop()
            if not self.options.dry_run:
                try:
                    self.worktree_manager.cleanup_all()
                except Exception:
                    logger.exception("Error during worktree cleanup in AddressReviewer.run()")

            # Report preserved worktrees (contain uncommitted changes after cleanup_all).
            # Mirror implementer.py:1263-1275 so operators know which worktrees survived.
            preserved = self.worktree_manager.preserved
            if preserved:
                logger.info("Preserved worktrees (contain uncommitted changes):")
                for issue_num, path in preserved:
                    logger.info("  #%d: %s", issue_num, path)
                logger.info("Inspect or discard them with: git worktree remove --force <path>")

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:
        """Pre-discover open PRs for all issues.

        Args:
            issue_numbers: Issue numbers to check

        Returns:
            Mapping of issue_number -> pr_number for issues that have an open PR

        """
        pr_map: dict[int, int] = {}
        for issue_num in issue_numbers:
            pr_number = self._find_pr_for_issue(issue_num)
            if pr_number is not None:
                pr_map[issue_num] = pr_number
            else:
                logger.info("Issue #%s: no open PR found, skipping", issue_num)
        return pr_map

    def _address_all(self, pr_map: dict[int, int]) -> dict[int, WorkerResult]:
        """Address all issues in parallel.

        Args:
            pr_map: Mapping of issue_number -> pr_number (pre-filtered to issues with PRs)

        Returns:
            Dictionary mapping issue number to WorkerResult

        """
        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}

            for issue_num, pr_num in pr_map.items():
                # Slot acquisition happens inside _address_issue via
                # self.status_tracker.acquire_slot() so workers that exceed
                # max_workers block rather than racing on a pre-assigned index
                # (#A3-005).
                future = executor.submit(self._address_issue, issue_num, pr_num)
                futures[future] = issue_num

            # Backoff on repeated wait() failures so a flapping condition
            # doesn't busy-loop silently. Resets to 0.1s on the first
            # successful wait().
            wait_backoff = 0.1
            while futures:
                try:
                    done, _pending = wait(futures.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
                    wait_backoff = 0.1
                except Exception as exc:
                    logger.warning(
                        "futures.wait() raised %s: %s — backing off %.1fs",
                        type(exc).__name__,
                        exc,
                        wait_backoff,
                    )
                    time.sleep(wait_backoff)
                    wait_backoff = min(wait_backoff * 2, 5.0)
                    continue

                for future in done:
                    issue_num = futures.pop(future)
                    try:
                        result = future.result()
                        results[issue_num] = result
                        if result.success:
                            logger.info("Issue #%s address review completed", issue_num)
                        else:
                            logger.error(
                                "Issue #%s address review failed: %s", issue_num, result.error
                            )
                    except Exception as e:
                        logger.error("Issue #%s raised exception: %s", issue_num, e)
                        results[issue_num] = WorkerResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

        self._print_summary(results)
        return results

    def _check_threads_for_address(
        self,
        issue_number: int,
        pr_number: int,
        thread_id: int,
    ) -> list[dict[str, Any]] | None:
        """List unresolved threads; return None if none or dry-run (caller returns success).

        Args:
            issue_number: GitHub issue number.
            pr_number: PR number to check.
            thread_id: Current thread id for logging.

        Returns:
            None if no threads or dry-run; list of unresolved thread dicts otherwise.

        """
        threads = gh_pr_list_unresolved_threads(pr_number, dry_run=self.options.dry_run)
        if not threads:
            self._log(
                "info",
                f"No unresolved threads on PR {pr_ref(pr_number)} for issue {issue_ref(issue_number)}",  # noqa: E501
                thread_id,
            )
            return None

        self._log(
            "info",
            f"Found {len(threads)} unresolved thread(s) on PR {pr_ref(pr_number)}",
            thread_id,
        )

        if self.options.dry_run:
            self._log(
                "info",
                f"[DRY RUN] Would address {len(threads)} unresolved thread(s) "
                f"and push for PR {pr_ref(pr_number)}",
                thread_id,
            )
            return None

        return threads

    def _setup_address_state(
        self,
        issue_number: int,
        pr_number: int,
        slot_id: int,
    ) -> tuple[str | None, ReviewState, str, Path]:
        """Load session_id, load/create ReviewState, resolve branch, create worktree.

        Args:
            issue_number: GitHub issue number.
            pr_number: PR number.
            slot_id: Worker slot for status tracking.

        Returns:
            Tuple of (session_id, review_state, branch_name, worktree_path).

        """
        session_id = self._load_impl_session_id(issue_number)
        review_state = self._load_review_state(issue_number)
        if review_state is None:
            branch_name = f"{issue_number}-auto-impl"
            review_state = ReviewState(
                issue_number=issue_number,
                pr_number=pr_number,
                branch_name=branch_name,
            )
        else:
            review_state.pr_number = pr_number
        branch_name = review_state.branch_name or f"{issue_number}-auto-impl"

        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Setting up worktree")
        worktree_path = self._get_or_create_worktree(issue_number, branch_name, review_state)

        with self.state_lock:
            review_state.worktree_path = str(worktree_path)
            review_state.branch_name = branch_name
            review_state.phase = ReviewPhase.FIXING
        self._save_review_state(review_state)
        return session_id, review_state, branch_name, worktree_path

    def _commit_push_and_resolve(
        self,
        *,
        issue_number: int,
        pr_number: int,
        branch_name: str,
        worktree_path: Path,
        addressed: list[str],
        replies: dict[str, str],
        threads: list[dict[str, Any]],
        review_state: ReviewState,
        slot_id: int,
        thread_id: int,
    ) -> None:
        """Commit fixes, push branch, resolve addressed threads, update review state.

        Args:
            issue_number: GitHub issue number.
            pr_number: PR number.
            branch_name: Git branch name.
            worktree_path: Path to worktree.
            addressed: Thread IDs the agent addressed.
            replies: Mapping of thread_id → reply text forwarded to resolve helper.
            threads: All threads presented to the fix session.
            review_state: Review state to update.
            slot_id: Worker slot for status tracking.
            thread_id: Current thread id for logging.

        """
        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Committing")
        self._commit_if_changes(issue_number, worktree_path)

        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Pushing")
        self._push_branch(branch_name, worktree_path)

        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Resolving threads")
        presented_thread_ids = {t["id"] for t in threads}
        # Pass the real replies dict — not {} — so _resolve_addressed_threads can post
        # reply comments on each resolved thread as required by the review protocol.
        self._resolve_addressed_threads(addressed, replies, presented_thread_ids)

        with self.state_lock:
            existing_ids = set(review_state.addressed_thread_ids)
            for tid in addressed:
                existing_ids.add(tid)
            review_state.addressed_thread_ids = list(existing_ids)
            review_state.phase = ReviewPhase.COMPLETED
            review_state.completed_at = datetime.now(timezone.utc)
        self._save_review_state(review_state)

        iref = issue_ref(issue_number)
        self.status_tracker.update_slot(slot_id, f"{iref}: Done")
        self._log(
            "info",
            f"Address review complete for issue {iref} (PR {pr_ref(pr_number)})",
            thread_id,
        )

    def _address_issue(self, issue_number: int, pr_number: int) -> WorkerResult:
        """Address unresolved review threads for a single issue."""
        slot_id = self.status_tracker.acquire_slot()
        if slot_id is None:
            return WorkerResult(
                issue_number=issue_number,
                success=False,
                error="Failed to acquire worker slot",
            )

        thread_id = threading.get_ident()
        self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Starting")
        self._log(
            "info",
            f"Addressing PR {pr_ref(pr_number)} for issue {issue_ref(issue_number)}",
            thread_id,
        )

        try:
            threads = self._check_threads_for_address(issue_number, pr_number, thread_id)
            if threads is None:
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            session_id, review_state, branch_name, worktree_path = self._setup_address_state(
                issue_number, pr_number, slot_id
            )

            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Running Claude fix"
            )
            fix_result = self._run_fix_session(
                issue_number=issue_number,
                pr_number=pr_number,
                worktree_path=worktree_path,
                threads=threads,
                session_id=session_id if self.options.resume_impl_session else None,
            )
            addressed: list[str] = fix_result.get("addressed", [])
            replies: dict[str, str] = fix_result.get("replies", {})
            self._log(
                "info",
                f"Claude addressed {len(addressed)} thread(s) on PR {pr_ref(pr_number)}",
                thread_id,
            )
            self._commit_push_and_resolve(
                issue_number=issue_number,
                pr_number=pr_number,
                branch_name=branch_name,
                worktree_path=worktree_path,
                addressed=addressed,
                replies=replies,
                threads=threads,
                review_state=review_state,
                slot_id=slot_id,
                thread_id=thread_id,
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

    def _find_pr_for_issue(self, issue_number: int) -> int | None:
        """Find the open PR for a single issue.

        Delegates to :func:`_review_utils.find_pr_for_issue` using the
        three-strategy variant (branch-name, on-disk review state, body
        search) so that the stored ``pr_number`` from a previous reviewer
        run is also consulted.

        Args:
            issue_number: GitHub issue number

        Returns:
            PR number if found, None otherwise

        """
        return find_pr_for_issue(
            issue_number,
            extra_strategies=True,
            _load_review_state_fn=lambda: self._load_review_state(issue_number),
        )

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Load the implementer's agent session ID from state file.

        Args:
            issue_number: GitHub issue number

        Returns:
            Session ID string if found, None otherwise

        """
        state_file = self.state_dir / f"issue-{issue_number}.json"
        if not state_file.exists():
            logger.warning(
                "No implementation state for issue #%s, will use fresh session", issue_number
            )
            return None
        try:
            data = json.loads(state_file.read_text())
            session_id: str | None = data.get("session_id")
            session_agent: str | None = data.get("session_agent")
            if session_id and not session_agent_matches(session_agent, self.options.agent):
                logger.info(
                    "Skipping impl session for issue #%s: session belongs to %s, "
                    "selected agent is %s",
                    issue_number,
                    session_agent or "claude",
                    self.options.agent,
                )
                return None
            return session_id
        except Exception as e:
            logger.warning("Could not load impl session for #%s: %s", issue_number, e)
            return None

    def _load_review_state(self, issue_number: int) -> ReviewState | None:
        """Load review state from disk.

        Thin wrapper around :meth:`BaseReviewer._load_review_state_from_disk`
        kept for backward compatibility with internal callers and tests.

        Args:
            issue_number: GitHub issue number

        Returns:
            ReviewState if state file exists and is valid, None otherwise

        """
        return self._load_review_state_from_disk(issue_number)

    def _save_review_state(self, state: ReviewState) -> None:
        """Save review state to disk.

        Thin wrapper around :meth:`BaseReviewer._save_state` kept for
        backward compatibility with internal callers.

        Args:
            state: ReviewState to persist

        """
        self._save_state(state)

    def _get_or_create_worktree(
        self,
        issue_number: int,
        branch_name: str,
        review_state: ReviewState,
    ) -> Path:
        """Get existing worktree or create a new one for the PR branch.

        Reuses the worktree path from review state if it still exists on disk.
        Otherwise creates a new worktree via WorktreeManager.

        Args:
            issue_number: GitHub issue number
            branch_name: PR branch name
            review_state: Current review state (may contain existing worktree path)

        Returns:
            Path to worktree directory

        """
        # Try to reuse existing worktree from review state
        if review_state.worktree_path:
            existing_path = Path(review_state.worktree_path)
            if existing_path.exists() and (existing_path / ".git").exists():
                logger.info(
                    "Reusing existing worktree at %s for issue #%s", existing_path, issue_number
                )
                # Register with worktree manager so cleanup works
                with self.worktree_manager.lock:
                    self.worktree_manager.worktrees[issue_number] = existing_path
                return existing_path

        # Create new worktree
        logger.info("Creating new worktree for issue #%s on branch %s", issue_number, branch_name)
        return self.worktree_manager.create_worktree(issue_number, branch_name)

    def _run_fix_session(
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        threads: list[dict[str, Any]],
        session_id: str | None,
    ) -> dict[str, Any]:
        """Run Claude fix session to address review threads.

        Delegates to the module-level :func:`run_address_fix_session` so the
        in-loop implementer step (#28) and this standalone phase share one
        invocation core (DRY). The Claude path resumes the implementer's
        deterministic session; ``session_id`` only feeds the Codex
        resume-then-fallback path below.

        Args:
            issue_number: GitHub issue number
            pr_number: GitHub PR number
            worktree_path: Path to git worktree containing PR branch
            threads: List of unresolved thread dicts (id, path, line, body)
            session_id: Previous Codex session ID to resume, or None for fresh session

        Returns:
            Parsed dict with "addressed" and "replies" keys

        """
        log_file = self.state_dir / f"address-review-{issue_number}.log"

        # Codex retains the resume-then-fallback behavior because it cannot
        # derive a deterministic session UUID; run the resume attempt here and
        # delegate the prompt build + parse to the shared core via a fresh run.
        if not self.options.dry_run and is_codex(self.options.agent) and session_id:
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
            prompt = get_address_review_prompt(
                pr_number=pr_number,
                issue_number=issue_number,
                worktree_path=str(worktree_path),
                threads_json=threads_json,
            )
            try:
                codex_result = resume_codex_session(
                    session_id,
                    prompt,
                    cwd=worktree_path,
                    timeout=address_review_claude_timeout(),
                )
            except subprocess.CalledProcessError as e:
                logger.warning(
                    "Issue #%s: Codex resume session %r failed for PR #%s; "
                    "falling back to fresh session: %s",
                    issue_number,
                    session_id,
                    pr_number,
                    (e.stderr or e.stdout or "")[:300],
                )
            else:
                log = codex_result.stdout
                if codex_result.session_id:
                    log = f"SESSION_ID: {codex_result.session_id}\n\n{log}"
                log_file.write_text(log)
                parsed = self._parse_json_block(codex_result.stdout, issue_number=issue_number)
                logger.info(
                    "Fix session complete for PR #%s; addressed %s thread(s)",
                    pr_number,
                    len(parsed.get("addressed", [])),
                )
                return parsed

        return run_address_fix_session(
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            threads=threads,
            agent=self.options.agent,
            repo_root=self.repo_root,
            parse_fn=lambda text: self._parse_json_block(text, issue_number=issue_number),
            log_file=log_file,
            dry_run=self.options.dry_run,
        )

    def _parse_json_block(self, text: str, issue_number: int | None = None) -> dict[str, Any]:
        """Extract the last ```json ... ``` block from an agent response.

        On parse failure or missing block, writes a trace file under the state
        dir so an empty ``addressed`` list is distinguishable from "the agent
        reviewed and decided no fixes were warranted". Without this the two
        cases looked identical in logs and it was hard to know whether to
        retry, escalate, or trust the result.

        Args:
            text: Claude's full response text
            issue_number: Issue number for diagnostic filenames; if None, no
                trace is written (used by tests).

        Returns:
            Parsed dict with "addressed" and "replies" keys, or defaults if not found

        """
        matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if not matches:
            self._write_parse_trace(
                issue_number,
                reason="no fenced ```json block found in response",
                text=text,
            )
            return {"addressed": [], "replies": {}}
        try:
            return dict(json.loads(matches[-1]))
        except json.JSONDecodeError as e:
            self._write_parse_trace(
                issue_number,
                reason=f"json.JSONDecodeError: {e}",
                text=text,
                last_block=matches[-1],
            )
            return {"addressed": [], "replies": {}}

    def _write_parse_trace(
        self,
        issue_number: int | None,
        *,
        reason: str,
        text: str,
        last_block: str | None = None,
    ) -> None:
        """Persist a diagnostic file describing why JSON parsing failed."""
        if issue_number is None:
            logger.warning("Parse trace skipped (no issue_number): %s", reason)
            return
        trace_path = self.state_dir / f"address-{issue_number}.parse-error.log"
        try:
            payload = [
                f"reason: {reason}",
                "",
                "=== last fenced block (if any) ===",
                last_block or "(none)",
                "",
                "=== full response ===",
                text,
            ]
            trace_path.write_text("\n".join(payload))
            logger.warning(
                "Issue #%d: address-review JSON parse failed (%s); trace at %s",
                issue_number,
                reason,
                trace_path,
            )
        except OSError as exc:
            logger.warning(
                "Issue #%d: address-review JSON parse failed and trace write also failed: %s",
                issue_number,
                exc,
            )

    def _resolve_addressed_threads(
        self,
        addressed: list[str],
        replies: dict[str, str],
        presented_thread_ids: set[str],
    ) -> None:
        """Resolve the review threads that the agent explicitly fixed.

        Only resolves threads listed in ``addressed`` AND present in
        ``presented_thread_ids``. Why: the agent response is untrusted input —
        a hallucinated or cross-PR thread ID would otherwise be passed straight
        to ``gh api graphql resolveReviewThread``. Membership against the set
        we actually presented to Claude is the trust boundary.

        Args:
            addressed: List of thread_id strings Claude reported as fixed
            replies: Mapping of thread_id to one-line reply describing the fix.
                Retained for the agent-output contract; resolution is quiet and
                does not post these replies to GitHub.
            presented_thread_ids: Set of thread IDs we presented to Claude
                (i.e. the unresolved set on this PR at fix time)

        """
        resolve_addressed_threads(
            addressed,
            replies,
            presented_thread_ids,
            dry_run=self.options.dry_run,
        )

    def _commit_if_changes(self, issue_number: int, worktree_path: Path) -> None:
        """Commit any pending changes in the worktree.

        Silently skips if there are no changes to commit.

        Args:
            issue_number: GitHub issue number (used in commit message)
            worktree_path: Path to git worktree

        """
        result = run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
        )
        if not result.stdout.strip():
            logger.info("No changes to commit for issue #%s", issue_number)
            return

        try:
            from .pr_manager import commit_changes

            commit_changes(issue_number, worktree_path, self.options.agent)
            logger.info("Committed fix changes for issue #%s", issue_number)
        except RuntimeError as e:
            # commit_changes raises RuntimeError if nothing to commit; already checked above
            logger.warning("Commit skipped for issue #%s: %s", issue_number, e)

    def _push_branch(self, branch_name: str, worktree_path: Path) -> None:
        """Push the branch to origin.

        Args:
            branch_name: Branch name to push
            worktree_path: Path to git worktree

        Raises:
            RuntimeError: If push fails

        """
        try:
            run(
                ["git", "push", "origin", branch_name],
                cwd=worktree_path,
            )
            logger.info("Pushed branch %s to origin", branch_name)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to push branch {branch_name}: {e}") from e

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print address review summary.

        Args:
            results: Mapping of issue number to WorkerResult

        """
        total = len(results)
        successful = sum(1 for r in results.values() if r.success)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("Address Review Summary")
        logger.info("=" * 60)
        logger.info("Total issues: %s", total)
        logger.info("Successful: %s", successful)
        logger.info("Failed: %s", failed)

        if failed > 0:
            logger.info("\nFailed issues:")
            for issue_num, result in results.items():
                if not result.success:
                    logger.info("  #%s: %s", issue_num, result.error)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for address review CLI."""
    parser = build_review_parser(
        description=(
            "Find PRs with unresolved review threads and use Claude Code or Codex to fix the "
            "code, then resolve only the threads the selected agent explicitly addresses."
        ),
        epilog="""
Examples:
  # Address review threads for specific issues
  %(prog)s --issues 595 596

  # Dry run — show what would be done without any GitHub writes or git pushes
  %(prog)s --issues 595 --dry-run

  # Use more parallel workers
  %(prog)s --issues 595 596 597 --max-workers 5
        """,
        issues_help="Issue numbers whose linked PRs should have review threads addressed",
        dry_run_help="Show what would be done without actually resolving threads or pushing code.",
    )
    add_json_arg(parser)
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the address review CLI."""
    return _build_parser().parse_args(argv)


def main() -> int:
    """Execute the address review workflow.

    Returns:
        Exit code: 0 on success, 1 if any issue failed, 130 on keyboard interrupt

    """
    args = _parse_args()
    setup_review_logging(args.verbose)
    agent = resolve_agent(args.agent)

    log = logging.getLogger(__name__)
    log.info("Starting address review for issues: %s", args.issues)

    from hephaestus.utils.terminal import terminal_guard

    options = AddressReviewOptions(
        issues=args.issues,
        agent=agent,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        enable_ui=not args.no_ui and not args.json,
        verbose=args.verbose,
    )

    with terminal_guard():
        try:
            reviewer = AddressReviewer(options)
            results = reviewer.run()

            failed = [num for num, result in results.items() if not result.success]
            if failed:
                log.error("Failed to address review for %s issue(s): %s", len(failed), failed)
                if args.json:
                    emit_json_status(1, issues=args.issues, failed=failed)
                return 1

            log.info("Address review complete")
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
