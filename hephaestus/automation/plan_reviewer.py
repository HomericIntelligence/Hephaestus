"""Plan review automation: reads issue plans and posts review comments.

Provides:
- Parallel plan review across multiple issues
- Duplicate review detection (skips already-reviewed issues)
- Plan detection using the same canonical marker as the planner
- Dry-run support with early return before any GitHub writes
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    resolve_agent,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.automation._review_utils import (
    build_automation_parser,
    drain_completed_futures,
    print_worker_summary,
    work_report_context,
)
from hephaestus.cli.utils import add_agent_timeout_arg, configure_cli_logging, emit_json_status
from hephaestus.github.rate_limit import wait_until
from hephaestus.utils.terminal import terminal_guard

from .agent_config import DEFAULT_AGENT_TIMEOUT
from .claude_invoke import invoke_claude_with_session, scan_quota_reset
from .claude_models import reviewer_model
from .git_utils import get_repo_root, get_repo_slug, issue_ref
from .github_api import (
    fetch_issue_comments_metadata,
    gh_current_login,
    gh_issue_edit_labels,
    gh_issue_json,
    gh_issue_upsert_owned_comment,
)
from .models import PlanReviewerOptions, WorkerResult
from .prompts import get_plan_review_prompt
from .protocol import PLAN_REVIEW_CANONICAL_MARKER
from .review_journal import (
    IssueComment,
    blocked_audit_recovery_body,
    comment_revision,
    is_plan_comment,
    parse_plan_review_state,
    render_current_review,
)
from .review_state import (
    PLAN_REVIEW_PREFIX as _REVIEW_PREFIX_SHARED,
)
from .session_naming import AGENT_PLAN_REVIEWER
from .state_labels import (
    ALL_STATE_LABELS,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    apply_plan_state,
    is_exclusive_plan_state,
)
from .status_tracker import StatusTracker

logger = logging.getLogger(__name__)

# Prefix used by this reviewer when posting review comments.
#
# IMPORTANT: byte-exact case-sensitive match assumption. Idempotency rests on
# ``body.startswith(_REVIEW_PREFIX)``. Both sides come from the same writer
# (this module) and GitHub stores comment bodies verbatim, so today this is
# safe. If a future GitHub or tooling change ever normalizes the U+1F50D
# magnifying-glass emoji to a different Unicode form (e.g. NFD vs NFC), or
# alters spacing/case, ``startswith`` will silently miss and the reviewer
# will post a duplicate review every loop. If that becomes a concern,
# NFC-normalize both sides via ``unicodedata.normalize("NFC", ...)`` before
# comparison. See issue #565.
# Aliased from review_state so both reviewer and implementer share one
# source of truth for the plan-review gate
# (see :mod:`hephaestus.automation.review_state` and issue #551).
_REVIEW_PREFIX = _REVIEW_PREFIX_SHARED

# Plan reviews terminate with exactly one supported ``state:plan-*`` token.
# The corresponding GitHub label is the only durable workflow authority;
# comment text remains an explanatory audit record and is never a gate.


class PlanReviewer:
    """Reviews implementation plans posted to GitHub issues by the planner.

    Features:
    - Parallel review across multiple issues
    - Skips issues that already have a plan review comment
    - Skips issues that have no plan comment yet
    - Dry-run mode exits before any GitHub write operation
    """

    def __init__(self, options: PlanReviewerOptions) -> None:
        """Initialize the plan reviewer.

        Args:
            options: Plan reviewer configuration options.

        """
        self.options = options
        self.status_tracker = StatusTracker(options.max_workers)
        self.lock = threading.Lock()
        # Per-instance cache for ``_fetch_issue_comments`` (#A3-009, #560).
        # Initialised here rather than lazily so mypy sees the type and the
        # ThreadPoolExecutor invariant is unambiguous.
        self._comments_cache: dict[int, list[dict[str, Any]]] = {}

    def run(self) -> dict[int, WorkerResult]:
        """Run the plan reviewer on all issues.

        Returns:
            Dictionary mapping issue number to WorkerResult.

        """
        logger.info(
            "Reviewing plans for %s issue(s) with %s parallel workers",
            len(self.options.issues),
            self.options.max_workers,
        )

        if not self.options.issues:
            logger.warning("No issues to review")
            return {}

        results: dict[int, WorkerResult] = {}

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
            futures: dict[Future[Any], int] = {}

            for idx, issue_num in enumerate(self.options.issues):
                future = executor.submit(self._review_issue, issue_num, idx)
                futures[future] = issue_num

            for future in drain_completed_futures(futures):
                issue_num = futures.pop(future)
                try:
                    result = future.result()
                    with self.lock:
                        results[issue_num] = result
                    if result.success:
                        logger.info("Issue %s: plan review completed", issue_ref(issue_num))
                    else:
                        logger.error(
                            "Issue %s: plan review failed: %s",
                            issue_ref(issue_num),
                            result.error,
                        )
                except Exception as e:
                    logger.error("Issue %s raised exception: %s", issue_ref(issue_num), e)
                    with self.lock:
                        results[issue_num] = WorkerResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

        self._print_summary(results)
        return results

    def _review_issue(self, issue_number: int, slot_id: int) -> WorkerResult:
        """Review the plan for a single issue.

        Args:
            issue_number: GitHub issue number to review.
            slot_id: Worker slot ID for status tracking.

        Returns:
            WorkerResult indicating success or failure.

        """
        with self.status_tracker.slot() as acquired_slot:
            if acquired_slot is None:
                return WorkerResult(
                    issue_number=issue_number,
                    success=False,
                    error="Failed to acquire worker slot",
                )

            try:
                self.status_tracker.update_slot(
                    acquired_slot, f"{issue_ref(issue_number)}: checking"
                )

                # --- Read-only checks (safe in dry-run) ---

                # One strict admission read controls the standalone path.
                # BLOCKED is an operator latch, contradictory labels fail
                # closed, and only exclusive GO short-circuits as approved.
                labels = self._read_plan_state_labels(issue_number)
                active_states = set(labels).intersection(ALL_STATE_LABELS)
                if STATE_PLAN_BLOCKED in active_states:
                    if not self.options.dry_run:
                        self._ensure_blocked_audit(issue_number)
                    logger.info(
                        "Issue %s: plan is BLOCKED; awaiting external intervention",
                        issue_ref(issue_number),
                    )
                    return WorkerResult(
                        issue_number=issue_number,
                        success=True,
                        already_reviewed=True,
                    )
                if len(active_states) > 1:
                    raise RuntimeError(f"contradictory plan-state labels: {sorted(active_states)}")
                if is_exclusive_plan_state(labels, STATE_PLAN_GO):
                    logger.info(
                        "Issue %s: latest plan review is APPROVED, skipping",
                        issue_ref(issue_number),
                    )
                    return WorkerResult(
                        issue_number=issue_number, success=True, already_reviewed=True
                    )

                # Skip if no plan exists
                plan_text = self._get_latest_plan(issue_number)
                if plan_text is None:
                    logger.info(
                        "Issue %s: no plan comment found, skipping", issue_ref(issue_number)
                    )
                    return WorkerResult(
                        issue_number=issue_number, success=True, already_reviewed=True
                    )

                # Fetch issue details for context
                self.status_tracker.update_slot(
                    acquired_slot, f"{issue_ref(issue_number)}: fetching issue"
                )
                try:
                    issue_data = gh_issue_json(issue_number)
                except Exception as e:
                    return WorkerResult(
                        issue_number=issue_number,
                        success=False,
                        error=f"Failed to fetch issue: {e}",
                    )

                issue_title: str = issue_data.get("title", f"Issue #{issue_number}")
                issue_body: str = issue_data.get("body", "")

                # Run Claude analysis
                self.status_tracker.update_slot(
                    acquired_slot, f"{issue_ref(issue_number)}: running Claude"
                )
                review_text = self._run_claude_analysis(
                    issue_number, issue_title, issue_body, plan_text
                )
                if review_text is None:
                    return WorkerResult(
                        issue_number=issue_number,
                        success=False,
                        error="Claude analysis returned no output",
                    )

                # --- DRY-RUN GUARD: no GitHub writes beyond this point ---
                if self.options.dry_run:
                    logger.info(
                        "[DRY RUN] Would post plan review to issue #%s:\n%s\n%s...",
                        issue_number,
                        _REVIEW_PREFIX,
                        review_text[:200],
                    )
                    return WorkerResult(issue_number=issue_number, success=True)

                # Post review comment
                self.status_tracker.update_slot(
                    acquired_slot, f"{issue_ref(issue_number)}: posting review"
                )
                self._post_review(issue_number, review_text)

                return WorkerResult(issue_number=issue_number, success=True)

            except Exception as e:
                logger.error("Issue %s: unexpected error: %s", issue_ref(issue_number), e)
                return WorkerResult(
                    issue_number=issue_number,
                    success=False,
                    error=str(e)[:80],
                )

    def _fetch_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        """Fetch the complete bounded issue journal, caching it per instance.

        Both ``_latest_review_is_final`` and ``_get_latest_plan`` call this
        helper so the ``gh issue view --comments`` API is hit only once per
        issue per worker invocation (#A3-009).

        Args:
            issue_number: GitHub issue number.

        Returns:
            Chronological comment dictionaries with normalized ownership.

        Raises:
            RuntimeError: GitHub cannot provide the complete bounded journal or
                the authenticated actor identity cannot be established.

        """
        if issue_number in self._comments_cache:
            return self._comments_cache[issue_number]

        comments = fetch_issue_comments_metadata(issue_number)
        viewer_login = (gh_current_login() or "").lower()
        if not viewer_login:
            raise RuntimeError("cannot verify GitHub comment ownership: viewer login unavailable")
        for comment in comments:
            if "viewerDidAuthor" in comment:
                continue
            author = comment.get("user") or comment.get("author") or {}
            login = author.get("login") if isinstance(author, dict) else ""
            comment["viewerDidAuthor"] = bool(login) and str(login).lower() == viewer_login

        self._comments_cache[issue_number] = comments
        return comments

    def _get_latest_plan(self, issue_number: int) -> str | None:
        """Return the body of the last comment that is the PLAN.

        Uses :meth:`_fetch_issue_comments` so the API call is shared with
        :meth:`_latest_review_is_final`.

        Selection rules (fixes the self-review bug of #455/#468/#484):
        - A plan comment must *start with* a plan heading, not merely contain
          one. A ``## 🔍 Plan Review`` body that quotes the plan contains
          ``## Objective``/``## Plan`` as substrings — matching those caused
          the reviewer to pick its own prior review as "the plan".
        - Review comments (``body.startswith(_REVIEW_PREFIX)``) are excluded
          outright, belt-and-suspenders.

        Args:
            issue_number: GitHub issue number.

        Returns:
            Plan comment body text, or None if no plan comment is found.

        """
        comments = self._fetch_issue_comments(issue_number)

        # Walk in reverse to find the *last* genuine plan comment.
        for comment in reversed(comments):
            if not bool(comment.get("viewerDidAuthor")):
                continue
            body: str = comment.get("body", "")
            stripped = body.lstrip()
            if stripped.startswith(_REVIEW_PREFIX):
                continue  # never treat a review comment as the plan
            # Match the single canonical marker ONLY at the start of the body
            # (anchored), never as a free substring.
            if is_plan_comment(stripped):
                logger.debug("Found plan comment for issue #%s", issue_number)
                return body

        return None

    def _ensure_blocked_audit(self, issue_number: int) -> None:
        """Repair an interrupted BLOCKED explanation without invoking an agent."""
        comments = [
            IssueComment(
                body=str(comment.get("body", "")),
                author_login=str(
                    (comment.get("user") or comment.get("author") or {}).get("login", "")
                ),
                viewer_did_author=bool(comment.get("viewerDidAuthor")),
                created_at=str(comment.get("createdAt") or comment.get("created_at") or ""),
                updated_at=str(comment.get("updatedAt") or comment.get("updated_at") or ""),
            )
            for comment in self._fetch_issue_comments(issue_number)
        ]
        body = blocked_audit_recovery_body(comments)
        if body is None:
            return
        gh_issue_upsert_owned_comment(
            issue_number,
            PLAN_REVIEW_CANONICAL_MARKER,
            body,
            legacy_marker=_REVIEW_PREFIX,
        )

    def _latest_review_is_final(self, issue_number: int) -> bool:
        """Return True iff GitHub carries the authoritative plan-GO label.

        Args:
            issue_number: GitHub issue number.

        Returns:
            True if GitHub currently carries ``state:plan-go``.

        """
        return is_exclusive_plan_state(
            self._read_plan_state_labels(issue_number),
            STATE_PLAN_GO,
        )

    @staticmethod
    def _read_plan_state_labels(issue_number: int) -> list[str]:
        """Strictly read the live plan-state labels for an authorization gate."""
        issue_data = gh_issue_json(issue_number)
        return [
            str(label.get("name")) if isinstance(label, dict) else str(label)
            for label in issue_data.get("labels", [])
            if isinstance(label, (dict, str))
        ]

    def _run_claude_analysis(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        plan_text: str,
        max_retries: int = 3,
    ) -> str | None:
        """Run Claude to produce a plan review.

        Calls ``claude --print`` with the review prompt piped to stdin.
        No filesystem tools are needed — the review is purely text-based.

        Why a retry loop: a 429 from the Claude CLI used to be silently
        swallowed (caught as generic Exception → None), so a single Anthropic
        outage corrupted the entire review phase. This now mirrors the
        planner's rate-limit handling — see :func:`scan_quota_reset` and
        :func:`wait_until`.

        Args:
            issue_number: GitHub issue number.
            issue_title: Issue title.
            issue_body: Issue body/description.
            plan_text: The full plan text to review.
            max_retries: Maximum retry attempts on rate-limit detection.

        Returns:
            Review text produced by Claude, or None on failure.

        """
        prompt = get_plan_review_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
        )

        if uses_direct_agent_runner(self.options.agent):
            return self._run_direct_agent_analysis(issue_number, prompt, max_retries=max_retries)

        repo_root = get_repo_root()
        repo = get_repo_slug(repo_root)

        try:
            stdout, _ = invoke_claude_with_session(
                repo=repo,
                issue=issue_number,
                agent=AGENT_PLAN_REVIEWER,
                prompt=prompt,
                model=reviewer_model(),
                cwd=repo_root,
                timeout=self.options.agent_timeout,
                allowed_tools="Read,Glob,Grep",
                input_via_stdin=True,
            )
            claude_output = (stdout or "").strip()
            if not claude_output:
                logger.error("Claude returned empty output for issue #%s", issue_number)
                return None
            return claude_output

        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            stdout_text = exc.stdout or ""
            reset_epoch = scan_quota_reset(stderr, stdout_text)
            if reset_epoch is not None and max_retries > 0:
                if reset_epoch > 0:
                    wait_until(reset_epoch)
                else:
                    time.sleep(5)
                return self._run_claude_analysis(
                    issue_number,
                    issue_title,
                    issue_body,
                    plan_text,
                    max_retries=max_retries - 1,
                )

            logger.error(
                "Claude returned exit code %s for issue #%s: %s",
                exc.returncode,
                issue_number,
                (stderr or stdout_text)[:200],
            )
            return None

        except subprocess.TimeoutExpired:
            logger.error("Claude timed out reviewing plan for issue #%s", issue_number)
            return None
        except FileNotFoundError:
            logger.error("'claude' CLI not found in PATH; cannot run plan review")
            return None
        except Exception as e:
            logger.error("Unexpected error calling Claude for issue #%s: %s", issue_number, e)
            return None

    def _run_direct_agent_analysis(
        self,
        issue_number: int,
        prompt: str,
        max_retries: int = 3,
    ) -> str | None:
        """Run a non-Claude direct agent to produce a plan review."""
        agent = self.options.agent
        try:
            result = run_agent_text(
                agent=agent,
                prompt=prompt,
                cwd=Path.cwd(),
                timeout=self.options.agent_timeout,
                model=direct_agent_model(agent, "HEPH_REVIEWER_MODEL"),
                sandbox="read-only",
            )
            output = (result.stdout or "").strip()
            if not output:
                logger.error("%s returned empty output for issue #%s", agent, issue_number)
                return None
            return output
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            stdout = e.stdout or ""
            reset_epoch = scan_quota_reset(stderr, stdout)
            if reset_epoch is not None and max_retries > 0:
                if reset_epoch > 0:
                    wait_until(reset_epoch)
                else:
                    time.sleep(5)
                return self._run_direct_agent_analysis(
                    issue_number,
                    prompt,
                    max_retries=max_retries - 1,
                )
            logger.error(
                "%s returned exit code %s for issue #%s: %s",
                agent,
                e.returncode,
                issue_number,
                (stderr or stdout)[:200],
            )
            return None
        except subprocess.TimeoutExpired:
            logger.error("%s timed out reviewing plan for issue #%s", agent, issue_number)
            return None
        except FileNotFoundError:
            logger.error("'%s' CLI not found in PATH; cannot run plan review", agent)
            return None
        except Exception as e:
            logger.error("Unexpected error calling %s for issue #%s: %s", agent, issue_number, e)
            return None

    def _post_review(self, issue_number: int, review_text: str) -> None:
        """Persist one canonical review and its authoritative state label.

        Legacy ``Verdict:`` output and ambiguous responses are rejected before
        any write. The explanatory audit record is published first for every
        outcome; only the subsequent GitHub label write changes durable state.
        A label-write failure therefore leaves the prior label authoritative,
        and the next standalone run may safely retry.

        Args:
            issue_number: GitHub issue number.
            review_text: Review body text from Claude.

        """
        state = parse_plan_review_state(review_text)
        if state is None:
            raise ValueError(
                "plan review must end with exactly one state:plan-go, "
                "state:plan-no-go, or explained state:plan-blocked token"
            )
        live_labels = self._read_plan_state_labels(issue_number)
        if STATE_PLAN_BLOCKED in live_labels and state != STATE_PLAN_BLOCKED:
            raise RuntimeError(
                "plan is blocked pending external intervention; automation cannot replace it"
            )
        active_states = set(live_labels).intersection(ALL_STATE_LABELS)
        if len(active_states) > 1 and STATE_PLAN_BLOCKED not in active_states:
            raise RuntimeError(f"contradictory plan-state labels: {sorted(active_states)}")
        revision = 1
        for comment in reversed(self._fetch_issue_comments(issue_number)):
            if not bool(comment.get("viewerDidAuthor")):
                continue
            body = str(comment.get("body", ""))
            if is_plan_comment(body):
                revision = comment_revision(body) or 1
                break
        comment_body = render_current_review(review_text, revision=revision)
        label_to_add, labels_to_remove = apply_plan_state(state)
        # BLOCKED is a safety latch, so make it durable before the fallible
        # audit write. GO/NOGO keep audit-first ordering. In every case only
        # a fresh exclusive label confirmation authorizes routing.
        if state == STATE_PLAN_BLOCKED:
            gh_issue_edit_labels(
                issue_number,
                add=[label_to_add],
                remove=labels_to_remove,
            )
        else:
            gh_issue_upsert_owned_comment(
                issue_number,
                PLAN_REVIEW_CANONICAL_MARKER,
                comment_body,
                legacy_marker=_REVIEW_PREFIX,
            )
            gh_issue_edit_labels(
                issue_number,
                add=[label_to_add],
                remove=labels_to_remove,
            )
        issue_data = gh_issue_json(issue_number)
        labels = [
            str(label.get("name")) if isinstance(label, dict) else str(label)
            for label in issue_data.get("labels", [])
            if isinstance(label, (dict, str))
        ]
        if not is_exclusive_plan_state(labels, state):
            raise RuntimeError(f"plan state label {state} was not confirmed exclusively")
        if state == STATE_PLAN_BLOCKED:
            gh_issue_upsert_owned_comment(
                issue_number,
                PLAN_REVIEW_CANONICAL_MARKER,
                comment_body,
                legacy_marker=_REVIEW_PREFIX,
            )
        logger.info("Posted plan review to issue #%s", issue_number)

    def _print_summary(self, results: dict[int, WorkerResult]) -> None:
        """Print a summary of plan review results.

        Args:
            results: Mapping of issue number to WorkerResult.

        """
        print_worker_summary("Plan Review Summary", results)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: Enable verbose (DEBUG) logging.

    """
    configure_cli_logging(verbose=verbose)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for plan_reviewer CLI."""
    parser = build_automation_parser(
        description="Review implementation plans posted to GitHub issues using Claude",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Review plans for specific issues
  %(prog)s --issues 123 456 789

  # Dry run (no GitHub writes)
  %(prog)s --issues 123 --dry-run

  # Review with more workers
  %(prog)s --issues 123 456 --max-workers 5

  # Verbose output
  %(prog)s --issues 123 -v
        """,
        add_github_throttle=False,
        dry_run_prefix="Suppress GitHub mutations (no review comments posted).",
        add_no_ui=True,
        add_version=False,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        required=True,
        help="Issue numbers whose plans should be reviewed",
    )
    add_agent_timeout_arg(parser)
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the plan reviewer CLI."""
    return _build_parser().parse_args(argv)


def main() -> int:
    """Execute the plan review workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt.

    """
    args = _parse_args()
    _setup_logging(args.verbose)
    agent = resolve_agent(args.agent)

    log = logging.getLogger(__name__)

    # Dedupe while preserving order — ``--issues 123 123`` would otherwise
    # post two reviews on the same issue.
    args.issues = list(dict.fromkeys(args.issues))

    log.info("Starting plan review for issues: %s", args.issues)

    work_units = 0
    shutdown = threading.Event()
    with work_report_context(lambda: work_units), terminal_guard(shutdown.set):
        try:
            options = PlanReviewerOptions(
                issues=args.issues,
                agent=agent,
                max_workers=args.max_workers,
                dry_run=args.dry_run,
                enable_ui=not args.no_ui and not args.json,
                verbose=args.verbose,
                agent_timeout=(
                    args.agent_timeout if args.agent_timeout is not None else DEFAULT_AGENT_TIMEOUT
                ),
            )

            reviewer = PlanReviewer(options)
            results = reviewer.run()

            # Compute work units for loop convergence (#613): non-skipped reviews
            work_units = sum(1 for r in results.values() if r.success and not r.already_reviewed)

            failed = [num for num, result in results.items() if not result.success]
            if failed:
                log.error("Failed to review %s plan(s) for issue(s): %s", len(failed), failed)
                if args.json:
                    emit_json_status(1, issues=args.issues, failed=failed)
                return 1

            log.info("Plan review complete")
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
