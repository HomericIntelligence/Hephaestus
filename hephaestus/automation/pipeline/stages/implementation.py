"""Implementation stage: gate plan GO, cut worktree, implement, test, push, PR.

Re-houses the implementation control flow from the legacy per-issue phase
runner (dispatch, plan-ready gate, existing-PR review, and the
``ensure_pr_auto_merge_deferred`` call) and
``_pr_create_phase.PRCreatePhase._finalize_pr`` (:36) as a pipeline stage
(docs/AUTOMATION_LOOP_ARCHITECTURE.md section "4. implementation" is the
binding contract):

- States: ENTER -> GATE -> WORKTREE_WAIT -> DIRTY_DECISION_WAIT ->
  ADVISE_WAIT -> IMPLEMENT_WAIT -> TEST_WAIT -> TESTFIX_WAIT ->
  COMMIT_PUSH_WAIT -> PR_CREATE. The existing-PR fast path short-circuits
  WORKTREE_WAIT -> DIRTY_DECISION_WAIT -> ADOPTED (ADVANCE to pr_review).
- Budgets: ``implement`` = 2 (bounds implement attempts INCLUDING
  agent_error retries — the doc's "agent_error -> RETRY (consumes the
  implement budget)"), ``test_fix`` = 1 (one fix attempt on red pre-PR
  tests). Both read from ROUTES via ``ctx.budget``, never hardcoded here.
- GATE [M]: existing-PR fast path first (``_review_existing_pr``
  semantics): a PR already carrying ``state:implementation-go`` fails back
  ``already_implementation_go_pr`` (routes to ci) with ``item.pr`` /
  ``item.branch`` set for the ci stage; a PR without it adopts the PR's
  REAL head branch, re-ensures the auto-merge deferral [durable], cuts a
  worktree on the ADOPTED branch (``refresh_base=False`` +
  ``sync_to_remote`` — the anti-clobber reset of
  ``_prepare_worktree_for_existing_pr`` :649, so pushed commits are never
  discarded), runs the dirty-salvage decision if needed, and only then
  ADVANCEs to pr_review (ADOPTED). Otherwise the plan-review verdict gate:
  at-or-past ``state:plan-go`` (or already ``state:implementation-go``)
  proceeds; anything else fails back ``plan_not_go`` (routes to
  plan_review).
- agent_error ping-pong bound: when pr_review fails back ``agent_error``
  (flagged in ``payload["agent_error_failback"]``), the GATE's existing-PR
  adoption CONSUMES the ``implement`` budget — otherwise the
  fail-back -> adopt -> ADVANCE cycle would never move a counter and could
  loop forever. Exhaustion -> FINISH_FAIL(``agent_error_exhausted``): the
  reviewer/address infrastructure failed repeatedly and re-adopting the
  same PR again cannot fix it; a human should look at the PR.
- Transient git failures (worktree creation, commit+push) RETRY without
  burning the implement budget, but are bounded by
  :data:`GIT_ERROR_RETRY_CAP` consecutive failures (mirrors
  pr_review.REVIEW_ERROR_RETRY_CAP); at the cap the item finishes failed
  (``git_error``) instead of retrying a broken remote forever. The counter
  resets on any successful git job.
- Owned labels: none — PR creation is the journal entry (doc section 4).
  The only label this stage ever writes is ``state:skip`` on the legacy
  "no commits vs base" runtime error (re-housed from the legacy phase
  runner's runtime-error handler), non-fatally.
- PR_CREATE [M]: ``ctx.github.create_pr`` (idempotent ensure semantics)
  with a ``prompts/pr_review.py get_pr_description`` body [durable], then
  ``ctx.github.defer_auto_merge`` — the load-bearing legacy order (runner
  :623): auto-merge stays disabled until ``state:implementation-go``.
- Prompt functions (imported, never re-authored):
  ``prompts/implementation.py get_implementation_prompt`` (composed with
  the advise-findings block by :func:`build_implementation_prompt`),
  ``get_dirty_reused_worktree_decision_prompt``,
  ``get_impl_resume_feedback_prompt`` (composed with the failing test
  output by :func:`build_test_fix_prompt`), and
  ``prompts/pr_review.py get_pr_description``.
"""

from __future__ import annotations

import logging

from hephaestus.automation.agent_config import (
    advise_claude_timeout,
    advise_model,
    implementer_claude_timeout,
    implementer_model,
)
from hephaestus.automation.prompts.advise import get_advise_prompt_builder
from hephaestus.automation.prompts.implementation import (
    get_dirty_reused_worktree_decision_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)
from hephaestus.automation.prompts.pr_review import get_pr_description
from hephaestus.automation.session_naming import AGENT_ADVISE, AGENT_IMPLEMENTER
from hephaestus.automation.state_labels import (
    STATE_SKIP,
    is_implementation_go,
    is_plan_go,
)

from .base import (
    GIT_JOB_TIMEOUT_S,
    AgentJob,
    BuildTestJob,
    Continue,
    Disposition,
    GitJob,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _issue_labels,
    _worktree_path,
    agent_provider,
    stage_model,
    write_skip_label,
)

logger = logging.getLogger(__name__)

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
GATE = "GATE"
WORKTREE_WAIT = "WORKTREE_WAIT"
DIRTY_DECISION_WAIT = "DIRTY_DECISION_WAIT"
ADOPTED = "ADOPTED"
ADVISE_WAIT = "ADVISE_WAIT"
IMPLEMENT_WAIT = "IMPLEMENT_WAIT"
TEST_WAIT = "TEST_WAIT"
TESTFIX_WAIT = "TESTFIX_WAIT"
COMMIT_PUSH_WAIT = "COMMIT_PUSH_WAIT"
PR_CREATE = "PR_CREATE"

#: Max CONSECUTIVE transient git failures (worktree creation / commit+push)
#: tolerated before the stage finishes failed (``git_error``) instead of
#: RETRYing forever. Mirrors pr_review.REVIEW_ERROR_RETRY_CAP: transient
#: failures never burn the implement budget, but a persistently broken
#: remote must still terminate. Reset on any successful git job.
GIT_ERROR_RETRY_CAP = 2

#: Timeout for the optional pre-PR unit-test run (mirrors the legacy
#: ``_pr_create_phase`` bound; the budget that matters — ``test_fix`` —
#: lives in ROUTES).
PRE_PR_TEST_TIMEOUT_S = 1800

#: Vetted pre-PR test command (BuildTestJob argv must never carry
#: issue-derived strings).
PRE_PR_TEST_ARGV: tuple[str, ...] = ("pixi", "run", "pytest", "tests/unit", "-q", "--tb=short")


def build_implementation_prompt(
    issue_number: int,
    issue_title: str = "",
    issue_body: str = "",
    branch_name: str = "",
    worktree_path: str = "",
    advise_findings: str = "",
) -> str:
    """Compose the implementation prompt with the advise-findings block.

    Module-level composed builder (NOT a closure): :class:`AgentJob` is
    frozen and prompt builders run in-worker, so the builder must be a
    top-level function receiving everything via ``prompt_kwargs``. The base
    prompt is reused verbatim via :func:`get_implementation_prompt`; the
    findings block mirrors :func:`..planning.build_plan_prompt`.

    Args:
        issue_number: GitHub issue number to implement.
        issue_title: Issue title.
        issue_body: Issue body (fenced as untrusted by the base builder).
        branch_name: Feature branch the worktree is on.
        worktree_path: Worktree the implementer works in.
        advise_findings: Advise-step findings; empty string means no block.

    Returns:
        The full implementer prompt, with the findings block appended when
        ``advise_findings`` is non-empty.

    """
    prompt = get_implementation_prompt(
        issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        branch_name=branch_name,
        worktree_path=worktree_path,
    )
    if not advise_findings:
        return prompt
    block = "\n".join(
        [
            "",
            "---",
            "",
            "## Prior Learnings from Team Knowledge Base",
            "",
            advise_findings,
        ]
    )
    return prompt + block


def build_test_fix_prompt(issue_number: int, prev_iteration: int, test_output: str) -> str:
    """Compose the resume prompt that feeds failing pre-PR test output back.

    Reuses :func:`get_impl_resume_feedback_prompt` verbatim (doc section 4
    step 7: "resume with test-failure feedback"), with the test failure
    framed as the NOGO review text the resume template expects.

    Args:
        issue_number: GitHub issue number being implemented.
        prev_iteration: 0-based index of the failed test round.
        test_output: Captured pytest output tail from the failing run.

    Returns:
        The resume prompt carrying the test-failure feedback block.

    """
    review_text = (
        "The pre-PR unit-test run failed. Fix ONLY what the failures below "
        "require, then the tests will be re-run.\n\n" + test_output
    )
    return get_impl_resume_feedback_prompt(
        issue_number=issue_number,
        prev_iteration=prev_iteration,
        verdict="NOGO",
        review_text=review_text,
    )


class ImplementationStage(Stage):
    """Stage: gate plan GO, worktree, advise, implement, test, commit, PR."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Proceed with no durable writes; all entry checks live in GATE.

        The doc's entry step (verify plan GO at-or-past + existing-PR fast
        path) is the GATE mini-state, an [M] step of this stage — so a
        restart re-runs it idempotently via step(). Nothing is written here.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()), or FINISH_FAIL when the item
            has no issue number.

        """
        if not item.issue:
            logger.warning("implementation: work item has no issue number")
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901
        """Execute the next implementation action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if not item.issue:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")

        if item.state == ENTER:
            return Continue(next_state=GATE)

        if item.state == GATE:
            return self._gate(item, ctx)

        if item.state == WORKTREE_WAIT:
            logger.info("implementation:%d: requesting worktree job", item.issue)
            adopted = bool(item.payload.get("existing_pr"))
            kwargs: dict[str, object] = {
                "issue_number": item.issue,
                "branch_name": item.branch,
                # Fresh branch: cut from a freshly refreshed trunk (doc step
                # 2: worktree_manager.create_worktree(refresh_base=True)).
                # ADOPTED branch: never reset to trunk — sync to the PR's
                # remote head instead (the anti-clobber reset of
                # _prepare_worktree_for_existing_pr :649/:693, so re-running
                # never discards pushed commits). Values coordinator-vetted.
                "refresh_base": not adopted,
            }
            if adopted:
                kwargs["sync_to_remote"] = True
            worktree_job = GitJob(
                repo=item.repo,
                op="create_worktree",
                timeout_s=GIT_JOB_TIMEOUT_S,
                kwargs=kwargs,
                descr="create_worktree",
            )
            return JobRequest(worktree_job, on_done_state=DIRTY_DECISION_WAIT)

        if item.state == DIRTY_DECISION_WAIT:
            if item.payload.pop("git_error", None):
                # Worktree creation failed: transient infrastructure, not an
                # implement outcome — RETRY without burning the implement
                # budget, bounded by GIT_ERROR_RETRY_CAP (M5).
                return self._git_retry(item, "worktree creation failed")
            # Adopted-PR path: after the (clean or salvaged) worktree is
            # ready, skip the implement leg — the PR's code already exists;
            # pr_review's address leg drives it from here.
            adopted_next = ADOPTED if item.payload.get("existing_pr") else ADVISE_WAIT
            if not item.payload.get("worktree_dirty"):
                return Continue(next_state=adopted_next)
            logger.info("implementation:%d: requesting dirty-worktree decision", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "implementer", implementer_model),
                prompt_builder=get_dirty_reused_worktree_decision_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=implementer_claude_timeout(),
                session_agent=AGENT_IMPLEMENTER,
                prompt_kwargs={
                    "branch_name": item.branch,
                    "status_text": item.payload.get("worktree_status", ""),
                    "diff_text": item.payload.get("worktree_diff", ""),
                },
                descr="dirty_decision",
            )
            return JobRequest(job, on_done_state=adopted_next)

        if item.state == ADOPTED:
            # Existing-PR fast path complete: worktree ready on the PR's real
            # head branch — hand the PR to pr_review (doc step 1 "skip to
            # step 8": nothing to implement, commit, or create).
            logger.info(
                "implementation:%d: adopted PR #%s (branch %r); advancing to pr_review",
                item.issue,
                item.pr,
                item.branch,
            )
            return StageOutcome(Disposition.ADVANCE, f"existing PR #{item.pr}")

        if item.state == ADVISE_WAIT:
            if not ctx.config.enable_advise:
                logger.info("implementation:%d: advise disabled; skipping", item.issue)
                return Continue(next_state=IMPLEMENT_WAIT)
            logger.info("implementation:%d: requesting advise job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "advise", advise_model),
                prompt_builder=get_advise_prompt_builder(ctx.config.agent),
                cwd=_worktree_path(item, ctx),
                timeout_s=advise_claude_timeout(),
                session_agent=AGENT_ADVISE,
                prompt_kwargs={
                    "issue_number": item.issue,
                    "issue_title": item.payload.get("issue_title", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                    "marketplace_path": item.payload.get("marketplace_path", ""),
                },
                descr="advise",
            )
            return JobRequest(job, on_done_state=IMPLEMENT_WAIT)

        if item.state == IMPLEMENT_WAIT:
            budget = ctx.budget("implement")
            if item.attempts.get("implement", 0) >= budget:
                logger.error(
                    "implementation:%d: implement budget exhausted (%d/%d)",
                    item.issue,
                    item.attempts.get("implement", 0),
                    budget,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "implement_exhausted")
            # Clear stale results at submission so a failed later attempt can
            # never replay an earlier attempt's output downstream.
            item.payload.pop("implement_error", None)
            item.payload.pop("implement_summary", None)
            logger.info("implementation:%d: requesting implement job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "implementer", implementer_model),
                prompt_builder=build_implementation_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=implementer_claude_timeout(),
                session_agent=AGENT_IMPLEMENTER,
                prompt_kwargs={
                    "issue_number": item.issue,
                    "issue_title": item.payload.get("issue_title", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                    "branch_name": item.branch,
                    "worktree_path": item.worktree,
                    "advise_findings": item.payload.get("advise_findings", ""),
                },
                descr="implement",
            )
            return JobRequest(job, on_done_state=TEST_WAIT)

        if item.state == TEST_WAIT:
            if item.payload.pop("implement_error", None):
                # The implement job hard-failed. The attempt was counted in
                # on_job_done (doc: agent_error consumes the implement
                # budget); RETRY re-enters the stage for the next attempt.
                return StageOutcome(Disposition.RETRY, "agent_error")
            if not getattr(ctx.config, "run_pre_pr_tests", False):
                return Continue(next_state=COMMIT_PUSH_WAIT)
            item.payload.pop("tests_failed", None)
            item.payload.pop("test_output", None)
            logger.info("implementation:%d: requesting pre-PR test job", item.issue)
            test_job = BuildTestJob(
                repo=item.repo,
                cwd=_worktree_path(item, ctx),
                argv=PRE_PR_TEST_ARGV,
                timeout_s=PRE_PR_TEST_TIMEOUT_S,
                descr="pre_pr_tests",
            )
            return JobRequest(test_job, on_done_state=COMMIT_PUSH_WAIT)

        if item.state == TESTFIX_WAIT:
            budget = ctx.budget("test_fix")
            if item.attempts.get("test_fix", 0) >= budget:
                logger.error(
                    "implementation:%d: tests still red after %d fix attempt(s)",
                    item.issue,
                    budget,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "tests_red")
            logger.info("implementation:%d: requesting test-fix job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "implementer", implementer_model),
                prompt_builder=build_test_fix_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=implementer_claude_timeout(),
                session_agent=AGENT_IMPLEMENTER,
                prompt_kwargs={
                    "issue_number": item.issue,
                    "prev_iteration": item.attempts.get("test_fix", 0),
                    "test_output": item.payload.get("test_output", ""),
                },
                descr="test_fix",
            )
            return JobRequest(job, on_done_state=TEST_WAIT)

        if item.state == COMMIT_PUSH_WAIT:
            if item.payload.get("tests_failed"):
                return Continue(next_state=TESTFIX_WAIT)
            logger.info("implementation:%d: requesting commit+push job", item.issue)
            push_job = GitJob(
                repo=item.repo,
                op="commit_push",
                timeout_s=GIT_JOB_TIMEOUT_S,
                kwargs={
                    "issue_number": item.issue,
                    "worktree_path": item.worktree,
                    "branch": item.branch,
                    "agent": agent_provider(ctx),
                },
                descr="commit_push",
            )
            return JobRequest(push_job, on_done_state=PR_CREATE)

        if item.state == PR_CREATE:
            return self._create_pr(item, ctx)

        logger.warning("implementation:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store job results on the item payload (state is still the WAIT state).

        The implement attempt is counted HERE, on job completion (success or
        hard failure alike — doc: "agent_error -> RETRY (consumes the
        implement budget)"). Interrupted results never reach this method, so
        an interrupt can never burn budget.

        Args:
            item: The work item to update.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.state == WORKTREE_WAIT:
            self._on_worktree_done(item, result)
            return

        if item.state == DIRTY_DECISION_WAIT:
            if result.ok and result.value:
                # COMMIT/STASH decision; the git worker acts on it (#1817).
                item.payload["dirty_decision"] = str(result.value)
            return

        if item.state == ADVISE_WAIT:
            if result.ok and result.value:
                item.payload["advise_findings"] = result.value
            return

        if item.state == IMPLEMENT_WAIT:
            self._on_implement_done(item, result)
            return

        if item.state == TEST_WAIT:
            self._on_tests_done(item, result)
            return

        if item.state == TESTFIX_WAIT:
            item.attempts["test_fix"] = item.attempts.get("test_fix", 0) + 1
            return

        if item.state == COMMIT_PUSH_WAIT:
            self._on_commit_push_done(item, result)

    @staticmethod
    def _on_commit_push_done(item: WorkItem, result: JobResult) -> None:
        """Record commit+push success, no-commit skip, or git failure."""
        if result.ok:
            if not bool(result.value):
                item.payload["no_commits"] = True
            # A successful worker result ends the consecutive-git-failure
            # streak even when no commit was produced; PR_CREATE handles skip.
            item.payload.pop("git_error_retries", None)
            return
        error_text = (result.error or "").lower()
        if "no commits" in error_text:
            # Legacy _handle_runtime_error (:348): "no commits between
            # base and branch" maps to state:skip, not a hard failure.
            item.payload["no_commits"] = True
            return
        logger.warning("implementation:%s: commit+push failed: %s", item.issue, result.error)
        item.payload["git_error"] = True

    @staticmethod
    def _on_implement_done(item: WorkItem, result: JobResult) -> None:
        """Count the implement attempt and record its outcome.

        The attempt is counted on completion, success or hard failure alike
        (doc: "agent_error -> RETRY (consumes the implement budget)").

        Args:
            item: The work item to update.
            result: The implement job result.

        """
        item.attempts["implement"] = item.attempts.get("implement", 0) + 1
        if not result.ok:
            logger.warning("implementation:%s: implement job failed: %s", item.issue, result.error)
            item.payload["implement_error"] = True
            return
        if result.value:
            item.payload["implement_summary"] = str(result.value)

    @staticmethod
    def _on_worktree_done(item: WorkItem, result: JobResult) -> None:
        """Record the created worktree's path and dirty snapshot.

        A failed worktree job flags ``git_error`` (transient — the
        DIRTY_DECISION_WAIT step RETRYs without burning the implement
        budget).

        Args:
            item: The work item to update.
            result: The create_worktree job result.

        """
        if not result.ok:
            logger.warning("implementation:%s: worktree job failed: %s", item.issue, result.error)
            item.payload["git_error"] = True
            return
        # A successful worktree job ends the consecutive-git-failure streak.
        item.payload.pop("git_error_retries", None)
        value = result.value
        if isinstance(value, dict):
            item.worktree = str(value.get("path", item.worktree))
            item.payload["worktree_dirty"] = bool(value.get("dirty"))
            item.payload["worktree_status"] = str(value.get("status", ""))
            item.payload["worktree_diff"] = str(value.get("diff", ""))
        elif isinstance(value, str) and value:
            item.worktree = value

    @staticmethod
    def _on_tests_done(item: WorkItem, result: JobResult) -> None:
        """Record the pre-PR test outcome (output tail travels to the fixer).

        Args:
            item: The work item to update.
            result: The pre-PR test job result.

        """
        if result.ok and result.value in (0, None, True):
            item.payload.pop("tests_failed", None)
            item.payload.pop("test_output", None)
            return
        item.payload["tests_failed"] = True
        item.payload["test_output"] = "\n".join(
            part for part in (result.stdout_tail, result.stderr_tail, result.error) if part
        )

    def _gate(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """GATE [M]: existing-PR fast path, then the plan-review verdict gate.

        Re-houses ``_review_existing_pr`` (:750) and ``_ensure_plan_ready``
        (:429). All checks are at-or-past reads; the only write is the
        auto-merge deferral re-ensure on PR adoption [durable].

        agent_error bound (M1): a re-entry from a pr_review ``agent_error``
        fail-back (``payload["agent_error_failback"]``) that adopts an
        existing PR CONSUMES the ``implement`` budget — the adoption produces
        no implement job whose completion would otherwise count it, and
        without a moving counter the fail-back -> adopt -> ADVANCE cycle
        would ping-pong forever. Exhaustion terminates with
        ``agent_error_exhausted``.
        """
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")

        # Pop the fail-back marker unconditionally: on the fresh-implement
        # path below the budget is consumed by the implement job itself, so
        # the marker must never survive into a later GATE pass.
        agent_error_reentry = bool(item.payload.pop("agent_error_failback", None))

        existing_pr = item.pr or ctx.github.find_pr_for_issue(item.issue)
        if existing_pr:
            item.pr = existing_pr
            head_branch = ctx.github.get_pr_head_branch(existing_pr)
            if head_branch:
                item.branch = head_branch
            has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(existing_pr)
            if has_go:
                # item.pr/item.branch are set above so the ci stage receives
                # a fully-identified PR (m7).
                logger.info(
                    "implementation:%d: PR #%d already implementation-go; routing to ci",
                    item.issue,
                    existing_pr,
                )
                return StageOutcome(Disposition.FAIL_BACK, "already_implementation_go_pr")
            if agent_error_reentry:
                # M1: consume the implement budget at GATE-adoption so the
                # pr_review agent_error -> re-adopt cycle is bounded.
                attempts = item.attempts.get("implement", 0) + 1
                item.attempts["implement"] = attempts
                budget = ctx.budget("implement")
                if attempts >= budget:
                    logger.error(
                        "implementation:%d: agent_error fail-backs exhausted the "
                        "implement budget (%d/%d) re-adopting PR #%d — stopping; "
                        "the review/address infrastructure failed repeatedly and "
                        "re-adopting the same PR cannot fix it (manual look needed)",
                        item.issue,
                        attempts,
                        budget,
                        existing_pr,
                    )
                    return StageOutcome(Disposition.FINISH_FAIL, "agent_error_exhausted")
            # Adopt the PR's REAL head branch — never assume {issue}-auto-impl
            # (the _review_existing_pr branch-assumption bug) — and re-ensure
            # auto-merge stays deferred on the adopted PR [durable]: it must
            # never be armed before state:implementation-go.
            item.payload["existing_pr"] = True
            ctx.github.defer_auto_merge(existing_pr)
            logger.info(
                "implementation:%d: existing PR #%d (branch %r); preparing adopted worktree",
                item.issue,
                existing_pr,
                item.branch,
            )
            return Continue(next_state=WORKTREE_WAIT)

        labels = _issue_labels(item, ctx)
        # At-or-past (never equality): plan-go OR already implementation-go
        # both satisfy the gate; anything earlier fails back to plan_review.
        if not (is_plan_go(labels) or is_implementation_go(labels)):
            logger.info("implementation:%d: plan not GO; failing back", item.issue)
            return StageOutcome(Disposition.FAIL_BACK, "plan_not_go")

        if not item.branch:
            item.branch = f"{item.issue}-auto-impl"
        return Continue(next_state=WORKTREE_WAIT)

    def _create_pr(self, item: WorkItem, ctx: StageContext) -> StageOutcome:
        """PR_CREATE [M]: durable PR creation, then auto-merge deferral.

        The ``create_pr`` write is the stage's journal entry and happens
        BEFORE the advancing outcome (durable write precedes the queue
        push); ``defer_auto_merge`` immediately after creation preserves the
        legacy runner order (:623).
        """
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")

        if item.payload.pop("no_commits", None):
            logger.warning(
                "implementation:%d: no commits vs base; applying %s", item.issue, STATE_SKIP
            )
            write_skip_label(item.issue, ctx)
            return StageOutcome(Disposition.SKIP, "no commits vs base")
        if item.payload.pop("git_error", None):
            # Push failed: transient git/network trouble — RETRY the stage
            # without burning the implement budget, bounded by
            # GIT_ERROR_RETRY_CAP (M5).
            return self._git_retry(item, "commit_push failed")

        if item.pr is None:
            title = item.payload.get("issue_title") or f"[Auto] Implement issue #{item.issue}"
            body = get_pr_description(
                item.issue,
                summary=item.payload.get("implement_summary", "")
                or f"Automated implementation for issue #{item.issue}.",
                changes="See the PR diff for the full change set.",
                testing=item.payload.get("test_output") or "pixi run pytest tests/unit -q",
            )
            pr_number = ctx.github.create_pr(item.issue, item.branch, title, body)
            item.pr = pr_number
            logger.info("implementation:%d: created PR #%d", item.issue, pr_number)
        # Load-bearing legacy order (runner :623): defer auto-merge right
        # after ensuring the PR exists — never armed before implementation GO.
        ctx.github.defer_auto_merge(item.pr)
        return StageOutcome(Disposition.ADVANCE, f"PR #{item.pr} ready for review")

    @staticmethod
    def _git_retry(item: WorkItem, note: str) -> StageOutcome:
        """RETRY a transient git failure, bounded by GIT_ERROR_RETRY_CAP (M5).

        Transient worktree/push failures never burn the implement budget,
        but a persistently failing remote must still terminate: at the cap
        the item finishes failed (``git_error``). The consecutive-failure
        counter lives in ``payload["git_error_retries"]`` and is reset by
        any successful git job (see ``on_job_done``).

        Args:
            item: The work item whose git job failed.
            note: Human-readable failure note for the RETRY outcome.

        Returns:
            RETRY below the cap; FINISH_FAIL(``git_error``) at the cap.

        """
        retries = item.payload.get("git_error_retries", 0) + 1
        item.payload["git_error_retries"] = retries
        if retries > GIT_ERROR_RETRY_CAP:
            logger.error(
                "implementation:%s: %s; %d consecutive git failures (cap %d) — "
                "finishing failed (git_error): the remote/worktree is persistently "
                "broken and needs a manual look",
                item.issue,
                note,
                retries,
                GIT_ERROR_RETRY_CAP,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "git_error")
        logger.warning(
            "implementation:%s: %s; git retry %d/%d (implement budget untouched)",
            item.issue,
            note,
            retries,
            GIT_ERROR_RETRY_CAP,
        )
        return StageOutcome(Disposition.RETRY, note)
