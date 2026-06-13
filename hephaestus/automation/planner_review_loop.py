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

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .claude_invoke import INFRA_ERROR_REVIEW_TEXT, parse_review_verdict
from .claude_models import planner_model, reviewer_model
from .claude_timeouts import learn_claude_timeout, planner_claude_timeout
from .git_utils import get_repo_root, issue_ref
from .github_api import (
    gh_issue_add_labels,
    gh_issue_json,
    gh_issue_remove_labels,
    gh_issue_upsert_comment,
)
from .learn import build_learn_prompt, mnemosyne_update_evidence
from .models import PLAN_COMMENT_MARKER
from .prompts import get_plan_loop_review_prompt, get_plan_prompt
from .review_state import PLAN_REVIEW_PREFIX
from .session_naming import AGENT_PLAN_REVIEWER, AGENT_PLANNER, reviewer_agent
from .state_labels import STATE_NEEDS_PLAN, STATE_PLAN_GO, STATE_PLAN_NO_GO

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 3

# Section headers a genuine plan is expected to contain at least one of. A body
# that contains NONE of these is a meta-narrative / changelog, not a plan — the
# planner agent occasionally returns such text (#693/#695 NOGO-exhaustion: the
# agent posted a "I fixed the comment" changelog instead of the plan, which the
# reviewer then correctly NOGO'd). We detect that shape and post a visible
# warning rather than letting a contentless body masquerade as the plan.
_PLAN_SECTION_HEADERS = (
    "objective",
    "approach",
    "files to create",
    "files to modify",
    "implementation order",
    "implementation steps",
    "verification",
    "skills used",
    "changes from review",
)

# Visible marker prepended (after the plan marker) when the body has no plan
# sections. Operators and the reviewer can grep for it; it is intentionally
# loud and machine-greppable.
_PLAN_CONTENT_MISSING_BANNER = (
    "> [!CAUTION]\n"
    "> **PLAN-CONTENT-MISSING** — The planner returned text containing no "
    "recognised plan sections (it looks like a changelog or status note, not a "
    "plan). This is posted verbatim for diagnosis but **must not be implemented**; "
    "the planner should re-plan with a full plan body (Objective, Approach, Files "
    "to Modify, Verification, etc.).\n\n"
)


def _plan_body_has_sections(body: str) -> bool:
    """Return True if ``body`` contains at least one recognised plan section header.

    Case-insensitive substring match on the known section names. A plan as
    terse as a single ``## Objective`` section passes; a pure narrative or
    changelog with none of the headers does not.
    """
    haystack = body.lower()
    return any(header in haystack for header in _PLAN_SECTION_HEADERS)


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
        """Generate implementation plan using the selected coding agent."""
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
        advise_findings: str = "",
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
        plan → upsert PLAN comment → capture learnings → independent review
        (fresh session, with pr-review-strict rubric) → upsert REVIEW comment →
        check verdict. Terminates on the first unambiguous GO or after
        :data:`MAX_REVIEW_ITERATIONS`.

        Each iteration upserts the plan and the review in place (one comment
        per role), so the issue holds at most one ``# Implementation Plan`` and
        one ``## 🔍 Plan Review`` comment at all times — never the 8-10
        appended duplicates that previously caused the reviewer to review its
        own prior review (#455/#468/#484).

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

            # Upsert the single long-lived PLAN comment for this iteration so
            # the issue always holds exactly one ``# Implementation Plan``
            # comment instead of accumulating one per re-plan (#455/#468/#484).
            self._upsert_plan_comment(
                issue_number, plan, re_planned=prior_review_for_plan is not None
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
                advise_findings=cached_advise,
            )

            verdict = parse_review_verdict(review_text)
            logger.info(
                "%s R%s: Verdict=%s Grade=%s",
                issue_ref(issue_number),
                iteration,
                verdict.verdict,
                verdict.grade or "?",
            )

            # Upsert the single long-lived REVIEW comment for this iteration so
            # the reviewer never re-reviews a stale verdict and the issue holds
            # exactly one ``## 🔍 Plan Review`` comment. The reviewer's GO/NOGO
            # verdict line is itself the gate the implementer reads.
            self._upsert_review_comment(issue_number, review_text)

            # Apply the state label corresponding to this iteration's verdict.
            # GO → state:plan-go (terminal — implementer trusts it absolutely).
            # NOGO (each iteration) → state:plan-no-go.
            # Either way, remove state:needs-plan (and the opposite terminal
            # label if it was set on a prior pass).
            # An ERROR verdict (reviewer-infra failure) is NOT a real verdict —
            # leave the plan labels untouched so the issue stays in its prior
            # state and gets re-reviewed next loop, rather than mislabeling a
            # never-reviewed plan as state:plan-no-go.
            if verdict.is_error:
                logger.warning(
                    "%s R%s: reviewer infrastructure failure — leaving plan labels "
                    "unchanged for re-review next loop",
                    issue_ref(issue_number),
                    iteration,
                )
            else:
                self._apply_state_label(issue_number, is_go=verdict.is_go)

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

    def _upsert_plan_comment(self, issue_number: int, plan: str, *, re_planned: bool) -> None:
        """Upsert the single PLAN comment for the current iteration.

        Ensures the issue holds exactly one ``# Implementation Plan`` comment,
        updated in place rather than appended. The body is normalised so it
        always begins with :data:`PLAN_COMMENT_MARKER` (the upsert helper keys
        off ``body.startswith(marker)``), prepending the marker when the model
        output omits it. When this plan is a re-plan driven by a prior NOGO
        review (``re_planned`` is ``True``), a ``## Changes from review``
        section is guaranteed to be present so the upserted plan documents what
        changed — a short generic note is appended as a defensive fallback if
        the model did not already include the section.

        Posting failure is non-fatal: it is logged as a warning and the loop
        continues, mirroring the fail-safe style of the rest of this module.

        Args:
            issue_number: GitHub issue number.
            plan: The plan text returned by ``_generate_plan``.
            re_planned: ``True`` when a prior review drove this re-plan, which
                requires the ``## Changes from review`` section.

        """
        # Normalise so ``body`` ALWAYS begins exactly at the marker (index 0).
        # ``plan.lstrip()`` in the passthrough is load-bearing: a plan that
        # arrives with leading whitespace (e.g. "\n\n# Implementation Plan…")
        # would otherwise keep the whitespace, breaking both ``startswith`` and
        # the banner-insert slice below (#700).
        stripped = plan.lstrip()
        body = (
            stripped
            if stripped.startswith(PLAN_COMMENT_MARKER)
            else f"{PLAN_COMMENT_MARKER}\n\n{stripped}"
        )
        # Guard (#695): if the model returned a contentless narrative/changelog
        # with no recognised plan sections, prepend a visible warning so the
        # reviewer and operators do not mistake it for a plan. We still post it
        # (for diagnosis) rather than dropping it silently.
        if not _plan_body_has_sections(body):
            logger.warning(
                "%s: planner output contains no recognised plan sections "
                "(possible changelog/meta-narrative); flagging with a "
                "PLAN-CONTENT-MISSING banner",
                issue_ref(issue_number),
            )
            # ``body`` is already marker-prefixed (normalised just above); insert
            # the banner immediately after the marker line without duplicating
            # the marker.
            after_marker = body[len(PLAN_COMMENT_MARKER) :].lstrip("\n")
            body = f"{PLAN_COMMENT_MARKER}\n\n{_PLAN_CONTENT_MISSING_BANNER}{after_marker}"
        if re_planned and "\n## Changes from review" not in f"\n{body}":
            body = (
                f"{body.rstrip()}\n\n## Changes from review\n\n"
                "This plan was revised to address the prior reviewer critique.\n"
            )
        try:
            gh_issue_upsert_comment(issue_number, PLAN_COMMENT_MARKER, body)
        except Exception as e:
            logger.warning(
                "%s: failed to upsert plan comment (non-fatal): %s",
                issue_ref(issue_number),
                e,
            )

    def _upsert_review_comment(self, issue_number: int, review_text: str) -> None:
        """Upsert the single REVIEW comment for the current iteration.

        Ensures the issue holds exactly one ``## 🔍 Plan Review`` comment,
        updated in place rather than appended. The body is normalised so it
        always begins with :data:`PLAN_REVIEW_PREFIX` (the upsert helper keys
        off ``body.startswith(marker)``), prepending the prefix when the model
        output omits it.

        The reviewer's ``Verdict: GO|NOGO`` line is the gate the implementer
        reads (via :func:`is_plan_review_go`, the same
        :func:`parse_review_verdict` this loop uses), so the review text is
        posted as-is — no verdict translation.

        Posting failure is non-fatal: it is logged as a warning and the loop
        continues, mirroring the fail-safe style of the rest of this module.

        Args:
            issue_number: GitHub issue number.
            review_text: The review text returned by ``_run_plan_review``.

        """
        body = (
            review_text
            if review_text.lstrip().startswith(PLAN_REVIEW_PREFIX)
            else f"{PLAN_REVIEW_PREFIX}\n\n{review_text}"
        )
        try:
            gh_issue_upsert_comment(issue_number, PLAN_REVIEW_PREFIX, body)
        except Exception as e:
            logger.warning(
                "%s: failed to upsert review comment (non-fatal): %s",
                issue_ref(issue_number),
                e,
            )

    def _apply_state_label(self, issue_number: int, *, is_go: bool) -> None:
        """Apply the verdict's ``state:*`` label and remove the others (#704).

        The state-label family is mutually exclusive. On GO, set
        ``state:plan-go`` and remove both ``state:plan-no-go`` and
        ``state:needs-plan``. On NOGO (each iteration), set
        ``state:plan-no-go`` and remove ``state:plan-go`` (in case a prior
        run had set it) plus ``state:needs-plan``.

        Failure is non-fatal: the label apply/remove is best-effort, mirroring
        the upsert helpers above. The reviewer's verdict comment is still the
        ultimate fallback for the backfill path.

        Args:
            issue_number: GitHub issue number.
            is_go: ``True`` when the reviewer's verdict is GO; ``False`` for
                NOGO (either per-iteration or NOGO-exhausted).

        """
        if is_go:
            label_to_add = STATE_PLAN_GO
            labels_to_remove = [STATE_PLAN_NO_GO, STATE_NEEDS_PLAN]
        else:
            label_to_add = STATE_PLAN_NO_GO
            labels_to_remove = [STATE_PLAN_GO, STATE_NEEDS_PLAN]
        try:
            gh_issue_add_labels(issue_number, [label_to_add])
        except Exception as e:
            logger.warning(
                "%s: failed to add label %r (non-fatal): %s",
                issue_ref(issue_number),
                label_to_add,
                e,
            )
        try:
            gh_issue_remove_labels(issue_number, labels_to_remove)
        except Exception as e:
            logger.warning(
                "%s: failed to remove labels %s (non-fatal): %s",
                issue_ref(issue_number),
                labels_to_remove,
                e,
            )

    def generate_plan(
        self,
        issue_number: int,
        max_retries: int = 3,
        *,
        prior_review: str | None = None,
        cached_advise: str | None = None,
        cached_issue_data: dict[str, Any] | None = None,
    ) -> str:
        """Generate implementation plan using the selected coding agent.

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
        """Ask the planner session to run ``/learn`` for the plan it just wrote.

        Resumes the planner's own session (``AGENT_PLANNER``) rather than
        opening a separate learnings session, so the model still "remembers"
        the plan it just wrote and can introspect its own reasoning rather
        than re-reading the plan cold. The prompt uses the user-facing
        ``/learn`` skill command so the useful bits are written to
        ProjectMnemosyne, then asks for short bullets to pass to the reviewer.
        Failure here is non-fatal — return empty string and let the review
        proceed without learnings.

        The pre-review learnings step inherits the planner's model
        (``planner_model()``) — /learn always runs on the same tier as the
        phase it is learning from, rather than carrying its own knob.

        Args:
            issue_number: GitHub issue number (used in prompt for grounding).
            plan: The plan text the planner just produced.

        Returns:
            Bullet-point learnings text, or "" on any failure.

        """
        context = (
            f"You just produced an implementation plan for GitHub issue "
            f"#{issue_number}. Below is the plan you wrote.\n\n"
            "Capture durable planning learnings for ProjectMnemosyne. Focus on:\n"
            "- The most uncertain assumptions in your plan\n"
            "- Any external sources, files, or APIs you relied on without "
            "directly verifying them\n"
            "- Risks the reviewer should focus on\n\n"
            "After the /learn update, reply with only 3-5 brief bullets for the "
            "plan reviewer — no preamble, no headers.\n\n"
            "---\n\n"
            f"{plan}"
        )
        prompt = build_learn_prompt(context)
        _learn_start = time.monotonic()
        try:
            output = self.planner._call_claude(
                prompt,
                model=planner_model(),
                agent=AGENT_PLANNER,
                issue_number=issue_number,
                timeout=learn_claude_timeout(),
            )
            self._write_planner_learn_record(
                issue_number, succeeded=True, output=output, start=_learn_start
            )
            return output
        except Exception as e:
            logger.warning(
                "%s: planner-learnings capture failed (non-fatal): %s", issue_ref(issue_number), e
            )
            self._write_planner_learn_record(
                issue_number, succeeded=False, error=str(e), start=_learn_start
            )
            return ""

    def _planner_learn_state_dir(self) -> Path:
        state_dir = get_repo_root() / "build" / ".issue_implementer"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    def _write_planner_learn_record(
        self,
        issue_number: int,
        *,
        succeeded: bool,
        output: str = "",
        error: str = "",
        start: float = 0.0,
    ) -> None:
        """Persist explicit planner ``/learn`` attempt evidence.

        Planner learn is best-effort and must never block planning, but the
        automation loop needs durable evidence that the learn step was tried
        and whether it actually succeeded.
        """
        try:
            state_dir = self._planner_learn_state_dir()
            timestamp = datetime.now(timezone.utc).isoformat()
            duration = round(time.monotonic() - start, 3) if start else None
            record_file = state_dir / f"planner-learn-{issue_number}.json"
            log_file = state_dir / f"planner-learn-{issue_number}.log"
            record = {
                "issue_number": issue_number,
                "learn_attempted_at": timestamp,
                "learn_duration_s": duration,
                "learn_status": "succeeded" if succeeded else "failed",
                "log_path": str(log_file),
            }
            if succeeded:
                record.update(mnemosyne_update_evidence(output))
            else:
                record.update(
                    {
                        "mnemosyne_update_status": "failed",
                        "mnemosyne_update_urls": [],
                        "mnemosyne_update_pr_numbers": [],
                    }
                )
            if error:
                record["error"] = error
            record_file.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
            log_file.write_text(output if succeeded else f"FAILED: {error}\n")
        except OSError as exc:
            logger.warning(
                "%s: failed to write planner-learnings state (non-fatal): %s",
                issue_ref(issue_number),
                exc,
            )

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
        advise_findings: str = "",
    ) -> str:
        """Run a reviewer pass on the current plan.

        The reviewer's session is distinct from the planner's (different
        ``agent`` string in the session UUID) so it stays unbiased by the
        planner's internal state. Each iteration also uses a *fresh* reviewer
        session via :func:`reviewer_agent` (the ``-r{iteration}`` token) so the
        reviewer never inherits — and therefore never re-reviews — its own prior
        verdict. Uses ``reviewer_model()`` (Sonnet by default).

        Args:
            issue_number: GitHub issue number.
            issue_title: Issue title.
            issue_body: Issue body.
            plan_text: Plan to review.
            learnings: Planner-captured learnings for this iteration.
            iteration: Iteration index (0, 1, or 2).
            prior_review: Previous iteration's review text, or ``None`` on iter 0.
            advise_findings: Prior team learnings from ProjectMnemosyne, shared
                with the plan reviewer so review also runs with advise context.

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
            advise_findings=advise_findings,
        )
        try:
            return self.planner._call_claude(
                prompt,
                model=reviewer_model(),
                agent=reviewer_agent(AGENT_PLAN_REVIEWER, iteration),
                issue_number=issue_number,
                timeout=planner_claude_timeout(),
            )
        except Exception as e:
            logger.error(
                "%s R%s: reviewer call failed: %s; recording ERROR (re-review next loop, "
                "no plan-no-go label) so an infra failure isn't mistaken for a NOGO",
                issue_ref(issue_number),
                iteration,
                e,
            )
            return (
                f"Reviewer invocation failed at iteration {iteration}: {e}\n\n"
                f"{INFRA_ERROR_REVIEW_TEXT}"
            )
