"""Bulk issue planning using Claude Code.

Provides:
- Parallel issue planning
- Duplicate plan detection
- Rate limit handling
- Plan posting to GitHub issues
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import add_agent_argument, is_codex, run_codex_text
from hephaestus.github.rate_limit import wait_until

from .claude_invoke import parse_review_verdict, scan_quota_reset
from .claude_models import advise_model, learn_model, planner_model, reviewer_model
from .claude_timeouts import planner_claude_timeout
from .git_utils import get_repo_root, issue_ref
from .github_api import (
    GitHubRateLimitError,
    _gh_call,
    gh_issue_comment,
    gh_issue_json,
    gh_list_open_issues,
    prefetch_issue_states,
)
from .models import PLAN_COMMENT_MARKERS, PlannerOptions, PlanResult
from .prompts import (
    get_advise_prompt,
    get_plan_loop_review_prompt,
    get_plan_prompt,
)
from .status_tracker import StatusTracker

MAX_REVIEW_ITERATIONS = 3

logger = logging.getLogger(__name__)


class Planner:
    """Plans GitHub issues using Claude Code.

    Supports parallel planning with rate limit handling and
    duplicate detection.
    """

    _mnemosyne_lock: threading.Lock = threading.Lock()

    def __init__(self, options: PlannerOptions):
        """Initialize planner.

        Args:
            options: Planner configuration options

        """
        self.options = options
        self.status_tracker = StatusTracker(options.parallel)
        self.results: dict[int, PlanResult] = {}
        self.lock = threading.Lock()

    def run(self) -> dict[int, PlanResult]:
        """Run the planner on all issues.

        Returns:
            Dictionary mapping issue number to PlanResult

        """
        logger.info(
            "Planning %s issues with %s parallel workers",
            len(self.options.issues),
            self.options.parallel,
        )

        # Filter closed issues if requested
        issues_to_plan = self._filter_issues()

        if not issues_to_plan:
            logger.warning("No issues to plan")
            return {}

        # Plan issues in parallel
        with ThreadPoolExecutor(max_workers=self.options.parallel) as executor:
            futures: dict[Future[Any], int] = {}

            for issue_num in issues_to_plan:
                future = executor.submit(self._plan_issue, issue_num)
                futures[future] = issue_num

            # Collect results
            for future in as_completed(futures):
                issue_num = futures[future]
                try:
                    result = future.result()
                    with self.lock:
                        self.results[issue_num] = result
                except Exception as e:
                    logger.error("Failed to plan %s: %s", issue_ref(issue_num), e)
                    with self.lock:
                        self.results[issue_num] = PlanResult(
                            issue_number=issue_num,
                            success=False,
                            error=str(e),
                        )

        self._print_summary()
        return self.results

    def _filter_issues(self) -> list[int]:
        """Filter issues based on options.

        Returns:
            List of issue numbers to plan

        """
        issues_to_plan = []

        # Batch fetch issue states if we need to check for closed issues
        cached_states = {}
        if self.options.skip_closed:
            cached_states = prefetch_issue_states(self.options.issues)

        for issue_num in self.options.issues:
            # Check if already planned (unless force)
            if not self.options.force and self._has_existing_plan(issue_num):
                logger.info("Issue #%s already has a plan, skipping", issue_num)
                with self.lock:
                    self.results[issue_num] = PlanResult(
                        issue_number=issue_num,
                        success=True,
                        plan_already_exists=True,
                    )
                continue

            # Check if closed (using cached states)
            if self.options.skip_closed:
                state = cached_states.get(issue_num)
                if state and state.value == "CLOSED":
                    logger.info("Issue #%s is closed, skipping", issue_num)
                    continue

            issues_to_plan.append(issue_num)

        return issues_to_plan

    def _has_existing_plan(self, issue_number: int) -> bool:
        """Check if an issue already has a plan in comments.

        Args:
            issue_number: Issue number to check

        Returns:
            True if plan exists

        """
        try:
            result = _gh_call(
                [
                    "issue",
                    "view",
                    str(issue_number),
                    "--comments",
                    "--json",
                    "comments",
                ],
            )

            data = json.loads(result.stdout)
            comments = data.get("comments", [])

            # Look for plan markers in comments (shared with plan_reviewer)
            for comment in comments:
                body = comment.get("body", "")
                if any(marker in body for marker in PLAN_COMMENT_MARKERS):
                    logger.debug("Found existing plan for %s", issue_ref(issue_number))
                    return True

            return False

        except Exception as e:
            logger.warning(
                "Failed to check for existing plan on %s: %s", issue_ref(issue_number), e
            )
            return False

    def _plan_issue(self, issue_number: int) -> PlanResult:
        """Plan a single issue.

        Args:
            issue_number: Issue number to plan

        Returns:
            PlanResult

        """
        slot_id = self.status_tracker.acquire_slot()
        if slot_id is None:
            return PlanResult(
                issue_number=issue_number,
                success=False,
                error="Failed to acquire worker slot",
            )

        try:
            self.status_tracker.update_slot(slot_id, f"Planning {issue_ref(issue_number)}")

            if self.options.dry_run:
                logger.info("[DRY RUN] Would plan %s", issue_ref(issue_number))
                return PlanResult(issue_number=issue_number, success=True)

            # Run the strict review loop: advise → loop[plan → learn → review]
            # → post final plan with last review attached. Loop terminates on
            # the first unambiguous GO or after MAX_REVIEW_ITERATIONS.
            plan, final_review, iterations, verdict_is_go = self._run_plan_review_loop(
                issue_number, slot_id
            )

            # Post final plan + review to issue regardless of verdict so
            # operators can see what was produced (NOGO banner is appended
            # inside _post_plan when verdict_is_go is False).
            self._post_plan(
                issue_number, plan, final_review=final_review, verdict_is_go=verdict_is_go
            )

            self.status_tracker.update_slot(
                slot_id, f"Completed {issue_ref(issue_number)} ({iterations} iter)"
            )

            if not verdict_is_go:
                return PlanResult(
                    issue_number=issue_number,
                    success=False,
                    error=(
                        "review loop exhausted all iterations without a GO verdict (NOGO-exhausted)"
                    ),
                )

            return PlanResult(issue_number=issue_number, success=True)

        except Exception as e:
            logger.error("Failed to plan %s: %s", issue_ref(issue_number), e)
            return PlanResult(
                issue_number=issue_number,
                success=False,
                error=str(e),
            )
        finally:
            self.status_tracker.release_slot(slot_id)

    def _call_claude(
        self,
        prompt: str,
        *,
        model: str,
        max_retries: int = 3,
        timeout: int = 300,
        extra_args: list[str] | None = None,
    ) -> str:
        """Call Claude CLI with retry logic for rate limits.

        Args:
            prompt: The prompt to send to Claude
            model: Claude model ID to pass to ``--model`` (caller picks per phase
                via :mod:`hephaestus.automation.claude_models`)
            max_retries: Maximum retry attempts for rate limits
            timeout: Timeout in seconds
            extra_args: Additional CLI arguments

        Returns:
            Claude's response text

        Raises:
            RuntimeError: If Claude call fails

        """
        if is_codex(self.options.agent):
            return self._call_codex(prompt, model=model, max_retries=max_retries, timeout=timeout)

        # Build command. ``--model`` pins the phase-appropriate model so this
        # call doesn't burn quota on whatever the user's CLI default is.
        cmd = [
            "claude",
            "--model",
            model,
            "--print",
            prompt,
            "--output-format",
            "text",
        ]

        # Add system prompt if configured
        if self.options.system_prompt_file and self.options.system_prompt_file.exists():
            cmd.extend(["--system-prompt", str(self.options.system_prompt_file)])

        # Add extra args
        if extra_args:
            cmd.extend(extra_args)

        # Invoke Claude
        try:
            # Preserve existing environment and set CLAUDECODE
            env = os.environ.copy()
            env["CLAUDECODE"] = ""

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout,
                env=env,  # Avoid nested-session guard
                stdin=subprocess.DEVNULL,
            )

            response = result.stdout.strip()

            if not response:
                raise RuntimeError("Claude returned empty response")

            return response

        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            stdout = e.stdout or ""

            # Rate-limit messages may appear in either stream. The Claude CLI
            # in particular returns its 429 ("You're out of extra usage ·
            # resets ...") inside the stdout JSON payload as the ``result``
            # field of an ``is_error: true`` response, not in stderr.
            reset_epoch = scan_quota_reset(stderr, stdout)
            if reset_epoch is not None and max_retries > 0:
                if reset_epoch > 0:
                    wait_until(reset_epoch)
                else:
                    # No reset time, wait a bit
                    import time

                    time.sleep(5)
                # Retry with decremented counter
                return self._call_claude(
                    prompt,
                    model=model,
                    max_retries=max_retries - 1,
                    timeout=timeout,
                    extra_args=extra_args,
                )

            # Surface stdout too — the CLI's 429 message lives there, and an
            # empty stderr in the original logs hid the actual cause.
            detail = stderr or stdout or "(no output)"
            raise RuntimeError(f"Claude failed: {detail}") from e

        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Claude timed out after {timeout}s") from e

    def _call_codex(
        self,
        prompt: str,
        *,
        model: str,
        max_retries: int = 3,
        timeout: int = 300,
    ) -> str:
        """Call Codex CLI with retry logic for rate limits."""
        try:
            result = run_codex_text(
                prompt,
                cwd=get_repo_root(),
                timeout=timeout,
                sandbox="workspace-write",
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            stdout = e.stdout or ""
            reset_epoch = scan_quota_reset(stderr, stdout)
            if reset_epoch is not None and reset_epoch > 0 and max_retries > 0:
                logger.warning("Codex usage cap hit; waiting for reset")
                wait_until(reset_epoch)
                return self._call_codex(
                    prompt,
                    model=model,
                    max_retries=max_retries - 1,
                    timeout=timeout,
                )
            detail = stderr or stdout or str(e)
            raise RuntimeError(f"Codex failed: {detail}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Codex timed out after {timeout}s") from e

        response = (result.stdout or "").strip()
        if not response:
            raise RuntimeError("Codex returned empty response")
        return response

    def _ensure_mnemosyne(self, mnemosyne_root: Path) -> bool:
        """Clone ProjectMnemosyne if it does not exist locally.

        Uses a class-level threading lock and an fcntl file lock to prevent
        race conditions when multiple parallel workers call this simultaneously.

        Args:
            mnemosyne_root: Expected local path for ProjectMnemosyne

        Returns:
            True if the directory exists (or was cloned successfully), False otherwise

        """
        with Planner._mnemosyne_lock:
            # TOCTOU guard: re-check inside the lock
            if mnemosyne_root.exists():
                # Refresh stale clone with a fast-forward pull
                try:
                    subprocess.run(
                        ["git", "-C", str(mnemosyne_root), "pull", "--ff-only"],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    logger.debug("ProjectMnemosyne refreshed at %s", mnemosyne_root)
                except Exception as e:
                    logger.warning(
                        "Failed to refresh ProjectMnemosyne (using existing clone): %s", e
                    )
                return True

            lock_path = mnemosyne_root.parent / ".mnemosyne.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)

            with open(lock_path, "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    # Re-check after acquiring file lock
                    if mnemosyne_root.exists():
                        return True

                    logger.info("Cloning ProjectMnemosyne to %s...", mnemosyne_root)
                    subprocess.run(
                        [
                            "gh",
                            "repo",
                            "clone",
                            "HomericIntelligence/ProjectMnemosyne",
                            str(mnemosyne_root),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    logger.info("ProjectMnemosyne cloned successfully")
                    # NOTE: do NOT unlink lock_path here — the file-lock sentinel
                    # must remain on disk until the fd closes in the finally block.
                    # Unlinking while LOCK_EX is held lets a second process open a
                    # new inode at the same path and grab its own lock, breaking
                    # cross-process mutual exclusion (#370).
                    return True

                except subprocess.TimeoutExpired:
                    logger.warning(
                        "gh repo clone timed out after 120 s; ProjectMnemosyne unavailable this run"
                    )
                    return False

                except subprocess.CalledProcessError as e:
                    logger.warning("Failed to clone ProjectMnemosyne: %s", e.stderr or e)
                    return False

                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Search team knowledge base for relevant prior learnings.

        Args:
            issue_number: Issue number
            issue_title: Issue title
            issue_body: Issue body/description

        Returns:
            Advise findings text, or empty string if advise fails

        """
        try:
            # Locate ProjectMnemosyne
            repo_root = get_repo_root()
            mnemosyne_root = repo_root / "build" / "ProjectMnemosyne"

            if not mnemosyne_root.exists() and not self._ensure_mnemosyne(mnemosyne_root):
                return self._advise_skipped("ProjectMnemosyne unavailable")

            marketplace_path = mnemosyne_root / ".claude-plugin" / "marketplace.json"
            if not marketplace_path.exists():
                logger.warning(
                    "Marketplace file not found at %s; "
                    "attempting recovery re-clone of ProjectMnemosyne",
                    marketplace_path,
                )
                shutil.rmtree(mnemosyne_root, ignore_errors=True)
                if not self._ensure_mnemosyne(mnemosyne_root) or not marketplace_path.exists():
                    logger.error(
                        "Recovery failed: marketplace.json still missing at %s; "
                        "skipping advise step",
                        marketplace_path,
                    )
                    return self._advise_skipped(f"marketplace.json missing at {marketplace_path}")

            # Build advise prompt
            advise_prompt = get_advise_prompt(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                marketplace_path=str(marketplace_path),
                repo_root=str(repo_root),
            )

            # Call Claude with shorter timeout. /advise is light search work
            # so it runs on the cheap model.
            logger.info("Running advise for %s...", issue_ref(issue_number))
            findings = self._call_claude(advise_prompt, model=advise_model(), timeout=180)

            return findings

        except Exception as e:
            logger.warning("Advise step failed for %s: %s", issue_ref(issue_number), e)
            return self._advise_skipped(f"unexpected error: {e}")

    @staticmethod
    def _advise_skipped(reason: str) -> str:
        """Return a marker string for plans that ran without advise findings.

        A silent ``""`` made it impossible for the implementer (or a human
        reading the plan) to tell whether advise wasn't attempted, was
        attempted but found nothing, or actually failed.
        """
        return f"<!-- advise step skipped: {reason} -->"

    def _generate_plan(
        self,
        issue_number: int,
        max_retries: int = 3,
        *,
        prior_review: str | None = None,
        cached_advise: str | None = None,
        cached_issue_data: dict[str, Any] | None = None,
    ) -> str:
        """Generate implementation plan using Claude Code.

        Args:
            issue_number: Issue number to plan
            max_retries: Maximum retry attempts for rate limits
            prior_review: When set, the previous review-loop iteration's NoGo
                critique. Injected into the prompt so the planner can address
                the findings on this iteration.
            cached_advise: Pre-computed advise findings (avoids re-running advise
                on every loop iteration). When ``None`` and advise is enabled,
                advise runs once.
            cached_issue_data: Pre-fetched issue JSON to avoid duplicate API calls.

        Returns:
            Generated plan text

        Raises:
            RuntimeError: If plan generation fails

        """
        # Fetch issue data
        if cached_issue_data is not None:
            issue_data = cached_issue_data
        else:
            issue_data = gh_issue_json(issue_number)
        issue_title = issue_data.get("title", f"Issue #{issue_number}")
        issue_body = issue_data.get("body", "")

        # Run advise step if enabled (use cache if provided)
        advise_findings = cached_advise if cached_advise is not None else ""
        if cached_advise is None and self.options.enable_advise:
            advise_findings = self._run_advise(issue_number, issue_title, issue_body)

        # Build prompt
        prompt = get_plan_prompt(issue_number)

        # Add issue context
        context_parts = [f"# Issue #{issue_number}: {issue_title}", "", issue_body]

        # Inject advise findings if available
        if advise_findings:
            context_parts.extend(
                [
                    "",
                    "---",
                    "",
                    "## Prior Learnings from Team Knowledge Base",
                    "",
                    advise_findings,
                ]
            )

        # Inject prior review feedback if this is a re-plan
        if prior_review:
            context_parts.extend(
                [
                    "",
                    "---",
                    "",
                    "## Prior reviewer critique — your previous plan got NOGO",
                    "",
                    "Address every concrete finding below in your revised plan:",
                    "",
                    prior_review,
                ]
            )

        context_parts.extend(["", "---", "", prompt])

        context = "\n".join(context_parts)

        # Call Claude to generate plan. Plans are small but reasoning-heavy,
        # so this is the right place to spend Opus.
        plan = self._call_claude(context, model=planner_model(), timeout=planner_claude_timeout())

        return plan

    def _post_plan(
        self,
        issue_number: int,
        plan: str,
        *,
        final_review: str | None = None,
        verdict_is_go: bool = True,
    ) -> None:
        """Post plan to issue as a comment.

        Args:
            issue_number: Issue number
            plan: Plan text
            final_review: When set, the last reviewer output (Grade + Verdict +
                rationale) is appended in a collapsible section so the human
                reviewer can see why the loop terminated.
            verdict_is_go: When ``False`` a visible NOGO banner is prepended to
                the comment so operators can tell at a glance that the review loop
                exhausted all iterations without approval (#369).

        """
        nogo_banner = ""
        if not verdict_is_go:
            nogo_banner = (
                "> [!WARNING]\n"
                "> **NOGO-EXHAUSTED** — The strict review loop ran all "
                f"{MAX_REVIEW_ITERATIONS} iterations without an unambiguous GO verdict. "
                "This plan was posted for operator review but **should not be implemented** "
                "until a human approves it.\n\n"
            )

        comment_body = f"""# Implementation Plan

{nogo_banner}{plan}
"""

        if final_review:
            comment_body += f"""
---

<details>
<summary>Final review verdict (from strict review loop)</summary>

{final_review}

</details>
"""

        comment_body += """
---
*Generated by Claude Code Planner (strict review loop)*
"""

        gh_issue_comment(issue_number, comment_body)
        logger.info("Posted plan to %s", issue_ref(issue_number))

    # ------------------------------------------------------------------
    # Strict review loop — advise → loop[plan → learn → review] → post
    # ------------------------------------------------------------------

    def _run_plan_review_loop(
        self, issue_number: int, slot_id: int
    ) -> tuple[str, str | None, int, bool]:
        """Run the bounded review loop for a single issue.

        Pre-fetches the issue and runs advise once, then iterates:
        plan → capture learnings → independent review (fresh session, with
        pr-review-strict rubric) → check verdict. Terminates on the first
        unambiguous GO or after :data:`MAX_REVIEW_ITERATIONS`.

        Args:
            issue_number: GitHub issue number.
            slot_id: Worker slot id for status updates.

        Returns:
            Tuple of (final plan text, final review text or None, iterations run,
            final_verdict_is_go). The fourth element is ``True`` only when the loop
            terminated with an unambiguous GO verdict; ``False`` when the loop
            exhausted all iterations without a GO (NOGO-exhausted).

        """
        # Pre-fetch issue once and cache for the whole loop
        issue_data = gh_issue_json(issue_number)
        issue_title = issue_data.get("title", f"Issue #{issue_number}")
        issue_body = issue_data.get("body", "")

        # Advise runs once before the loop — same findings inform every iteration
        cached_advise = ""
        if self.options.enable_advise:
            cached_advise = self._run_advise(issue_number, issue_title, issue_body)

        plan = ""
        review_text: str | None = None
        prior_review_for_plan: str | None = None
        iterations_run = 0
        final_verdict_is_go = False

        for iteration in range(MAX_REVIEW_ITERATIONS):
            iterations_run = iteration + 1
            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: planning [R{iteration}]"
            )

            plan = self._generate_plan(
                issue_number,
                prior_review=prior_review_for_plan,
                cached_advise=cached_advise,
                cached_issue_data=issue_data,
            )

            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: capturing learnings [R{iteration}]"
            )
            learnings = self._capture_planner_learnings(issue_number, plan)

            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: reviewing plan [R{iteration}]"
            )
            review_text = self._run_plan_review(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                plan_text=plan,
                learnings=learnings,
                iteration=iteration,
                prior_review=review_text,
            )

            verdict = parse_review_verdict(review_text)
            logger.info(
                "%s R%s: Verdict=%s Grade=%s",
                issue_ref(issue_number),
                iteration,
                verdict.verdict,
                verdict.grade or "?",
            )

            if verdict.is_go:
                logger.info(
                    "%s: GO on iteration %s — loop terminated",
                    issue_ref(issue_number),
                    iteration,
                )
                final_verdict_is_go = True
                break

            # NoGo or AMBIGUOUS — feed this review back into next plan iteration
            prior_review_for_plan = review_text

        if not final_verdict_is_go:
            logger.warning(
                "%s: review loop exhausted %s iteration(s) without a GO verdict — "
                "plan posted with NOGO-exhausted status",
                issue_ref(issue_number),
                iterations_run,
            )

        return plan, review_text, iterations_run, final_verdict_is_go

    def _capture_planner_learnings(self, issue_number: int, plan: str) -> str:
        """Ask Claude to summarize what the planner just learned.

        These learnings are passed to the reviewer alongside the plan, giving
        the reviewer extra signal about which aspects the planner is most/least
        confident in. Failure here is non-fatal — return empty string and let
        the review proceed without learnings.

        Uses ``learn_model()`` (Haiku by default) per the per-phase model
        selection in :mod:`hephaestus.automation.claude_models`.

        Args:
            issue_number: GitHub issue number (used in prompt for grounding).
            plan: The plan text the planner just produced.

        Returns:
            Bullet-point learnings text, or "" on any failure.

        """
        prompt = (
            f"You just produced an implementation plan for GitHub issue "
            f"#{issue_number}. Below is the plan you wrote.\n\n"
            "List 3-5 brief bullets describing:\n"
            "- The most uncertain assumptions in your plan\n"
            "- Any external sources, files, or APIs you relied on without "
            "directly verifying them\n"
            "- Risks the reviewer should focus on\n\n"
            "Output only the bullets — no preamble, no headers.\n\n"
            "---\n\n"
            f"{plan}"
        )
        try:
            return self._call_claude(prompt, model=learn_model(), timeout=120)
        except Exception as e:
            logger.warning(
                "%s: planner-learnings capture failed (non-fatal): %s", issue_ref(issue_number), e
            )
            return ""

    def _run_plan_review(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        plan_text: str,
        learnings: str,
        iteration: int,
        prior_review: str | None,
    ) -> str:
        """Run a fresh-session reviewer pass on the current plan.

        Uses a separate ``claude --print`` invocation (no ``--resume``) so each
        review is unbiased by the planner session. Applies the strict
        ``review-pr-strict`` rubric embedded in the prompt. Uses
        ``reviewer_model()`` (Sonnet by default).

        Args:
            issue_number: GitHub issue number.
            issue_title: Issue title.
            issue_body: Issue body.
            plan_text: Plan to review.
            learnings: Planner-captured learnings for this iteration.
            iteration: Iteration index (0, 1, or 2).
            prior_review: Previous iteration's review text, or ``None`` on iter 0.

        Returns:
            Review text. On reviewer-call failure, returns a synthetic NoGo
            review so the loop can continue (failing safe — never silently GO).

        """
        prompt = get_plan_loop_review_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
            learnings=learnings,
            iteration=iteration,
            prior_review=prior_review,
        )
        try:
            return self._call_claude(
                prompt, model=reviewer_model(), timeout=planner_claude_timeout()
            )
        except Exception as e:
            logger.error(
                "%s R%s: reviewer call failed: %s; treating as NOGO so the loop continues",
                issue_ref(issue_number),
                iteration,
                e,
            )
            return (
                f"Reviewer invocation failed at iteration {iteration}: {e}\n\n"
                "Grade: F\nVerdict: NOGO\n"
            )

    def _print_summary(self) -> None:
        """Print summary of planning results."""
        total = len(self.results)
        successful = sum(1 for r in self.results.values() if r.success)
        already_planned = sum(1 for r in self.results.values() if r.plan_already_exists)
        failed = total - successful

        logger.info("=" * 60)
        logger.info("Planning Summary")
        logger.info("=" * 60)
        logger.info("Total issues: %s", total)
        logger.info("Successfully planned: %s", successful - already_planned)
        logger.info("Already planned: %s", already_planned)
        logger.info("Failed: %s", failed)

        if failed > 0:
            logger.info("\nFailed issues:")
            for issue_num, result in self.results.items():
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
    """Parse command line arguments for the planner CLI."""
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="Bulk plan GitHub issues using Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Plan all open issues (no arguments needed)
  %(prog)s

  # Plan specific issues
  %(prog)s --issues 123 456 789

  # Force re-plan even if plan exists
  %(prog)s --issues 123 --force

  # Dry run (no actual planning)
  %(prog)s --issues 123 --dry-run

  # Use custom system prompt
  %(prog)s --issues 123 --system-prompt .claude/agents/planner.md

  # Plan with more parallelism
  %(prog)s --issues 123 456 789 --parallel 5
        """,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        help="Issue numbers to plan (default: all open issues)",
    )
    add_agent_argument(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Suppress GitHub mutations (no issue comments posted). NOTE: Claude "
            "is still invoked to generate plans — dry-run still incurs full "
            "Claude token cost. It is for correctness rehearsal, not cost preview."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-planning even if plan already exists",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=3,
        choices=range(1, 33),
        metavar="N",
        help="Number of parallel workers, 1-32 (default: 3)",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        help="Path to system prompt file for Claude Code",
    )
    parser.add_argument(
        "--no-skip-closed",
        action="store_true",
        help="Plan closed issues (default: skip closed issues)",
    )
    parser.add_argument(
        "--no-advise",
        action="store_true",
        help="Skip the advise step (don't search team knowledge base before planning)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    return parser.parse_args()


def main() -> int:
    """Execute the issue planning workflow.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt

    """
    args = _parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger(__name__)
    log.info("Starting issue planner")

    if not args.issues:
        try:
            discovered = gh_list_open_issues()
        except GitHubRateLimitError as e:
            # Don't smear a 100-line traceback across the driver's loop output
            # when the only problem is that the GraphQL hourly budget is gone.
            # Exit cleanly so run_automation_loop.sh moves on to the next repo.
            log.error(
                "GitHub API rate-limited; cannot discover issues this run "
                "(reset at epoch %s). Skipping cleanly.",
                e.reset_epoch,
            )
            return 0
        log.info("No --issues given; discovered %s open issues: %s", len(discovered), discovered)
        args.issues = discovered

    # Dedupe while preserving first-seen order. dict.fromkeys is the
    # canonical "ordered set" trick. Without this, ``--issues 123 123``
    # would race two workers on the same issue and produce double-posts.
    args.issues = list(dict.fromkeys(args.issues))

    log.info("Issues to plan: %s", args.issues)

    try:
        options = PlannerOptions(
            issues=args.issues,
            agent=args.agent,
            dry_run=args.dry_run,
            force=args.force,
            parallel=args.parallel,
            system_prompt_file=args.system_prompt,
            skip_closed=not args.no_skip_closed,
            enable_advise=not args.no_advise,
        )

        planner = Planner(options)
        results = planner.run()

        failed = [num for num, result in results.items() if not result.success]
        if failed:
            log.error("Failed to plan %s issue(s): %s", len(failed), failed)
            return 1

        log.info("Planning complete")
        return 0
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning("Interrupted by user")
        return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
