"""Address unresolved PR review threads using Claude Code.

Provides:
- Parallel processing of issues with unresolved review threads
- Session resume for the original implementer's Claude session
- Selective thread resolution based on Claude's reported fixes
- State persistence and UI monitoring

This module finds PRs with unresolved review threads, resumes the original
implementer's Claude session (or starts a fresh one), runs Claude to fix the
code, then resolves only the threads Claude explicitly reports as addressed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    add_agent_argument,
    is_codex,
    resume_codex_session,
    run_codex_session,
    session_agent_matches,
)

from ._review_utils import find_pr_for_issue
from .claude_models import implementer_model
from .claude_timeouts import address_review_claude_timeout
from .curses_ui import CursesUI, ThreadLogManager
from .git_utils import get_repo_root, issue_ref, pr_ref, run
from .github_api import (
    gh_pr_list_unresolved_threads,
    gh_pr_resolve_thread,
    write_secure,
)
from .models import AddressReviewOptions, ReviewPhase, ReviewState, WorkerResult
from .prompts import get_address_review_prompt
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


class AddressReviewer:
    """Addresses unresolved PR review threads using Claude Code.

    Features:
    - Parallel processing across multiple issues
    - Session resume from implementer's saved Claude session
    - Selective thread resolution (only resolves threads Claude explicitly fixed)
    - State persistence for observability
    - Real-time curses UI for status monitoring
    """

    def __init__(self, options: AddressReviewOptions) -> None:
        """Initialize address reviewer.

        Args:
            options: Reviewer configuration options

        """
        self.options = options
        self.repo_root = get_repo_root()
        self.state_dir = self.repo_root / ".issue_implementer"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.worktree_manager = WorktreeManager()
        self.status_tracker = StatusTracker(options.max_workers)
        self.log_manager = ThreadLogManager()

        self.states: dict[int, ReviewState] = {}
        self.state_lock = threading.Lock()

        self.ui: CursesUI | None = None

    def _log(self, level: str, msg: str, thread_id: int | None = None) -> None:
        """Log to both standard logger and UI thread buffer.

        Args:
            level: Log level ("error", "warning", or "info")
            msg: Message to log
            thread_id: Thread ID (defaults to current thread)

        """
        getattr(logger, level)(msg)
        tid = thread_id or threading.get_ident()
        prefix = {"error": "ERROR", "warning": "WARN", "info": ""}.get(level, "")
        ui_msg = f"{prefix}: {msg}" if prefix else msg
        self.log_manager.log(tid, ui_msg)

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

    def _address_issue(self, issue_number: int, pr_number: int) -> WorkerResult:
        """Address unresolved review threads for a single issue.

        The pr_number is pre-discovered by run() — no Claude agent is ever launched
        for issues that have no open PR.

        Flow:
        1. Acquire worker slot via StatusTracker
        2. List unresolved threads
        3. DRY-RUN GUARD: return before any worktree/state mutations if dry_run
        4. Load impl session_id from state file
        5. Load/create review state
        6. Checkout worktree for the PR branch
        7. Run Claude fix session
        8. Commit changes if any
        9. Push branch
        10. Resolve addressed threads
        11. Update review state
        12. Return WorkerResult

        Args:
            issue_number: GitHub issue number
            pr_number: Pre-discovered open PR number for this issue

        Returns:
            WorkerResult

        """
        # Step 1: Acquire slot (mirrors pr_reviewer/_review_pr pattern, fixes A3-005)
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
            # Step 1: List unresolved threads
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Listing threads")
            threads = gh_pr_list_unresolved_threads(pr_number, dry_run=self.options.dry_run)
            if not threads:
                iref = issue_ref(issue_number)
                pref = pr_ref(pr_number)
                self._log(
                    "info",
                    f"No unresolved threads on PR {pref} for issue {iref}",
                    thread_id,
                )
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    pr_number=pr_number,
                )

            self._log(
                "info",
                f"Found {len(threads)} unresolved thread(s) on PR {pr_ref(pr_number)}",
                thread_id,
            )

            # Step 2: DRY-RUN GUARD — must come before any worktree creation or
            # state mutation so that dry-run leaves no side-effects on disk (#A3-004).
            if self.options.dry_run:
                self._log(
                    "info",
                    f"[DRY RUN] Would address {len(threads)} unresolved thread(s) "
                    f"and push for PR {pr_ref(pr_number)}",
                    thread_id,
                )
                return WorkerResult(
                    issue_number=issue_number,
                    success=True,
                    pr_number=pr_number,
                )

            # Step 3: Load impl session_id
            session_id = self._load_impl_session_id(issue_number)

            # Step 4: Load or create review state
            review_state = self._load_review_state(issue_number)
            if review_state is None:
                branch_name = f"{issue_number}-auto-impl"
                review_state = ReviewState(
                    issue_number=issue_number,
                    pr_number=pr_number,
                    branch_name=branch_name,
                )
            else:
                # Update PR number in case it changed
                review_state.pr_number = pr_number

            branch_name = review_state.branch_name or f"{issue_number}-auto-impl"

            # Step 5: Checkout worktree
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Setting up worktree"
            )
            worktree_path = self._get_or_create_worktree(issue_number, branch_name, review_state)

            with self.state_lock:
                review_state.worktree_path = str(worktree_path)
                review_state.branch_name = branch_name
                review_state.phase = ReviewPhase.FIXING
            self._save_review_state(review_state)

            # Step 6: Run Claude fix session
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

            # Step 7: Commit changes if any
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Committing")
            self._commit_if_changes(issue_number, worktree_path)

            # Step 8: Push branch
            self.status_tracker.update_slot(slot_id, f"{issue_ref(issue_number)}: Pushing")
            self._push_branch(branch_name, worktree_path)

            # Step 9: Resolve addressed threads
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: Resolving threads"
            )
            presented_thread_ids = {t["id"] for t in threads}
            self._resolve_addressed_threads(addressed, replies, presented_thread_ids)

            # Step 10: Update review state
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
        """Load the implementer's Claude session ID from state file.

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

        Args:
            issue_number: GitHub issue number

        Returns:
            ReviewState if state file exists and is valid, None otherwise

        """
        state_file = self.state_dir / f"review-{issue_number}.json"
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text())
            return ReviewState.model_validate(data)
        except Exception as e:
            logger.warning("Could not load review state for #%s: %s", issue_number, e)
            return None

    def _save_review_state(self, state: ReviewState) -> None:
        """Save review state to disk.

        Args:
            state: ReviewState to persist

        """
        state_file = self.state_dir / f"review-{state.issue_number}.json"
        write_secure(state_file, state.model_dump_json(indent=2))

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

    def _run_fix_session(  # noqa: C901  # session-resume + fallback + cleanup + parse error paths
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        threads: list[dict[str, Any]],
        session_id: str | None,
    ) -> dict[str, Any]:
        """Run Claude fix session to address review threads.

        Builds the address review prompt and runs Claude with --resume if a
        session_id is provided. Falls back to a fresh session if --resume fails.

        Args:
            issue_number: GitHub issue number
            pr_number: GitHub PR number
            worktree_path: Path to git worktree containing PR branch
            threads: List of unresolved thread dicts (id, path, line, body)
            session_id: Previous Claude session ID to resume, or None for fresh session

        Returns:
            Parsed dict with "addressed" and "replies" keys

        """
        if self.options.dry_run:
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

        prompt = get_address_review_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            worktree_path=str(worktree_path),
            threads_json=threads_json,
        )

        prompt_file = worktree_path / f".claude-address-review-{issue_number}.md"
        prompt_file.write_text(prompt)
        log_file = self.state_dir / f"address-review-{issue_number}.log"

        def _build_cmd(with_resume: bool, sid: str | None = None) -> list[str]:
            # When resuming, the session locks the original model — passing
            # --model would either be ignored or rejected. Only pin a model
            # for fresh-session invocations.
            base = ["claude", str(prompt_file), "--output-format", "json"]
            if with_resume and sid:
                base += ["--resume", sid]
            else:
                base[1:1] = ["--model", implementer_model()]
            base += [
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "Read,Write,Edit,Glob,Grep,Bash",
            ]
            return base

        try:
            if is_codex(self.options.agent):
                if session_id:
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
                        codex_result = run_codex_session(
                            prompt,
                            cwd=worktree_path,
                            timeout=address_review_claude_timeout(),
                            sandbox="workspace-write",
                        )
                else:
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
                parsed = self._parse_json_block(codex_result.stdout, issue_number=issue_number)
                logger.info(
                    "Fix session complete for PR #%s; addressed %s thread(s)",
                    pr_number,
                    len(parsed.get("addressed", [])),
                )
                return parsed

            # Attempt with session resume first if we have a session_id
            if session_id:
                claude_timeout = address_review_claude_timeout()
                try:
                    claude_result = run(
                        _build_cmd(with_resume=True, sid=session_id),
                        cwd=worktree_path,
                        timeout=claude_timeout,
                    )
                except subprocess.CalledProcessError as e:
                    stderr = (e.stderr or "").lower()
                    stdout = (e.stdout or "").lower()
                    combined = stderr + stdout
                    # Well-known "session gone" phrases (#A3-010 extended list)
                    session_error_phrases = (
                        "session not found",
                        "invalid session",
                        "session expired",
                        "no such session",
                        "session does not exist",
                        "cannot resume",
                        "resume failed",
                        "failed to resume",
                    )
                    # Fall back to fresh session on any --resume failure:
                    # either a known "session gone" phrase in stderr/stdout,
                    # OR any non-zero exit from a --resume invocation (the
                    # phrase list can never be exhaustive, so we default to
                    # fresh-session on unknown errors too — losing the resume
                    # context is preferable to a hard failure).
                    if any(phrase in combined for phrase in session_error_phrases) or True:
                        logger.warning(
                            "Session %r resume failed for issue #%d (exit=%d, reason=%r); "
                            "falling back to fresh session",
                            session_id,
                            issue_number,
                            e.returncode,
                            (e.stderr or "")[:120],
                        )
                        claude_result = run(
                            _build_cmd(with_resume=False),
                            cwd=worktree_path,
                            timeout=claude_timeout,
                        )
            else:
                claude_result = run(
                    _build_cmd(with_resume=False),
                    cwd=worktree_path,
                    timeout=address_review_claude_timeout(),
                )

            log_file.write_text(claude_result.stdout or "")

            # Extract response text from Claude's JSON wrapper
            try:
                data = json.loads(claude_result.stdout or "{}")
                response_text: str = data.get("result", claude_result.stdout or "")
            except (json.JSONDecodeError, AttributeError):
                response_text = claude_result.stdout or ""

            parsed = self._parse_json_block(response_text, issue_number=issue_number)
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

    def _parse_json_block(self, text: str, issue_number: int | None = None) -> dict[str, Any]:
        """Extract the last ```json ... ``` block from Claude's response.

        On parse failure or missing block, writes a trace file under the state
        dir so an empty ``addressed`` list is distinguishable from "Claude
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
        """Resolve the review threads that Claude explicitly fixed.

        Only resolves threads listed in ``addressed`` AND present in
        ``presented_thread_ids``. Why: Claude's response is untrusted input —
        a hallucinated or cross-PR thread ID would otherwise be passed straight
        to ``gh api graphql resolveReviewThread``. Membership against the set
        we actually presented to Claude is the trust boundary.

        Args:
            addressed: List of thread_id strings Claude reported as fixed
            replies: Mapping of thread_id to one-line reply describing the fix
            presented_thread_ids: Set of thread IDs we presented to Claude
                (i.e. the unresolved set on this PR at fix time)

        """
        for thread_id in addressed:
            if thread_id not in presented_thread_ids:
                logger.warning(
                    "Skipping resolve of unknown thread_id %r — not in the "
                    "unresolved-set presented to Claude (likely hallucinated)",
                    thread_id,
                )
                continue
            reply = replies.get(thread_id, "Addressed in code.")
            try:
                gh_pr_resolve_thread(thread_id, reply, dry_run=self.options.dry_run)
            except Exception as e:
                logger.warning("Could not resolve thread %s: %s", thread_id, e)

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

            commit_changes(issue_number, worktree_path)
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

    def _fail(
        self,
        issue_number: int,
        error_msg: str,
        slot_id: int,
    ) -> WorkerResult:
        """Record a failure, update state and tracker, and return a failed WorkerResult.

        Args:
            issue_number: GitHub issue number
            error_msg: Human-readable error description
            slot_id: Worker slot ID for status updates

        Returns:
            WorkerResult with success=False

        """
        self.status_tracker.update_slot(
            slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
        )
        err_state = self.states.get(issue_number)
        if err_state:
            with self.state_lock:
                err_state.phase = ReviewPhase.FAILED
                err_state.error = error_msg
            self._save_review_state(err_state)
        return WorkerResult(issue_number=issue_number, success=False, error=error_msg)

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


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging

    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments for the address review CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Find PRs with unresolved review threads and use Claude Code to fix the code, "
            "then resolve only the threads Claude explicitly addresses."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Address review threads for specific issues
  %(prog)s --issues 595 596

  # Dry run — show what would be done without any GitHub writes or git pushes
  %(prog)s --issues 595 --dry-run

  # Use more parallel workers
  %(prog)s --issues 595 596 597 --max-workers 5
        """,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        required=True,
        help="Issue numbers whose linked PRs should have review threads addressed",
    )
    add_agent_argument(parser)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=3,
        choices=range(1, 33),
        metavar="N",
        help="Maximum number of parallel workers, 1-32 (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually resolving threads or pushing code",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable curses UI (use plain logging instead)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    return parser.parse_args()


def main() -> int:
    """Execute the address review workflow.

    Returns:
        Exit code: 0 on success, 1 if any issue failed, 130 on keyboard interrupt

    """
    args = _parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)
    log.info("Starting address review for issues: %s", args.issues)

    from hephaestus.utils.terminal import terminal_guard

    options = AddressReviewOptions(
        issues=args.issues,
        agent=args.agent,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        enable_ui=not args.no_ui,
        verbose=args.verbose,
    )

    with terminal_guard():
        try:
            reviewer = AddressReviewer(options)
            results = reviewer.run()

            failed = [num for num, result in results.items() if not result.success]
            if failed:
                log.error("Failed to address review for %s issue(s): %s", len(failed), failed)
                return 1

            log.info("Address review complete")
            return 0
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
