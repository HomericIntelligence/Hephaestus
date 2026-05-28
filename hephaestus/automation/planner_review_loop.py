"""Strict plan review loop for :class:`Planner`.

Owns the bounded ``plan → capture learnings → review`` iteration cycle that
runs per issue:

1. Pre-fetch issue + run advise once before the loop.
2. Up to :data:`MAX_REVIEW_ITERATIONS` iterations of
   ``_generate_plan → _capture_planner_learnings → _run_plan_review``.
3. Stop on the first unambiguous GO; otherwise feed the review back and
   re-plan.

Extracted from ``planner.py`` (#598) so the coordinator class stays focused
on the worker-pool driver. No behavior change.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .claude_invoke import parse_review_verdict
from .claude_models import learn_model, planner_model, reviewer_model
from .claude_timeouts import planner_claude_timeout
from .git_utils import issue_ref
from .github_api import gh_issue_json
from .prompts import get_plan_loop_review_prompt, get_plan_prompt
from .session_naming import AGENT_LEARNINGS, AGENT_PLAN_REVIEWER, AGENT_PLANNER

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 3


class PlannerHost(Protocol):
    """Minimal Planner surface the review loop depends on.

    Declares only the methods and attributes PlanReviewLoop calls, allowing
    the loop to depend on this interface rather than the concrete Planner's
    full private surface (Dependency Inversion Principle). Implements
    structural substitutability — any object with these members satisfies
    the protocol.

    Method names retain their underscore prefix because they are the existing
    call surface. This is deliberate: the test seam (e.g.,
    ``patch.object(planner, "_generate_plan", ...)``) targets these private
    names, and renaming them is out of scope. The docstring here makes the
    contract explicit and documented.
    """

    options: Any
    status_tracker: Any

    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Search team knowledge base for relevant prior learnings."""
        pass

    def _generate_plan(
        self,
        issue_number: int,
        max_retries: int = 3,
        *,
        prior_review: str | None = None,
        cached_advise: str | None = None,
        cached_issue_data: dict[str, Any] | None = None,
    ) -> str:
        """Generate implementation plan using Claude Code."""
        pass

    def _capture_planner_learnings(self, issue_number: int, plan: str) -> str:
        """Capture learnings from the generated plan."""
        pass

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
        """Run a reviewer pass on the current plan."""
        pass

    def _call_claude(
        self,
        prompt: str,
        *,
        model: str,
        agent: str,
        issue_number: int | str,
        max_retries: int = 3,
        timeout: int = 300,
        extra_args: list[str] | None = None,
    ) -> str:
        """Call Claude with the given prompt."""
        pass


class PlanReviewLoop:
    """Bounded plan→learn→review iteration loop.

    Holds a back-reference to a :class:`PlannerHost` and intentionally routes
    per-issue work back through ``planner._generate_plan`` /
    ``planner._capture_planner_learnings`` / ``planner._run_plan_review``
    rather than calling :class:`PlannerClaudeRunner` directly. The indirection
    preserves the existing ``patch.object(planner, "_generate_plan", ...)``
    test seam: a patched method on the host instance still intercepts the
    loop's inner calls.

    Depends on the minimal :class:`PlannerHost` protocol rather than the
    concrete Planner class, making the loop substitutable for testing and
    allowing implementation flexibility.
    """

    def __init__(self, planner: PlannerHost) -> None:
        """Bind the loop to its owning planner host.

        Args:
            planner: A :class:`PlannerHost` instance (typically a Planner)
                whose options, status tracker, and Claude helpers this loop reuses.

        """
        self.planner = planner

    @property
    def options(self) -> Any:
        """Shortcut to the planner's options."""
        return self.planner.options

    @property
    def status_tracker(self) -> Any:
        """Shortcut to the planner's status tracker."""
        return self.planner.status_tracker

    # ------------------------------------------------------------------
    # Strict review loop — advise → loop[plan → learn → review] → post
    # ------------------------------------------------------------------

    def run(self, issue_number: int, slot_id: int) -> tuple[str, str | None, int, bool]:
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
        issue_data = gh_issue_json(issue_number)
        issue_title = issue_data.get("title", f"Issue #{issue_number}")
        issue_body = issue_data.get("body", "")

        cached_advise = ""
        if self.options.enable_advise:
            cached_advise = self.planner._run_advise(issue_number, issue_title, issue_body)

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

            # Route through planner's delegation methods so test patches on
            # Planner._generate_plan / _capture_planner_learnings / _run_plan_review
            # continue to intercept the calls.
            plan = self.planner._generate_plan(
                issue_number,
                prior_review=prior_review_for_plan,
                cached_advise=cached_advise,
                cached_issue_data=issue_data,
            )

            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: capturing learnings [R{iteration}]"
            )
            learnings = self.planner._capture_planner_learnings(issue_number, plan)

            self.status_tracker.update_slot(
                slot_id, f"{issue_ref(issue_number)}: reviewing plan [R{iteration}]"
            )
            review_text = self.planner._run_plan_review(
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

            prior_review_for_plan = review_text

        if not final_verdict_is_go:
            logger.warning(
                "%s: review loop exhausted %s iteration(s) without a GO verdict — "
                "plan posted with NOGO-exhausted status",
                issue_ref(issue_number),
                iterations_run,
            )

        return plan, review_text, iterations_run, final_verdict_is_go

    def generate_plan(
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
        if cached_issue_data is not None:
            issue_data = cached_issue_data
        else:
            issue_data = gh_issue_json(issue_number)
        issue_title = issue_data.get("title", f"Issue #{issue_number}")
        issue_body = issue_data.get("body", "")

        advise_findings = cached_advise if cached_advise is not None else ""
        if cached_advise is None and self.options.enable_advise:
            advise_findings = self.planner._run_advise(issue_number, issue_title, issue_body)

        prompt = get_plan_prompt(issue_number)

        context_parts = [f"# Issue #{issue_number}: {issue_title}", "", issue_body]

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

        plan = self.planner._call_claude(
            context,
            model=planner_model(),
            agent=AGENT_PLANNER,
            issue_number=issue_number,
            timeout=planner_claude_timeout(),
        )

        return plan

    def capture_planner_learnings(self, issue_number: int, plan: str) -> str:
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
            return self.planner._call_claude(
                prompt,
                model=learn_model(),
                agent=AGENT_LEARNINGS,
                issue_number=issue_number,
                timeout=120,
            )
        except Exception as e:
            logger.warning(
                "%s: planner-learnings capture failed (non-fatal): %s", issue_ref(issue_number), e
            )
            return ""

    def run_plan_review(
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
        """Run a reviewer pass on the current plan.

        The reviewer's session is distinct from the planner's (different
        ``agent`` string in the session UUID) so it stays unbiased by the
        planner's internal state, but it resumes itself across review
        iterations so successive critiques compound. Uses ``reviewer_model()``
        (Sonnet by default).

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
            return self.planner._call_claude(
                prompt,
                model=reviewer_model(),
                agent=AGENT_PLAN_REVIEWER,
                issue_number=issue_number,
                timeout=planner_claude_timeout(),
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
