"""Strict in-loop review + address phase.

Extracted from :class:`ImplementationPhaseRunner` as part of the #712
decomposition. :class:`ReviewPhase` owns the bounded review→address→re-review
cycle (#28/#1083/#1152): each iteration runs a fresh reviewer that posts inline
PR threads, validates that prior comments were truly addressed, gates GO on
zero unresolved threads, and resumes the implementer session to fix what
remains. It also owns the diff/changed-file collectors, the review-log
persistence helpers, and the per-PR labeling of the final verdict.

The module-level helper ``_is_automation_owned_thread`` lives here because the
GO-gate thread accounting is its only caller.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hephaestus.agents.runtime import (
    is_codex,
    resume_codex_session,
    run_codex_text,
)
from hephaestus.github.client import ClaudeUsageCapError, gh_call
from hephaestus.github.rate_limit import resolve_quota_reset_epoch, wait_until

from . import review_state
from ._stage_context import StageMixin
from .address_review import run_address_fix_session
from .claude_invoke import (
    INFRA_ERROR_REVIEW_TEXT,
    SESSION_EXPIRED_PHRASES,
    detect_server_overload,
    invoke_claude_with_session,
    parse_review_verdict,
)
from .claude_models import implementer_model, reviewer_model
from .claude_timeouts import implementer_claude_timeout
from .git_utils import (
    get_repo_info,
    get_repo_slug,
    issue_ref,
    pr_ref,
    push_current_branch_with_lease_on_divergence,
    rebase_worktree_onto,
    run,
    sync_worktree_to_remote_branch,
)
from .github_api import (
    gh_current_login,
    gh_issue_add_labels,
    gh_pr_list_unresolved_threads,
)
from .models import PLAN_COMMENT_MARKER, ImplementationState
from .pr_manager import (
    commit_changes,
    enable_auto_merge_after_implementation_go,
    mark_pr_implementation_go,
    mark_pr_implementation_no_go,
)
from .pr_reviewer import gather_impl_review_context, review_pr_inline
from .prompts import get_impl_loop_review_prompt, get_impl_resume_feedback_prompt
from .review_validator import validate_prior_comments_addressed
from .session_naming import AGENT_IMPLEMENTER, current_trunk_githash  # noqa: F401
from .state_labels import STATE_SKIP

if TYPE_CHECKING:
    from ._stage_context import StageContext

logger = logging.getLogger(__name__)

# Base review-budget: a loop that is NOT making progress (a stuck or oscillating
# reviewer) terminates after this many iterations and is tagged ``state:skip``.
MAX_REVIEW_ITERATIONS = 3

# Absolute ceiling on review iterations. A loop that makes genuine progress every
# round (resolves a fresh finding without the validator re-opening a prior one)
# earns extra iterations beyond ``MAX_REVIEW_ITERATIONS`` — up to this hard cap —
# so a steadily-improving PR with more real findings than the base budget can
# converge to a clean GO instead of being stranded one address-pass short (#1554).
# The cap bounds a truly non-converging reviewer so it can never spin forever.
MAX_REVIEW_ITERATIONS_HARD_CAP = MAX_REVIEW_ITERATIONS * 2

# Bounded exponential backoff for transient 529 server-overload responses
# (5s → 10s → 20s, capped). Unlike a 429 quota cap these carry no reset epoch,
# so the correct response is a short backoff, not a wait-until-reset.
_OVERLOAD_BACKOFF_BASE_SECONDS = 5
_OVERLOAD_BACKOFF_MAX_SECONDS = 20


def _handle_reviewer_quota_or_overload(
    error: Exception, *, issue_number: int, iteration: int
) -> None:
    """Block on a Claude quota cap / server overload before recording ERROR.

    The in-loop PR-review path used to catch a 429 session-limit failure, record
    a synthetic ``Verdict=ERROR``, and immediately re-review — firing fresh
    reviewer sessions against an exhausted quota (issue #1528). The implement
    phase already detects the cap and blocks; this helper gives the review path
    the same behavior so both honor the same transient-failure families.

    The reviewer subprocess surfaces the 429/529 text inside the ``RuntimeError``
    message (``Analysis session failed for PR …: <CLI output>``), so the
    classifiers read ``str(error)``. A :class:`ClaudeUsageCapError` raised by the
    central ``is_error`` envelope guard carries the reset epoch as an attribute
    instead (its message has no reset phrasing), so that is honored first.

    Args:
        error: The exception raised by the reviewer invocation.
        issue_number: Issue under review (for log context).
        iteration: Current review iteration; also drives the overload backoff.

    """
    # A typed cap carries the reset epoch directly — prefer it over text scanning.
    cap_reset = (
        getattr(error, "reset_epoch", None) if isinstance(error, ClaudeUsageCapError) else None
    )
    text = str(error)
    reset_epoch = cap_reset if cap_reset is not None else resolve_quota_reset_epoch(text)
    if reset_epoch is not None and reset_epoch > 0:
        logger.warning(
            "#%s R%s: in-loop PR review hit Claude usage cap; waiting for reset",
            issue_number,
            iteration,
        )
        wait_until(reset_epoch)
        return
    if detect_server_overload(text):
        delay = min(
            _OVERLOAD_BACKOFF_BASE_SECONDS * (2**iteration),
            _OVERLOAD_BACKOFF_MAX_SECONDS,
        )
        logger.warning(
            "#%s R%s: in-loop PR review hit server overload; backing off %ss",
            issue_number,
            iteration,
            delay,
        )
        time.sleep(delay)


def _is_automation_owned_thread(thread: dict[str, Any], current_login: str | None) -> bool:
    """Return True for unresolved review threads the automation may resolve on GO."""
    authors = {str(author).strip() for author in thread.get("authors", []) if str(author).strip()}
    author = (thread.get("author") or "").strip()
    if author:
        authors.add(author)
    for comment in thread.get("comments", []):
        comment_author = (comment.get("author") or "").strip()
        if comment_author:
            authors.add(comment_author)

    if current_login and current_login in authors:
        return True
    automation_bot_logins = {
        "github-actions[bot]",
        "hephaestus[bot]",
    }
    return bool(authors & automation_bot_logins)


class ReviewPhase(StageMixin):
    """Run the bounded review + address cycle and label the final verdict."""

    def _repo_name_with_owner(self) -> str:
        """Return ``OWNER/REPO`` for gh commands that require an explicit repo."""
        owner, repo = get_repo_info(self.repo_root)
        return f"{owner}/{repo}"

    def __init__(self, ctx: StageContext) -> None:
        """Store the shared :class:`StageContext`."""
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Final-verdict labeling
    # ------------------------------------------------------------------

    def _apply_impl_review_verdict(
        self,
        *,
        issue_number: int,
        pr_number: int,
        last_verdict: str | None,
        slot_id: int | None,
        thread_id: int | None,
    ) -> None:
        """Label a PR from the review loop's final verdict and arm auto-merge.

        Shared by both the fresh-implementation path and the existing-PR review
        path so the GO → ``mark_pr_implementation_go`` (+ auto-merge) /
        non-GO → ``mark_pr_implementation_no_go`` mapping cannot drift between
        them.

        An ``ERROR`` verdict (reviewer-infrastructure failure) or a
        ``HUMAN_BLOCKED`` verdict (review reached GO but an unresolved human
        review thread remains) applies **neither** label: the PR is not settled,
        so it must be left unlabeled for the "no go/no-go label → re-review" path
        to pick it up next loop (#911 / PR #1069). Labeling it no-go would falsely
        record a converged failure; labeling it go would arm auto-merge on a PR
        that was never reviewed (ERROR) or still has open human threads
        (HUMAN_BLOCKED).
        """
        impl = self.impl
        if last_verdict in ("ERROR", "HUMAN_BLOCKED"):
            reason = (
                "reviewer-infrastructure error"
                if last_verdict == "ERROR"
                else "unresolved human review thread(s)"
            )
            impl._log(
                "warning",
                f"{issue_ref(issue_number)}: implementation review blocked by {reason}; "
                f"leaving {pr_ref(pr_number)} unlabeled for re-review "
                "(no implementation go/no-go label, auto-merge unchanged)",
                thread_id,
            )
            return
        if last_verdict == "GO":
            mark_pr_implementation_go(pr_number)
            if self.options.auto_merge:
                if slot_id is not None:
                    self.status_tracker.update_slot(
                        slot_id, f"{pr_ref(pr_number)}: enabling auto-merge"
                    )
                enable_auto_merge_after_implementation_go(pr_number)
        else:
            mark_pr_implementation_no_go(pr_number)
            impl._log(
                "warning",
                f"{issue_ref(issue_number)}: implementation review did not reach GO; "
                f"auto-merge remains disabled for {pr_ref(pr_number)}",
                thread_id,
            )

    # ------------------------------------------------------------------
    # Prior-thread validation
    # ------------------------------------------------------------------

    def _validate_prior_threads(
        self,
        *,
        issue_number: int,
        pr_number: int | None,
        branch_name: str,
        worktree_path: Path,
        prior_threads: list[dict[str, Any]],
        iteration: int,
        thread_id: int | None,
        prior_reopened_keys: set[str],
    ) -> tuple[list[str], bool, set[str]]:
        """Re-open prior review comments the current diff does not address.

        Runs the read-only validation sub-agent
        (:func:`review_validator.validate_prior_comments_addressed`) against the
        previous iteration's threads.

        Returns ``(reopened_ids, is_clean, reopened_keys)``: the IDs of any
        inline threads it re-opened (empty when there is no PR, no prior threads,
        on the first iteration, in dry-run, or when everything was addressed);
        ``is_clean`` False when at least one finding was re-opened (including
        PR-level findings that produce no inline thread id, #1329); and the
        cumulative set of stable re-open keys to thread forward (#1329) so a
        documented by-design recurrence is accepted once and never re-added.
        """
        if pr_number is None or not prior_threads or self.options.dry_run:
            return [], True, prior_reopened_keys
        diff_text = self.impl._collect_diff(worktree_path, branch_name)
        reopened, is_clean, reopened_keys = validate_prior_comments_addressed(
            pr_number=pr_number,
            issue_number=issue_number,
            worktree_path=worktree_path,
            prior_threads=prior_threads,
            diff_text=diff_text,
            agent=self.options.agent,
            iteration=iteration,
            state_dir=self.state_dir,
            dry_run=False,
            prior_reopened_keys=prior_reopened_keys,
        )
        if not is_clean:
            self.impl._log(
                "warning",
                f"{issue_ref(issue_number)} R{iteration}: validator re-opened "
                f"{len(reopened)} prior review comment(s) the diff did not address",
                thread_id,
            )
        return reopened, is_clean, reopened_keys

    # ------------------------------------------------------------------
    # Strict review loop
    # ------------------------------------------------------------------

    def _evaluate_go_verdict(
        self,
        *,
        issue_number: int,
        pr_number: int | None,
        thread_id: int | None,
    ) -> tuple[str, bool, bool]:
        """Resolve a reviewer GO against the PR's still-open threads.

        A reviewer ``GO`` only converges when the PR has ZERO unresolved review
        threads (#1152): a reviewer can emit GO in the same pass that posts new
        findings, or while prior automation threads remain open. This counts
        what is still open (resolving nothing) and maps it to a terminal state.

        Returns ``(verdict, go_blocked_by_automation, should_break)``:

        * ``verdict`` — ``"GO"`` (clean), ``"NOGO"`` (automation threads remain,
          address + re-review), or ``"HUMAN_BLOCKED"`` (a human thread is open).
        * ``go_blocked_by_automation`` — force the address step to run even on a
          GO pass that posted no new threads.
        * ``should_break`` — terminate the loop now (clean GO or HUMAN_BLOCKED).
        """
        impl = self.impl
        automation_unresolved = 0
        human_unresolved = 0
        if pr_number is not None and not self.options.dry_run:
            (
                automation_unresolved,
                human_unresolved,
            ) = self._count_unresolved_threads_blocking_go(
                issue_number=issue_number,
                pr_number=pr_number,
                thread_id=thread_id,
            )
        if human_unresolved and pr_number is not None:
            # A GO cannot stand while a HUMAN review thread is open. Automation
            # must NOT resolve it and cannot fix it — a human has to. Break with
            # a distinct terminal state (no spin to exhaustion, no state:skip) so
            # the PR stays unlabeled, awaiting the human; the loop re-runs next
            # pass via the "no go/no-go label → re-review" path once threads
            # resolve.
            impl._log(
                "info",
                f"{pr_ref(pr_number)}: reviewer said GO but "
                f"{human_unresolved} unresolved human review thread(s) remain "
                f"— not accepting GO; awaiting human resolution, "
                f"leaving PR unlabeled",
                thread_id,
            )
            return "HUMAN_BLOCKED", False, True
        if automation_unresolved and pr_number is not None:
            # GO + open automation thread(s): the work is NOT actually done.
            # Downgrade to NOGO so the address step (below) fixes and resolves
            # them, and the next iteration re-reviews to confirm.
            # ``go_blocked_by_automation`` forces the address step to run even
            # though this GO pass may have posted no new threads (otherwise the
            # zero-thread guard would skip it and the loop would spin
            # GO→downgrade to exhaustion).
            impl._log(
                "info",
                f"{pr_ref(pr_number)}: reviewer said GO but "
                f"{automation_unresolved} unresolved automation review thread(s) "
                "remain — addressing and re-reviewing before GO can stand",
                thread_id,
            )
            return "NOGO", True, False
        impl._log(
            "info",
            f"{pr_ref(pr_number) if pr_number is not None else issue_ref(issue_number)}"
            f": GO on iteration — all review threads resolved, "
            "review loop terminated",
            thread_id,
        )
        return "GO", False, True

    def _validate_and_review(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        branch_name: str,
        worktree_path: Path,
        session_id: str | None,
        slot_id: int | None,
        thread_id: int | None,
        pr_number: int | None,
        iteration: int,
        prior_review: str | None,
        prior_addressed_threads: list[dict[str, Any]],
        prior_reopened_keys: set[str],
        advise_findings: str,
    ) -> tuple[list[str], str, list[str], Any, bool, set[str]]:
        """Validate prior threads, run one review, and parse the verdict.

        Step 1 (#1152): before the fresh review, verify (via the read-only
        sub-agent) that every PRIOR review comment was truly addressed by the
        current diff — resolving the ones confirmed fixed and re-opening the ones
        that aren't. On iteration 0 of the existing-PR path there is no
        prior-address snapshot, so seed it with the PR's currently unresolved
        threads; otherwise pre-existing threads would never be verified and the
        GO gate would (wrongly) ignore them.

        The seed only applies to the existing-PR review path (``session_id is
        None`` — see ``_review_existing_pr``): that PR arrives with threads from
        earlier loops that must be re-verified before a GO. The
        fresh-implementation path (``session_id`` set) has no prior threads at
        iteration 0 — its threads are posted by R0's own review — so seeding
        there would wrongly validate not-yet-addressed comments against an empty
        diff.

        Returns ``(reopened, review_text, posted_thread_ids, verdict,
        validator_clean, reopened_keys)`` for the caller to drive convergence
        on. ``validator_clean`` is False whenever the validator re-opened a
        finding (inline OR PR-level), and ``reopened_keys`` threads the
        recurrence state forward across rounds (#1329).
        """
        impl = self.impl
        threads_to_validate = prior_addressed_threads
        if (
            not threads_to_validate
            and session_id is None
            and pr_number is not None
            and not self.options.dry_run
        ):
            with contextlib.suppress(Exception):
                threads_to_validate = gh_pr_list_unresolved_threads(pr_number, dry_run=False)
        reopened, validator_clean, reopened_keys = self.runner._validate_prior_threads(
            issue_number=issue_number,
            pr_number=pr_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            prior_threads=threads_to_validate,
            iteration=iteration,
            thread_id=thread_id,
            prior_reopened_keys=prior_reopened_keys,
        )

        # Review step: a fresh reviewer session posts inline PR threads and
        # returns its verdict text. ``prior_review`` carries the previous
        # iteration's critique forward as reviewer context.
        if slot_id is not None:
            review_ref = pr_ref(pr_number) if pr_number is not None else issue_ref(issue_number)
            self.status_tracker.update_slot(slot_id, f"{review_ref}: reviewing impl [R{iteration}]")
        review_text, posted_thread_ids = impl._run_impl_review_step(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            branch_name=branch_name,
            worktree_path=worktree_path,
            pr_number=pr_number,
            iteration=iteration,
            prior_review=prior_review,
            advise_findings=advise_findings,
        )
        impl._save_review_log(issue_number, iteration, review_text)

        verdict = parse_review_verdict(review_text)
        impl._log(
            "info",
            f"{issue_ref(issue_number)} R{iteration}: Verdict={verdict.verdict} "
            f"Grade={verdict.grade or '?'} threads={len(posted_thread_ids)} "
            f"reopened={len(reopened)}",
            thread_id,
        )

        # A2-005: Persist review iteration progress so --resume can skip
        # already-completed iterations.  Persist BEFORE the caller may break out
        # so the final iteration's data is always on disk.
        impl._save_review_iteration_state(issue_number, iteration + 1, review_text)
        return reopened, review_text, posted_thread_ids, verdict, validator_clean, reopened_keys

    # ------------------------------------------------------------------
    # Pre-review conflict gate (#1328)
    # ------------------------------------------------------------------

    def _pr_merge_state(self, pr_number: int) -> tuple[str, str]:
        """Return ``(mergeStateStatus, mergeable)`` upper-cased for *pr_number*.

        Mirrors the merge-state query the CI driver uses
        (``ci_driver._gh_pr_state`` / ``_attempt_mechanical_rebase``). Returns
        empty strings on any query failure so an unknown merge-state is never
        misread as CONFLICTING.
        """
        try:
            result = gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    self._repo_name_with_owner(),
                    "--json",
                    "mergeStateStatus,mergeable",
                ],
            )
            state = dict(json.loads(result.stdout or "{}"))
        except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as exc:
            logger.warning(
                "#%d: could not fetch PR %s merge-state for conflict gate: %s",
                pr_number,
                pr_ref(pr_number),
                exc,
            )
            return "", ""
        return (
            str(state.get("mergeStateStatus") or "").upper(),
            str(state.get("mergeable") or "").upper(),
        )

    def _resolve_conflict_before_review(
        self,
        *,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        branch_name: str,
        session_id: str | None,
        slot_id: int | None,
        thread_id: int | None,
        state: ImplementationState | None,
    ) -> bool:
        """Ensure *pr_number* is conflict-free before the review iterations (#1328).

        Reviewing a PR that has a merge conflict with its base is pointless — a
        conflicted PR can never merge, so any GO verdict would be wasted spend.
        Per the user's instruction this resolves the conflict with the
        IMPLEMENTATION agent BEFORE the first review iteration.

        The flow reuses the SAME primitives the CI driver's ``_resolve_dirty_pr``
        uses (``rebase_worktree_onto`` / ``push_current_branch_with_lease_on_divergence``
        from :mod:`git_utils`, and the implementer-session resume in
        ``_resume_impl_with_feedback``), rather than reinventing conflict
        resolution. ``ReviewPhase`` runs under ``IssueImplementer`` — a different
        object than ``CIDriver`` — so the helper cannot be called directly; this
        is the smallest seam that reuses the underlying rebase/agent primitives.

        Steps:

        1. Query merge-state. If NOT DIRTY/CONFLICTING, return ``True`` (nothing
           to do — proceed straight to review). An unknown merge-state also
           returns ``True`` so a transient gh failure never strands a healthy PR.
        2. Mechanical rebase onto the base. A clean rebase + lease-push
           re-triggers CI; return ``True`` once the merge-state clears.
        3. Still conflicting → dispatch the implementation agent with explicit
           conflict-resolution instructions, commit + push, then re-check.
        4. Return ``True`` only if the PR is conflict-free afterwards; ``False``
           if the conflict could not be resolved (caller returns early so the PR
           is treated as not-GO rather than reviewed).

        Returns:
            ``True`` if the PR is conflict-free (review may proceed); ``False``
            if an unresolved merge conflict remains.

        """
        if self.options.dry_run:
            return True

        merge_state, mergeable = self._pr_merge_state(pr_number)
        if merge_state not in ("DIRTY", "CONFLICTING") and mergeable != "CONFLICTING":
            return True

        impl = self.impl
        impl._log(
            "warning",
            f"{issue_ref(issue_number)}: {pr_ref(pr_number)} is {merge_state or 'CONFLICTING'} "
            "(merge conflict) before review — resolving with the implementation agent "
            "before any review iteration",
            thread_id,
        )
        if slot_id is not None:
            self.status_tracker.update_slot(
                slot_id, f"{pr_ref(pr_number)}: resolving merge conflict"
            )

        # Resolve the base branch once for both the rebase target and the agent
        # prompt. Best-effort: default to ``main`` like ``_resolve_dirty_pr``.
        base_branch = "main"
        try:
            base_result = gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    self._repo_name_with_owner(),
                    "--json",
                    "baseRefName",
                ],
            )
            base_branch = dict(json.loads(base_result.stdout or "{}")).get("baseRefName") or "main"
        except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as exc:
            logger.debug(
                "#%d: failed to determine base branch for %s; defaulting to 'main': %s",
                issue_number,
                pr_ref(pr_number),
                exc,
            )

        # 1. Cheap path: mechanical rebase. A clean rebase resolves a PR that is
        #    merely behind / non-overlapping with no agent spend.
        with contextlib.suppress(subprocess.CalledProcessError):
            sync_worktree_to_remote_branch(worktree_path, branch_name)
            if rebase_worktree_onto(worktree_path, base_branch):
                push_current_branch_with_lease_on_divergence(
                    worktree_path,
                    branch=branch_name,
                    push_ref=f"HEAD:{branch_name}",
                )
                logger.info(
                    "#%d: mechanically rebased %s onto %s (no agent) for conflict gate",
                    issue_number,
                    pr_ref(pr_number),
                    base_branch,
                )
                cleared_state, cleared_mergeable = self._pr_merge_state(pr_number)
                if cleared_state not in ("DIRTY", "CONFLICTING") and (
                    cleared_mergeable != "CONFLICTING"
                ):
                    return True

        # 2. Rebase still conflicts → the implementation agent resolves it. Reuse
        #    the same conflict_context wording as ci_driver._resolve_dirty_pr.
        if session_id is None:
            # Without an implementer session to resume there is no agent seam in
            # this phase to drive a code-level conflict resolution; bail so the
            # PR is treated as not-GO rather than reviewed-while-conflicted.
            impl._log(
                "warning",
                f"{issue_ref(issue_number)}: {pr_ref(pr_number)} still conflicts after rebase and "
                "has no implementer session to resume — skipping review (not-GO)",
                thread_id,
            )
            return False

        conflict_context = (
            f"This PR has a MERGE CONFLICT with `origin/{base_branch}` "
            f"(mergeStateStatus=DIRTY) — it cannot merge until the conflict is "
            f"resolved. Rebase the PR head branch onto `origin/{base_branch}` and "
            f"resolve every conflict, keeping both the PR's intent and the latest "
            f"base changes. Then commit the resolution (signed). There may be NO "
            f"failing CI checks — the conflict itself is the blocker."
        )
        resolved = self._resume_impl_with_feedback(
            session_id=session_id,
            worktree_path=worktree_path,
            issue_number=issue_number,
            review_text=conflict_context,
            prev_iteration=-1,
            verdict="CONFLICT",
            state=state,
        )
        if resolved and self.runner._commit_if_changes(issue_number, worktree_path):
            self.runner._push_branch(branch_name, worktree_path)

        final_state, final_mergeable = self._pr_merge_state(pr_number)
        if final_state in ("DIRTY", "CONFLICTING") or final_mergeable == "CONFLICTING":
            impl._log(
                "warning",
                f"{issue_ref(issue_number)}: {pr_ref(pr_number)} still has an unresolved merge "
                "conflict after agent resolution — skipping review (not-GO)",
                thread_id,
            )
            return False
        return True

    def _run_impl_review_loop(
        self,
        *,
        issue_number: int,
        worktree_path: Path,
        branch_name: str,
        issue_title: str,
        issue_body: str,
        session_id: str | None,
        slot_id: int | None,
        thread_id: int | None,
        state: ImplementationState | None = None,
        pr_number: int | None = None,
        advise_findings: str = "",
    ) -> tuple[int, str | None, str | None]:
        """Run the bounded in-loop review + address cycle for an implementation.

        Each iteration posts inline PR review threads and returns a verdict; on
        NOGO the implementer session is resumed to address threads. Terminates on
        GO or zero blocking threads, or on budget exhaustion. The budget starts
        at :data:`MAX_REVIEW_ITERATIONS` and is extended one iteration at a time —
        up to :data:`MAX_REVIEW_ITERATIONS_HARD_CAP` — for as long as each round
        keeps making real progress (resolves a fresh finding without the
        validator re-opening a prior one), so a steadily-improving PR converges
        instead of being stranded one address-pass short (#1554).

        #1328: before the FIRST review iteration, a PR with a merge conflict is
        rebased / handed to the implementation agent to resolve. A conflicted PR
        can never merge, so reviewing it would be wasted spend; if the conflict
        cannot be resolved the loop returns early with a non-GO verdict so the PR
        is treated as needs-action instead of being reviewed.
        """
        last_verdict: str | None = None
        last_grade: str | None = None
        prior_review: str | None = None
        iterations_run = 0
        prior_addressed_threads: list[dict[str, Any]] = []
        # #1329: stable keys of findings the validator re-opened, carried across
        # rounds so a re-open recurring on an already-documented design decision
        # is accepted once and never re-added (converging the loop toward GO).
        prior_reopened_keys: set[str] = set()

        # #1328: resolve any merge conflict BEFORE reviewing. Never review a
        # conflicted PR — it cannot merge regardless of the verdict.
        if pr_number is not None and not self._resolve_conflict_before_review(
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            branch_name=branch_name,
            session_id=session_id,
            slot_id=slot_id,
            thread_id=thread_id,
            state=state,
        ):
            self._finalize_review_outcome(
                issue_number=issue_number,
                pr_number=pr_number,
                last_verdict="NOGO",
                iterations_run=0,
            )
            return 0, "NOGO", None

        # #1554: progress-aware review budget. The loop starts with the base
        # ``MAX_REVIEW_ITERATIONS`` budget; whenever the PREVIOUS round made
        # genuine PROGRESS (its address step resolved a fresh finding without the
        # validator re-opening a prior one) and the loop is about to exhaust the
        # budget, grant one more iteration — up to ``MAX_REVIEW_ITERATIONS_HARD_CAP``
        # — so a steadily-improving PR with more real findings than the base
        # budget converges to a clean GO instead of being stranded one
        # address-pass short. A stuck or oscillating reviewer makes no progress
        # and is never extended, so it still terminates at the base budget and is
        # tagged ``state:skip``. The extension happens at the TOP of the loop
        # (before the address-step gate that breaks on the final budgeted
        # iteration), keyed off the prior round's progress.
        budget = MAX_REVIEW_ITERATIONS
        prior_round_made_progress = False
        iteration = 0
        while iteration < budget:
            # Extend the budget when the prior round made real progress and this
            # would otherwise be the last budgeted iteration — so the fix the
            # prior round landed gets a re-review (and its findings addressed).
            if (
                prior_round_made_progress
                and iteration == budget - 1
                and budget < MAX_REVIEW_ITERATIONS_HARD_CAP
            ):
                budget += 1
                self.impl._log(
                    "info",
                    f"{issue_ref(issue_number)} R{iteration}: prior round resolved "
                    f"finding(s); extending review budget to "
                    f"{budget}/{MAX_REVIEW_ITERATIONS_HARD_CAP} iteration(s)",
                    thread_id,
                )
            prior_round_made_progress = False
            (
                last_verdict,
                last_grade,
                review_text,
                posted_thread_ids,
                go_blocked_by_automation,
                reopened,
                should_break,
                prior_reopened_keys,
                validator_clean,
            ) = self._process_review_iteration(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                branch_name=branch_name,
                worktree_path=worktree_path,
                session_id=session_id,
                slot_id=slot_id,
                thread_id=thread_id,
                pr_number=pr_number,
                iteration=iteration,
                prior_review=prior_review,
                prior_addressed_threads=prior_addressed_threads,
                prior_reopened_keys=prior_reopened_keys,
                advise_findings=advise_findings,
            )
            iterations_run = iteration + 1
            if should_break:
                break
            # An unclean validator pass (#1329) — including a PR-level-only
            # re-open that posts no inline thread id — means a prior comment is
            # still unaddressed and must run the address step. This is distinct
            # from a zero-thread REVIEWER non-GO (validator clean), which must
            # simply re-review (the loop's zero-thread convergence contract).
            if (
                pr_number is not None
                and not posted_thread_ids
                and not reopened
                and not go_blocked_by_automation
                and validator_clean
            ):
                prior_review = review_text
                iteration += 1
                continue
            prior_review = review_text
            address_result = self._run_address_step_if_needed(
                issue_number=issue_number,
                pr_number=pr_number,
                branch_name=branch_name,
                worktree_path=worktree_path,
                iteration=iteration,
                budget=budget,
                session_id=session_id,
                slot_id=slot_id,
                thread_id=thread_id,
                issue_title=issue_title,
                issue_body=issue_body,
            )
            if address_result is None:
                break
            prior_addressed_threads, addressed = address_result
            if pr_number is None:
                iteration += 1
                continue
            if not addressed:
                break
            # The address step resolved a fresh finding (``addressed``) without
            # the validator re-opening a prior one (``validator_clean``): this
            # round made real progress. Record it so the NEXT iteration can
            # extend the budget if it would otherwise be the last — letting a PR
            # that fixes one real bug per round converge instead of being
            # stranded one pass short of GO and wrongly skipped (#1554).
            prior_round_made_progress = addressed and validator_clean
            iteration += 1

        self._finalize_review_outcome(
            issue_number=issue_number,
            pr_number=pr_number,
            last_verdict=last_verdict,
            iterations_run=iterations_run,
        )
        return iterations_run, last_verdict, last_grade

    def _process_review_iteration(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        branch_name: str,
        worktree_path: Path,
        session_id: str | None,
        slot_id: int | None,
        thread_id: int | None,
        pr_number: int | None,
        iteration: int,
        prior_review: str | None,
        prior_addressed_threads: list[dict[str, Any]],
        prior_reopened_keys: set[str],
        advise_findings: str,
    ) -> tuple[str | None, str | None, str, list[str], bool, list[str], bool, set[str], bool]:
        """Run one review+verdict iteration.

        Args:
            issue_number: GitHub issue number.
            issue_title: Issue title for context.
            issue_body: Issue body for context.
            branch_name: Git branch being reviewed.
            worktree_path: Path to the checked-out worktree.
            session_id: Optional implementer session ID.
            slot_id: Worker slot for status tracking.
            thread_id: Current thread id for logging.
            pr_number: PR number, or None for diff-only review.
            iteration: Zero-based iteration index.
            prior_review: Previous review text for context.
            prior_addressed_threads: Threads addressed in previous iteration.
            prior_reopened_keys: Stable keys re-opened in earlier rounds, threaded
                forward so a documented by-design recurrence is accepted (#1329).
            advise_findings: Prior learnings from the advise step.

        Returns:
            Tuple of (last_verdict, last_grade, review_text, posted_thread_ids,
                      go_blocked_by_automation, reopened, should_break,
                      reopened_keys, validator_clean).

        """
        (
            reopened,
            review_text,
            posted_thread_ids,
            verdict,
            validator_clean,
            reopened_keys,
        ) = self._validate_and_review(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            branch_name=branch_name,
            worktree_path=worktree_path,
            session_id=session_id,
            slot_id=slot_id,
            thread_id=thread_id,
            pr_number=pr_number,
            iteration=iteration,
            prior_review=prior_review,
            prior_addressed_threads=prior_addressed_threads,
            prior_reopened_keys=prior_reopened_keys,
            advise_findings=advise_findings,
        )
        last_verdict = verdict.verdict
        last_grade = verdict.grade

        go_blocked_by_automation = False
        should_break = False
        # A validator re-open (inline thread id present) OR an unclean pass with
        # only PR-level findings (#1329, no thread id) both mean prior comments
        # are unaddressed → force NOGO so the address step runs.
        if reopened or not validator_clean:
            last_verdict = "NOGO"
        elif verdict.is_go:
            last_verdict, go_blocked_by_automation, should_break = self._evaluate_go_verdict(
                issue_number=issue_number,
                pr_number=pr_number,
                thread_id=thread_id,
            )

        return (
            last_verdict,
            last_grade,
            review_text,
            posted_thread_ids,
            go_blocked_by_automation,
            reopened,
            should_break,
            reopened_keys,
            validator_clean,
        )

    def _run_address_step_if_needed(
        self,
        *,
        issue_number: int,
        pr_number: int | None,
        branch_name: str,
        worktree_path: Path,
        iteration: int,
        budget: int,
        session_id: str | None,
        slot_id: int | None,
        thread_id: int | None,
        issue_title: str,
        issue_body: str,
    ) -> tuple[list[dict[str, Any]], bool] | None:
        """Run address iteration unless this is the final budgeted iteration.

        Args:
            issue_number: GitHub issue number.
            pr_number: PR number, or None for diff-only mode.
            branch_name: Git branch name.
            worktree_path: Path to the checked-out worktree.
            iteration: Current zero-based iteration index.
            budget: Current iteration budget. Addressing is skipped on the final
                budgeted iteration (``iteration == budget - 1``); a productive
                round extends ``budget`` in the caller BEFORE the next pass, so a
                steadily-improving PR still gets its findings addressed (#1554).
            session_id: Optional implementer session ID.
            slot_id: Worker slot for status tracking.
            thread_id: Current thread id for logging.
            issue_title: Issue title for context.
            issue_body: Issue body for context.

        Returns:
            None if this is the final budgeted iteration (caller should break).
            (prior_addressed_threads, addressed) tuple otherwise.

        """
        if iteration == budget - 1:
            return None
        prior_addressed_threads, addressed = self._run_address_iteration(
            issue_number=issue_number,
            pr_number=pr_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            iteration=iteration,
            session_id=session_id,
            slot_id=slot_id,
            thread_id=thread_id,
            issue_title=issue_title,
            issue_body=issue_body,
        )
        return prior_addressed_threads, addressed

    def _run_address_iteration(
        self,
        *,
        issue_number: int,
        pr_number: int | None,
        branch_name: str,
        worktree_path: Path,
        iteration: int,
        session_id: str | None,
        slot_id: int | None,
        thread_id: int | None,
        issue_title: str,
        issue_body: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run the address step for one iteration.

        Resumes Session 2 to fix the posted threads, commit, push, and resolve
        the threads it actually addressed. Skipped only when there is no PR (no
        inline threads to address). ``session_id`` is informational — the
        address step resumes ``AGENT_IMPLEMENTER`` by its deterministic
        per-(repo,issue,agent) id (or starts a fresh implementer session when no
        transcript exists), so the existing-PR path (which has no initial
        session_id) can still fix review threads rather than dead-ending here.

        Returns ``(prior_addressed_threads, addressed)``: the snapshot of
        unresolved threads the address step was asked to fix (so the NEXT
        iteration's validator can check they were truly addressed) and whether
        anything was addressed. When there is no PR, returns ``([], True)`` —
        the caller ``continue``s to the next iteration regardless.
        """
        impl = self.impl
        if pr_number is None:
            return [], True
        # Snapshot the unresolved threads the address step is about to fix so
        # the NEXT iteration's validator can check they were truly addressed.
        prior_addressed_threads = gh_pr_list_unresolved_threads(pr_number, dry_run=False)
        if slot_id is not None:
            self.status_tracker.update_slot(
                slot_id, f"{pr_ref(pr_number)}: addressing review [R{iteration}]"
            )
        addressed = impl._run_address_review_step(
            issue_number=issue_number,
            pr_number=pr_number,
            branch_name=branch_name,
            worktree_path=worktree_path,
            iteration=iteration,
            # When the loop started without an initial implementer session
            # (existing-PR review path), the address session may run fresh
            # with no transcript to resume — give it the task + task-review
            # + diff so it can read the work and continue. The fresh-impl
            # path already carries this in its long-lived transcript.
            include_bootstrap_context=session_id is None,
            issue_title=issue_title,
            issue_body=issue_body,
        )
        if not addressed:
            impl._log(
                "info",
                f"{issue_ref(issue_number)}: address step resolved no threads on "
                f"iteration {iteration}; stopping review loop",
                thread_id,
            )
        return prior_addressed_threads, addressed

    def _finalize_review_outcome(
        self,
        *,
        issue_number: int,
        pr_number: int | None,
        last_verdict: str | None,
        iterations_run: int,
    ) -> None:
        """Apply the post-loop labeling/skip policy for the final verdict.

        #1083 Bug 2 / #1085 C3: the loop must reach an explicit GO to be
        considered converged. Apply ``state:skip`` only on TRUE iteration
        exhaustion — ran all MAX_REVIEW_ITERATIONS without a GO — NOT on a single
        non-GO (including AMBIGUOUS) outcome. Per the pr-review-loop skill
        (verified-ci), a zero-thread AMBIGUOUS/NO-GO pass no longer ends the loop
        early (it re-reviews), so a transient garbage review gets
        MAX_REVIEW_ITERATIONS chances instead of stranding a fixable PR after R0.
        ``last_verdict`` is None only when there was no PR to review (dry-run /
        no-PR path).

        ERROR (reviewer-infrastructure failure — API 400, timeout, crash) is NOT
        a real verdict: the reviewer never actually judged the code, so an
        exhausted run that ended in ERROR must NOT be skipped (#911 / PR #1069).
        Neither ERROR nor HUMAN_BLOCKED (GO blocked by an open human thread) is a
        converged failure, so neither applies state:skip — both leave the PR
        unlabeled for re-review / human action.
        """
        # A2-003: Surface AMBIGUOUS verdict distinctly so operators can triage
        # without inspecting raw log files. #1554: the loop may run beyond the
        # base budget when it keeps making progress, so anchor on ">=" rather
        # than "==" — an extended run that still ended non-GO must warn too.
        if last_verdict == "AMBIGUOUS" or (
            iterations_run >= MAX_REVIEW_ITERATIONS and last_verdict not in (None, "GO")
        ):
            logger.warning(
                "#%d: review loop ended without clear GO — "
                "final verdict=%r after %d iteration(s); "
                "PR created but manual review is recommended",
                issue_number,
                last_verdict,
                iterations_run,
            )

        exhausted = iterations_run >= MAX_REVIEW_ITERATIONS and last_verdict not in (
            "GO",
            "ERROR",
            "HUMAN_BLOCKED",
        )
        if (
            pr_number is not None
            and last_verdict is not None
            and exhausted
            and not self.options.dry_run
        ):
            logger.info(
                "#%d: review loop ended without GO (verdict=%r, iterations=%d) — applying %s",
                issue_number,
                last_verdict,
                iterations_run,
                STATE_SKIP,
            )
            with contextlib.suppress(Exception):
                gh_issue_add_labels(issue_number, [STATE_SKIP])
        elif last_verdict == "ERROR":
            logger.warning(
                "#%d: review loop ended in reviewer-infrastructure ERROR after %d "
                "iteration(s) — leaving PR unlabeled for re-review (no %s)",
                issue_number,
                iterations_run,
                STATE_SKIP,
            )
        elif last_verdict == "HUMAN_BLOCKED":
            logger.warning(
                "#%d: review reached GO but is blocked by unresolved human review "
                "thread(s) — leaving PR unlabeled for human resolution (no %s)",
                issue_number,
                STATE_SKIP,
            )

    def _count_unresolved_threads_blocking_go(
        self,
        *,
        issue_number: int,
        pr_number: int,
        thread_id: int | None,
    ) -> tuple[int, int]:
        """Count unresolved review threads that block a GO, by ownership.

        A GO verdict may NOT stand while ANY review thread is unresolved — not
        even an automation-owned one. The earlier implementation bulk-resolved
        automation threads here so a GO could converge immediately; that was
        wrong (#1152). A reviewer can emit ``Verdict: GO`` in the SAME pass that
        posts new inline findings, and force-resolving those "automation-owned"
        threads accepted the GO without the implementer ever addressing them or
        a subsequent review verifying the fix. The GO label must only be earned
        once every comment has been genuinely addressed AND a clean re-review
        confirms zero unresolved threads.

        This method therefore RESOLVES NOTHING. It returns
        ``(automation_unresolved, human_unresolved)``:

        * ``human_unresolved > 0`` — automation cannot fix human threads, so the
          caller breaks with a distinct ``HUMAN_BLOCKED`` terminal state.
        * ``automation_unresolved > 0`` — the caller downgrades GO to NOGO so the
          address step fixes (and resolves only what it actually addressed) and a
          re-review re-evaluates. Convergence requires a GO pass that leaves zero
          unresolved threads.

        Returns ``(0, 0)`` when the thread list can't be fetched (fail-open:
        don't strand a GO on a transient API blip — a genuinely unresolved
        thread re-surfaces on the next loop's existing-PR re-review).
        """
        impl = self.impl
        try:
            threads = gh_pr_list_unresolved_threads(pr_number, dry_run=False)
        except Exception as exc:
            impl._log(
                "warning",
                f"{issue_ref(issue_number)}: could not list unresolved threads "
                f"after GO for {pr_ref(pr_number)}: {exc}",
                thread_id,
            )
            return (0, 0)
        if not threads:
            return (0, 0)

        current_login = gh_current_login()
        automation_unresolved = 0
        human_unresolved = 0
        for thread in threads:
            if _is_automation_owned_thread(thread, current_login):
                automation_unresolved += 1
            else:
                human_unresolved += 1
        if automation_unresolved or human_unresolved:
            impl._log(
                "info",
                f"{issue_number}: GO pass left {automation_unresolved} automation + "
                f"{human_unresolved} human unresolved thread(s) on {pr_ref(pr_number)} "
                "— GO cannot stand until all are addressed and a clean re-review confirms",
                thread_id,
            )
        return (automation_unresolved, human_unresolved)

    def _run_impl_review_step(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        branch_name: str,
        worktree_path: Path,
        pr_number: int | None,
        iteration: int,
        prior_review: str | None,
        advise_findings: str = "",
    ) -> tuple[str, list[str]]:
        """Run one in-loop review and return ``(review_text, posted_thread_ids)``.

        With a ``pr_number`` this folds in ``pr_reviewer``'s core
        (:func:`review_pr_inline`): a fresh per-iteration reviewer session posts
        INLINE PR review threads and returns its summary text (carrying the
        ``Grade:`` / ``Verdict:`` line). The reviewer context includes the TASK
        (issue title + body), the PLAN and PLAN_REVIEW comments, and the impl
        diff (#28).

        Without a ``pr_number`` (dry-run / no PR) it falls back to the diff-only
        reviewer (:meth:`_run_impl_review`) which posts nothing.
        """
        impl = self.impl
        if pr_number is None:
            diff_text = impl._collect_diff(worktree_path, branch_name)
            files_changed = impl._collect_changed_files(worktree_path, branch_name)
            review_text = impl._run_impl_review(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                diff_text=diff_text,
                files_changed=files_changed,
                iteration=iteration,
                prior_review=prior_review,
            )
            return review_text, []

        if self.options.dry_run:
            logger.info("[DRY RUN] Would run in-loop PR review for #%s", pr_number)
            return "Grade: A\nVerdict: GO\n", []

        diff_text = impl._collect_diff(worktree_path, branch_name)
        plan_text, plan_review_text = self.runner._fetch_plan_and_review(issue_number)
        context = gather_impl_review_context(
            pr_number=pr_number,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
            plan_review_text=plan_review_text,
            diff_text=diff_text,
            advise_findings=advise_findings,
            include_nitpicks=self.options.include_nitpicks,
        )
        try:
            return review_pr_inline(
                pr_number=pr_number,
                issue_number=issue_number,
                worktree_path=worktree_path,
                context=context,
                agent=self.options.agent,
                iteration=iteration,
                state_dir=self.state_dir,
                dry_run=False,
            )
        except Exception as e:
            # A 429 quota cap / 529 overload must pause the loop, not spin fresh
            # reviewer sessions against an exhausted quota (issue #1528). After
            # any wait, still record ERROR so a transient infra failure re-reviews
            # next loop and is never mistaken for a NOGO.
            _handle_reviewer_quota_or_overload(e, issue_number=issue_number, iteration=iteration)
            logger.error(
                "#%s R%s: in-loop PR review failed: %s; recording ERROR (re-review next "
                "loop, no skip/label) so an infra failure isn't mistaken for a NOGO",
                issue_number,
                iteration,
                e,
            )
            return (
                f"In-loop reviewer invocation failed at iteration {iteration}: {e}\n\n"
                f"{INFRA_ERROR_REVIEW_TEXT}"
            ), []

    def _run_address_review_step(
        self,
        *,
        issue_number: int,
        pr_number: int,
        branch_name: str,
        worktree_path: Path,
        iteration: int,
        include_bootstrap_context: bool = False,
        issue_title: str = "",
        issue_body: str = "",
    ) -> bool:
        """Address the posted PR threads in-loop, resuming Session 2.

        Folds in ``address_review``'s core: lists the unresolved threads on the
        PR, runs the fix session (resuming ``AGENT_IMPLEMENTER`` via
        :func:`run_address_fix_session`, which fans out one sub-agent per COMMENT
        at the model tier matching its classified difficulty, #1083), then
        commits + pushes the fixes. Thread RESOLUTION is no longer done here — it
        moved to the evidence-based validator (#1083); this step only counts as
        progress when a real commit was produced (#1085).

        Returns:
            ``True`` if at least one thread was addressed (so the loop should
            re-review); ``False`` when nothing was addressable (the loop stops).

        """
        threads = gh_pr_list_unresolved_threads(pr_number, dry_run=False)
        if not threads:
            logger.info(
                "#%s R%s: no unresolved threads to address on PR %s",
                issue_number,
                iteration,
                pr_ref(pr_number),
            )
            return False

        # On the existing-PR path the address session may run fresh (no
        # implementer transcript to resume), so it has no memory of the task or
        # the implementation. Bootstrap it with the task, the plan-review, and
        # the current diff. The fresh-impl path already carries this in its
        # long-lived transcript, so the blocks stay empty there (no extra cost).
        task_block = ""
        task_review_block = ""
        diff_text = ""
        if include_bootstrap_context:
            task_block = f"#{issue_number} {issue_title}\n\n{issue_body}".strip()
            _, task_review_block = self.runner._fetch_plan_and_review(issue_number)
            diff_text = self.impl._collect_diff(worktree_path, branch_name)

        log_file = self.state_dir / f"address-review-{issue_number}-r{iteration}.log"
        fix_result = run_address_fix_session(
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            threads=threads,
            agent=self.options.agent,
            repo_root=self.repo_root,
            parse_fn=lambda text: self.runner._parse_address_result(text, issue_number, iteration),
            log_file=log_file,
            dry_run=False,
            task_block=task_block,
            task_review_block=task_review_block,
            diff_text=diff_text,
        )
        addressed: list[str] = fix_result.get("addressed", [])

        # #1083 Bug 1: gate "addressed" on a REAL commit. The fix session's
        # self-reported ``addressed`` list is not trusted on its own — a session
        # can claim it resolved a thread while leaving the worktree clean (e.g.
        # replying "documented as a limitation" with no code change). Only when
        # _commit_if_changes actually produced a commit do we push and report
        # progress, so a no-op fix can never advance the loop.
        committed = self.runner._commit_if_changes(issue_number, worktree_path)
        if not (addressed and committed):
            logger.info(
                "#%s R%s: address step produced no committed change "
                "(addressed=%s, committed=%s) — not counted as progress",
                issue_number,
                iteration,
                bool(addressed),
                committed,
            )
            return False
        self.runner._push_branch(branch_name, worktree_path)

        # #1083 Bug 1/Move-to-reviewer: resolution is NO LONGER done here. The
        # validator (review_validator.validate_prior_comments_addressed) resolves
        # each prior thread only after a fresh read-only sub-agent confirms the
        # current diff actually addresses it. This closes the "resolved without
        # implementing" hole — a self-reported fix with no diff change stays open.
        return True

    def _parse_address_result(self, text: str, issue_number: int, iteration: int) -> dict[str, Any]:
        """Parse the address-session JSON block, tracing parse failures.

        Wraps :func:`address_review._parse_addressed_block` but writes a
        diagnostic trace file when the block is missing/malformed, so an empty
        ``addressed`` list is distinguishable from "the model reviewed and
        chose no fixes" (mirrors the standalone phase's behavior).
        """
        from .address_review import _parse_addressed_block

        matches = _parse_addressed_block(text)
        if not matches.get("addressed") and "```json" not in text:
            with contextlib.suppress(Exception):
                trace_path = self.state_dir / f"address-{issue_number}-r{iteration}.parse-error.log"
                trace_path.write_text(
                    f"reason: no fenced ```json block found in response\n\n"
                    f"=== full response ===\n{text}"
                )
        return matches

    def _fetch_plan_and_review(self, issue_number: int) -> tuple[str, str]:
        """Return ``(plan_text, plan_review_text)`` for the reviewer context.

        The PLAN comment is identified the same way
        :func:`planner_state._comments_contain_plan` does — prefix-anchored on
        :data:`PLAN_COMMENT_MARKER`, skipping comments whose body starts with
        :data:`review_state.PLAN_REVIEW_PREFIX`. The PLAN_REVIEW comment is
        the one whose body starts with ``review_state.PLAN_REVIEW_PREFIX``.
        Best-effort: any fetch failure yields empty strings. ``review_state``
        is imported top-level so tests patch its internals at
        ``hephaestus.automation._review_phase.review_state``.
        """
        plan_text = ""
        plan_review_text = ""
        try:
            comments = review_state._fetch_issue_comments_graphql(issue_number)
            for comment in comments:
                body = comment.get("body", "")
                stripped = body.lstrip()
                if stripped.startswith(review_state.PLAN_REVIEW_PREFIX):
                    plan_review_text = body
                elif stripped.startswith(PLAN_COMMENT_MARKER):
                    plan_text = body
        except Exception as e:
            logger.warning("#%s: failed to fetch PLAN/PLAN_REVIEW context: %s", issue_number, e)
        return plan_text, plan_review_text

    def _commit_if_changes(self, issue_number: int, worktree_path: Path) -> bool:
        """Commit any pending changes from the in-loop address step.

        Silently skips when the worktree is clean. Mirrors
        ``AddressReviewer._commit_if_changes``.

        Returns:
            ``True`` iff a commit was actually created. ``False`` when the
            worktree was clean (nothing to commit) or the commit failed. The
            caller (#1083) uses this to gate progress/resolution on a real
            change rather than the model's self-report.

        """
        result = run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
        )
        if not result.stdout.strip():
            logger.info("No changes to commit for issue #%s", issue_number)
            return False
        try:
            commit_changes(issue_number, worktree_path, self.options.agent)
            logger.info("Committed in-loop address changes for issue #%s", issue_number)
            return True
        except RuntimeError as e:
            logger.warning("Commit skipped for issue #%s: %s", issue_number, e)
            return False

    def _push_branch(self, branch_name: str, worktree_path: Path) -> None:
        """Push *branch_name* to origin after an in-loop address step.

        Mirrors ``AddressReviewer._push_branch``.

        Raises:
            RuntimeError: If the push fails.

        """
        try:
            run(["git", "push", "origin", branch_name], cwd=worktree_path)
            logger.info("Pushed branch %s to origin", branch_name)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to push branch {branch_name}: {e}") from e

    def _resume_impl_with_feedback(
        self,
        *,
        session_id: str,
        worktree_path: Path,
        issue_number: int,
        review_text: str,
        prev_iteration: int,
        verdict: str,
        state: ImplementationState | None = None,
    ) -> bool:
        """Resume the impl session and feed reviewer feedback as the next prompt."""
        impl = self.impl
        prompt = get_impl_resume_feedback_prompt(
            issue_number=issue_number,
            prev_iteration=prev_iteration,
            verdict=verdict,
            review_text=review_text,
        )
        if is_codex(self.options.agent):
            try:
                result = resume_codex_session(
                    session_id,
                    prompt,
                    cwd=worktree_path,
                    timeout=implementer_claude_timeout(),
                )
                log_file = (
                    self.state_dir / f"codex-feedback-{issue_number}-r{prev_iteration + 1}.log"
                )
                log_file.write_text(result.stdout or "")
                return True
            except subprocess.CalledProcessError as e:
                logger.error(
                    "#%d: Codex failed to address R%d feedback (exit=%d): %s",
                    issue_number,
                    prev_iteration + 1,
                    e.returncode,
                    (e.stderr or e.stdout or "")[:500],
                )
                return False
            except subprocess.TimeoutExpired:
                logger.error(
                    "#%d: Codex timed out addressing R%d feedback",
                    issue_number,
                    prev_iteration + 1,
                )
                return False

        # Route through the centralized helper so create/resume semantics and
        # the SESSION_EXPIRED phrase list stay in one place. The deterministic
        # UUID matches what the initial impl session was created with, so the
        # passed-in ``session_id`` (legacy ``state.session_id``) is ignored on
        # the Claude path — it's still consumed by the codex branch above.
        # ``recreate_on_resume_failure=False`` propagates the underlying error
        # so we can preserve the "stop iterating on expiry" contract.
        repo_slug = get_repo_slug(self.repo_root)
        try:
            invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_IMPLEMENTER,
                prompt=prompt,
                model=implementer_model(),
                cwd=worktree_path,
                timeout=implementer_claude_timeout(),
                permission_mode="dontAsk",
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                recreate_on_resume_failure=False,
            )
            return True
        except subprocess.CalledProcessError as e:
            combined = ((e.stderr or "") + (e.stdout or "")).lower()
            if any(phrase in combined for phrase in SESSION_EXPIRED_PHRASES):
                # Session pruned — partial work may still be committable;
                # don't treat this as an unrecoverable failure.
                error_tag = f"session_expired:{session_id}"
                logger.warning(
                    "#%d: impl session %r expired before R%d; "
                    "stopping review loop (partial work preserved)",
                    issue_number,
                    session_id,
                    prev_iteration + 1,
                )
                if state is not None:
                    with self.state_lock:
                        state.error = error_tag
                    impl._save_state(state)
            else:
                logger.error(
                    "#%d: failed to resume impl session for R%d (exit=%d): %s",
                    issue_number,
                    prev_iteration + 1,
                    e.returncode,
                    (e.stderr or e.stdout or "")[:500],
                )
            return False
        except Exception as e:  # broad: resume is best-effort, never crash the loop
            logger.error(
                "#%d: unexpected error resuming impl session for R%d: %s",
                issue_number,
                prev_iteration + 1,
                e,
            )
            return False

    def _run_impl_review(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        diff_text: str,
        files_changed: str,
        iteration: int,
        prior_review: str | None,
    ) -> str:
        """Run a fresh-session reviewer against the current impl diff."""
        prompt = get_impl_loop_review_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            diff_text=diff_text,
            files_changed=files_changed,
            iteration=iteration,
            prior_review=prior_review,
        )
        try:
            if is_codex(self.options.agent):
                result = run_codex_text(
                    prompt,
                    cwd=self.repo_root,
                    timeout=600,
                    sandbox="read-only",
                )
                output = (result.stdout or "").strip()
                if not output:
                    raise RuntimeError("reviewer returned empty output")
                return output

            env = os.environ.copy()
            env["CLAUDECODE"] = ""
            result = subprocess.run(
                [
                    "claude",
                    "--print",
                    "--model",
                    reviewer_model(),
                    "--output-format",
                    "text",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                check=True,
                timeout=600,
                env=env,
            )
            output = (result.stdout or "").strip()
            if not output:
                raise RuntimeError("reviewer returned empty output")
            return output
        except Exception as e:
            # Mirror the in-loop path: pause on a Claude 429 cap / 529 overload
            # before recording ERROR so the diff-only fallback does not burn
            # sessions against an exhausted quota either (issue #1528).
            _handle_reviewer_quota_or_overload(e, issue_number=issue_number, iteration=iteration)
            logger.error(
                "#%s R%s: impl reviewer call failed: %s; recording ERROR (re-review next "
                "loop, no skip/label) so an infra failure isn't mistaken for a NOGO",
                issue_number,
                iteration,
                e,
            )
            return (
                f"Reviewer invocation failed at iteration {iteration}: {e}\n\n"
                f"{INFRA_ERROR_REVIEW_TEXT}"
            )

    def _collect_diff(self, worktree_path: Path, branch_name: str) -> str:
        """Return the cumulative diff of *branch_name* against ``origin/main``."""
        try:
            result = run(
                ["git", "diff", "origin/main...HEAD"],
                cwd=worktree_path,
                capture_output=True,
                check=False,
                timeout=60,
            )
            diff = result.stdout or ""
            if not diff.strip():
                fb = run(
                    ["git", "diff", "HEAD~1..HEAD"],
                    cwd=worktree_path,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                diff = fb.stdout or ""
        except Exception as e:
            logger.warning("diff collection failed for %s: %s", branch_name, e)
            return ""

        # #1329: a 200k cap routinely truncated the diff the validator inspects,
        # so it could not SEE the fix/documentation at a later hunk and re-opened
        # comments it judged "unaddressed" — an unwinnable loop. Raise the cap an
        # order of magnitude so the whole diff is normally fed; only a genuinely
        # huge diff is still bounded (to protect the model context window).
        max_chars = 2_000_000
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n\n[... diff truncated at {max_chars} chars ...]\n"
        return diff

    def _collect_changed_files(self, worktree_path: Path, branch_name: str) -> str:
        """Return a newline-separated list of changed files vs ``origin/main``."""
        try:
            result = run(
                ["git", "diff", "--name-only", "origin/main...HEAD"],
                cwd=worktree_path,
                capture_output=True,
                check=False,
                timeout=30,
            )
            files = (result.stdout or "").strip()
            if files:
                return files
            fb = run(
                ["git", "diff", "--name-only", "HEAD~1..HEAD"],
                cwd=worktree_path,
                capture_output=True,
                check=False,
                timeout=30,
            )
            return (fb.stdout or "").strip()
        except Exception as e:
            logger.warning("changed-files collection failed for %s: %s", branch_name, e)
            return ""

    def _save_review_log(self, issue_number: int, iteration: int, review_text: str) -> None:
        """Persist a per-iteration review log for later inspection."""
        try:
            log_file = self.state_dir / f"review-{issue_number}-r{iteration}.log"
            log_file.write_text(review_text)
        except Exception as e:
            logger.warning("#%s: failed to save review log r%s: %s", issue_number, iteration, e)

    def _save_review_iteration_state(
        self, issue_number: int, iterations_run: int, prior_review: str
    ) -> None:
        """Persist review loop progress for ``--resume`` continuity (A2-005)."""
        try:
            iter_file = self.state_dir / f"review-iter-{issue_number}.json"
            iter_file.write_text(json.dumps({"iterations_run": iterations_run}))
        except Exception as e:
            logger.warning("#%d: failed to persist review iteration count: %s", issue_number, e)
        try:
            prior_file = self.state_dir / f"review-prior-{issue_number}.txt"
            prior_file.write_text(prior_review)
        except Exception as e:
            logger.warning("#%d: failed to persist prior review text: %s", issue_number, e)

    def _load_review_iteration_state(self, issue_number: int) -> tuple[int, str | None]:
        """Load persisted review iteration progress for ``--resume`` (A2-005)."""
        iterations_run = 0
        prior_review: str | None = None
        try:
            iter_file = self.state_dir / f"review-iter-{issue_number}.json"
            if iter_file.exists():
                data = json.loads(iter_file.read_text())
                iterations_run = int(data.get("iterations_run", 0))
        except Exception as e:
            logger.warning(
                "#%d: failed to load persisted review iteration count: %s", issue_number, e
            )
        try:
            prior_file = self.state_dir / f"review-prior-{issue_number}.txt"
            if prior_file.exists():
                prior_review = prior_file.read_text()
        except Exception as e:
            logger.warning("#%d: failed to load persisted prior review text: %s", issue_number, e)
        return iterations_run, prior_review
