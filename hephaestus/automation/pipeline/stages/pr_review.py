"""PR-review stage: review, validate, post, address, evaluate, follow up.

Re-houses the fused implementation-review loop from ``_review_phase.py``
(``_run_impl_review_loop`` :671, ``_evaluate_go_verdict`` :314,
``_review_thread_count_decreased`` :155) and its collaborators
(``pr_reviewer.review_pr_inline``, ``review_validator
.validate_prior_comments_addressed``, ``address_review
.run_address_fix_session``) as a pipeline stage
(docs/AUTOMATION_LOOP_ARCHITECTURE.md section "5. pr_review" is the binding
contract):

- States: ENTER -> REVIEW_WAIT -> VALIDATE_WAIT -> POST -> DIFFICULTY_WAIT
  -> ADDRESS_WAIT -> PUSH_WAIT -> EVAL -> (loop to REVIEW_WAIT) ->
  FOLLOWUP_WAIT.
- Budgets: ``pr_review_iter`` = 3 (soft cap), ``pr_review_hard`` = 6 (hard
  cap; rounds 4-6 are admitted ONLY while the unresolved-thread count
  strictly decreases — the #1554 progress-aware extension, legacy
  ``_review_thread_count_decreased`` + the budget bump at
  ``_run_impl_review_loop:758-770``). Both read from ROUTES via
  ``ctx.budget``, never hardcoded here.
- Iteration accounting: ``item.attempts["pr_review_iter"]`` is the
  PER-LIFETIME audit trail (routing.py contract: attempts are never
  reset), so EVAL gates on the CYCLE-RELATIVE counter
  ``item.payload["pr_review_round"]``, reset by ``on_enter`` whenever a
  fresh implementation pass starts a new review cycle (keyed on
  ``attempts["implement"]``). ``attempts["pr_review_hard"]`` audits the
  extension rounds (rounds past the soft cap).
- Rounds advance in EVAL and ONLY for real verdicts (GO/NOGO/AMBIGUOUS).
  ERROR and missing verdicts never burn a round or touch labels
  (#911/#1554/#1794); they RETRY, bounded in-stage by
  ``payload["review_error_retries"]`` (cap :data:`REVIEW_ERROR_RETRY_CAP`
  consecutive failures, reset on any real verdict — the plan_review
  pattern). At the cap the item fails back ``agent_error`` (routes to
  implementation: a fresh implement pass, bounded by the ``implement``
  budget, is the doc's designated agent-error recovery).
- EVAL verdict semantics (re-housed ``_evaluate_go_verdict``): a GO stands
  only with ZERO unresolved threads (#1152). GO + open HUMAN thread ->
  HUMAN_BLOCKED: finish failed with the PR left UNLABELED (a human must
  act; automation may not resolve their thread). GO + open automation
  thread -> downgraded to NOGO (address + re-review). Clean GO -> durably
  ``mark_pr_implementation_go`` then ``arm_auto_merge`` [durable, in that
  order — the label authorizes the arming; arming is skipped if the mark
  write fails] -> follow-up step -> ADVANCE. Exhaustion -> durably apply
  ``state:skip`` [durable] -> SKIP.
- Prompt functions (imported, never re-authored):
  ``prompts/pr_review.py get_pr_review_analysis_prompt`` /
  ``get_review_validation_prompt`` / ``get_comment_difficulty_prompt``,
  ``prompts/implementation.py get_impl_resume_feedback_prompt`` (fresh-PR
  address path), ``prompts/address_review.py get_address_review_prompt``
  (existing-PR address path), ``prompts/follow_up.py get_follow_up_prompt``.
- Verdict parsed IN-WORKER by ``claude_invoke.parse_review_verdict``
  (carried as the review job's ``parse`` callable; symbol-scoped zero-I/O
  exemption mirrors plan_review's). REVIEW_WAIT clears all stale
  round-scoped payload at submission so a failed later round can never
  replay an earlier round's verdict or threads.
- FOLLOWUP_WAIT intentionally stores nothing in ``on_job_done``: the
  follow-up job's output is a side effect (follow-up issues filed by the
  agent), not a payload value any later state consumes.
"""

from __future__ import annotations

import json
import logging

from hephaestus.automation.agent_config import (
    address_review_claude_timeout,
    follow_up_claude_timeout,
    implementer_claude_timeout,
    implementer_model,
    pr_reviewer_claude_timeout,
    reviewer_model,
)
from hephaestus.automation.claude_invoke import parse_review_verdict
from hephaestus.automation.prompts.address_review import get_address_review_prompt
from hephaestus.automation.prompts.follow_up import get_follow_up_prompt
from hephaestus.automation.prompts.implementation import get_impl_resume_feedback_prompt
from hephaestus.automation.prompts.pr_review import (
    get_comment_difficulty_prompt,
    get_pr_review_analysis_prompt,
    get_review_validation_prompt,
)
from hephaestus.automation.session_naming import (
    AGENT_ADDRESS_REVIEW,
    AGENT_COMMENT_CLASSIFIER,
    AGENT_IMPLEMENTER,
    AGENT_PR_REVIEWER,
)
from hephaestus.automation.state_labels import STATE_SKIP

from .base import (
    AgentJob,
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
)
from .implementation import GIT_JOB_TIMEOUT_S, _worktree_path

logger = logging.getLogger(__name__)

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
REVIEW_WAIT = "REVIEW_WAIT"
VALIDATE_WAIT = "VALIDATE_WAIT"
POST = "POST"
DIFFICULTY_WAIT = "DIFFICULTY_WAIT"
ADDRESS_WAIT = "ADDRESS_WAIT"
PUSH_WAIT = "PUSH_WAIT"
EVAL = "EVAL"
FOLLOWUP_WAIT = "FOLLOWUP_WAIT"
FINISH = "FINISH"

#: Max CONSECUTIVE reviewer-infrastructure failures (ERROR verdicts or
#: failed/valueless review jobs) tolerated before failing back
#: ``agent_error``. Bounds the in-stage ERROR retry loop without burning
#: ``pr_review_iter`` or stamping labels (#911/#1554; mirrors
#: plan_review.REVIEW_ERROR_RETRY_CAP). Reset whenever a real verdict
#: arrives.
REVIEW_ERROR_RETRY_CAP = 2

#: Round-scoped payload keys cleared at REVIEW_WAIT submission so a failed
#: later round can never replay an earlier round's results.
_ROUND_PAYLOAD_KEYS = (
    "review_verdict",
    "review_text",
    "review_failed",
    "validation_result",
    "review_threads",
    "posted_thread_ids",
    "difficulty_tiers",
    "address_error",
    "address_output",
)


class PrReviewStage(Stage):
    """Stage: review -> validate -> post -> address -> EVAL -> follow-up.

    State machine (doc section "5. pr_review"):

    - ENTER: route to REVIEW_WAIT.
    - REVIEW_WAIT: clear stale round payload, submit the inline-review job
      (verdict parsed in-worker; review text is the verdict's ``raw``).
    - VALIDATE_WAIT: submit the prior-comment validation job (skipped
      straight to EVAL when the review job failed — the ERROR path burns
      no downstream work).
    - POST [M]: durably post surviving review threads, refresh the
      unresolved-thread counts; zero open automation threads skip the
      address leg straight to EVAL.
    - DIFFICULTY_WAIT: submit the comment-difficulty classification job.
    - ADDRESS_WAIT: fresh-PR path resumes the implementer with the review
      feedback; existing-PR path runs the address-review session.
    - PUSH_WAIT: commit+push the addressing changes.
    - EVAL [M]: re-housed ``_evaluate_go_verdict`` + budget gate (see
      module docstring).
    - FOLLOWUP_WAIT (GO only): submit the follow-up job, then FINISH ->
      ADVANCE.
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Reset the cycle-relative round counter on a new implementation pass.

        ``attempts["pr_review_iter"]`` is per-lifetime (routing.py: attempts
        are never reset), so the per-cycle review budget is tracked in
        ``payload["pr_review_round"]``. The reset keys on
        ``attempts["implement"]`` (recorded in ``payload["pr_review_cycle"]``)
        so it fires exactly once per implementation pass: a same-cycle
        re-entry (e.g. the ERROR-path RETRY) keeps its round count and its
        progress trail. Idempotent — a literal double on_enter is a no-op.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
        cycle = item.attempts.get("implement", 0)
        if item.payload.get("pr_review_cycle") != cycle:
            item.payload["pr_review_cycle"] = cycle
            item.payload["pr_review_round"] = 0
            item.payload.pop("prev_unresolved", None)
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901
        """Execute the next PR-review action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if not item.issue:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        if item.pr is None:
            # Nothing to review: fail back to implementation, whose
            # PR_CREATE step is the designated (re)creation path.
            logger.warning("pr_review:%d: no PR on item; failing back", item.issue)
            return StageOutcome(Disposition.FAIL_BACK, "agent_error")

        if item.state == ENTER:
            return Continue(next_state=REVIEW_WAIT)

        if item.state == REVIEW_WAIT:
            # Clear ALL round-scoped payload at submission (stale-result
            # guard, M3 pattern): a failed later round must never replay an
            # earlier round's verdict, threads, or address output.
            for key in _ROUND_PAYLOAD_KEYS:
                item.payload.pop(key, None)
            round_index = item.payload.get("pr_review_round", 0)
            logger.info(
                "pr_review:%d: requesting review job (round %d, PR #%d)",
                item.issue,
                round_index,
                item.pr,
            )
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=AGENT_PR_REVIEWER,
                model=reviewer_model(),
                prompt_builder=get_pr_review_analysis_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=pr_reviewer_claude_timeout(),
                # Diff / body / CI context are seeded into item.payload by
                # the coordinator (#1817), which owns the gh reads.
                prompt_kwargs={
                    "pr_number": item.pr,
                    "issue_number": item.issue,
                    "pr_diff": item.payload.get("pr_diff", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                    "ci_status": item.payload.get("ci_status", ""),
                    "pr_description": item.payload.get("pr_description", ""),
                    "advise_findings": item.payload.get("advise_findings", ""),
                    "include_nitpicks": False,
                },
                parse=parse_review_verdict,  # verdict parsed in-worker
                descr="review",
            )
            return JobRequest(job, on_done_state=VALIDATE_WAIT)

        if item.state == VALIDATE_WAIT:
            if item.payload.pop("review_failed", None):
                # The review job itself failed: skip the validate/post/
                # address leg — EVAL's missing-verdict ERROR path handles it
                # without burning a round.
                return Continue(next_state=EVAL)
            logger.info("pr_review:%d: requesting validation job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=AGENT_PR_REVIEWER,
                model=reviewer_model(),
                prompt_builder=get_review_validation_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=pr_reviewer_claude_timeout(),
                prompt_kwargs={
                    "pr_number": item.pr,
                    "issue_number": item.issue,
                    "prior_comments_json": item.payload.get("prior_comments_json", "[]"),
                    "diff_text": item.payload.get("pr_diff", ""),
                },
                descr="validate",
            )
            return JobRequest(job, on_done_state=POST)

        if item.state == POST:
            return self._post(item, ctx)

        if item.state == DIFFICULTY_WAIT:
            logger.info("pr_review:%d: requesting difficulty job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=AGENT_COMMENT_CLASSIFIER,
                model=reviewer_model(),
                prompt_builder=get_comment_difficulty_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=pr_reviewer_claude_timeout(),
                prompt_kwargs={
                    "issue_number": item.issue,
                    "comments_json": json.dumps(item.payload.get("review_threads", [])),
                },
                descr="difficulty",
            )
            return JobRequest(job, on_done_state=ADDRESS_WAIT)

        if item.state == ADDRESS_WAIT:
            return self._address(item, ctx)

        if item.state == PUSH_WAIT:
            logger.info("pr_review:%d: requesting push job", item.issue)
            git_job = GitJob(
                repo=item.repo,
                op="commit_push",
                timeout_s=GIT_JOB_TIMEOUT_S,
                kwargs={"branch": item.branch},
                descr="push_fixes",
            )
            return JobRequest(git_job, on_done_state=EVAL)

        if item.state == EVAL:
            return self._eval(item, ctx)

        if item.state == FOLLOWUP_WAIT:
            logger.info("pr_review:%d: requesting follow-up job", item.issue)
            job = AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent=AGENT_IMPLEMENTER,  # resume the implementer's session (legacy parity)
                model=implementer_model(),
                prompt_builder=get_follow_up_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=follow_up_claude_timeout(),
                prompt_kwargs={"issue_number": item.issue},
                descr="follow_up",
            )
            return JobRequest(job, on_done_state=FINISH)

        if item.state == FINISH:
            logger.info("pr_review:%d: follow-up completed; advancing", item.issue)
            return StageOutcome(Disposition.ADVANCE, "implementation review approved")

        logger.warning("pr_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store job results on the item payload (state is still the WAIT state).

        Args:
            item: The work item to update.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if not result.ok:
            logger.warning("pr_review:%s: job failed: %s", item.issue, result.error)
            if item.state == REVIEW_WAIT:
                # EVAL treats the missing verdict as a reviewer-infrastructure
                # ERROR; the flag lets VALIDATE_WAIT skip the dead round.
                item.payload["review_failed"] = True
            elif item.state in (ADDRESS_WAIT, PUSH_WAIT):
                item.payload["address_error"] = True
            return

        if item.state == REVIEW_WAIT and result.value is not None:
            item.payload["review_verdict"] = result.value
            item.payload["review_text"] = getattr(result.value, "raw", str(result.value))
        elif item.state == VALIDATE_WAIT and result.value is not None:
            item.payload["validation_result"] = result.value
        elif item.state == DIFFICULTY_WAIT and result.value is not None:
            item.payload["difficulty_tiers"] = str(result.value)
        elif item.state == ADDRESS_WAIT and result.value is not None:
            item.payload["address_output"] = str(result.value)
        # FOLLOWUP_WAIT intentionally has no branch: the follow-up job's
        # output is a side effect (issues filed by the agent), not a payload
        # value any later state consumes.

    def _post(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """POST [M]: durably post surviving threads, refresh unresolved counts.

        The thread post is the round's durable write (doc step 3). The
        surviving-thread list is parsed from the review/validation outputs
        by the worker/coordinator (#1817) into ``payload["review_threads"]``.
        Zero open automation threads skip the address leg straight to EVAL
        (the legacy zero-thread guard — nothing to classify or address).
        """
        if item.pr is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FAIL_BACK, "agent_error")
        threads = item.payload.get("review_threads") or []
        if threads:
            posted = ctx.github.post_review_threads(
                item.pr, list(threads), item.payload.get("review_text", "")
            )
            item.payload["posted_thread_ids"] = posted
        automation_unresolved, human_unresolved = ctx.github.count_unresolved_threads(item.pr)
        item.payload["unresolved_auto"] = automation_unresolved
        item.payload["unresolved_human"] = human_unresolved
        if automation_unresolved == 0:
            return Continue(next_state=EVAL)
        return Continue(next_state=DIFFICULTY_WAIT)

    def _address(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ADDRESS_WAIT: dispatch the fresh-PR or existing-PR address job.

        Fresh-PR path (this pipeline created the PR): resume the implementer
        session with the review feedback (doc step 5,
        ``get_impl_resume_feedback_prompt``). Existing-PR path (adopted by
        the implementation GATE fast path): run the address-review session
        against the PR's unresolved threads (``get_address_review_prompt``).
        """
        if item.pr is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FAIL_BACK, "agent_error")
        verdict = item.payload.get("review_verdict")
        if item.payload.get("existing_pr"):
            job = AgentJob(
                repo=item.repo,
                issue=item.issue if item.issue is not None else 0,
                agent=AGENT_ADDRESS_REVIEW,
                model=implementer_model(),
                prompt_builder=get_address_review_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=address_review_claude_timeout(),
                prompt_kwargs={
                    "pr_number": item.pr,
                    "issue_number": item.issue,
                    "worktree_path": item.worktree,
                    "threads_json": json.dumps(item.payload.get("review_threads", [])),
                    "todo_block": item.payload.get("difficulty_tiers", ""),
                },
                descr="address",
            )
            return JobRequest(job, on_done_state=PUSH_WAIT)
        job = AgentJob(
            repo=item.repo,
            issue=item.issue if item.issue is not None else 0,
            agent=AGENT_IMPLEMENTER,
            model=implementer_model(),
            prompt_builder=get_impl_resume_feedback_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=implementer_claude_timeout(),
            prompt_kwargs={
                "issue_number": item.issue,
                "prev_iteration": item.payload.get("pr_review_round", 0),
                "verdict": getattr(verdict, "verdict", "NOGO"),
                "review_text": item.payload.get("review_text", ""),
            },
            descr="address",
        )
        return JobRequest(job, on_done_state=PUSH_WAIT)

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """EVAL [M]: re-housed ``_evaluate_go_verdict`` + the budget gate.

        Every durable write below happens BEFORE the outcome that causes a
        queue push. The round counters (lifetime ``attempts`` audit trail
        and cycle-relative ``payload`` gate) advance here, and only for real
        verdicts — never for ERROR or missing verdicts (#911/#1554/#1794).
        """
        if item.pr is None or item.issue is None:  # guarded by step(); narrowing
            return StageOutcome(Disposition.FAIL_BACK, "agent_error")
        payload = item.payload

        if payload.pop("address_error", None):
            # The address/push leg hard-failed: the doc's agent_error route —
            # back to implementation for a fresh implement pass (bounded by
            # the implement budget). No labels, no round burned.
            logger.warning("pr_review:%d: address step failed; failing back", item.issue)
            return StageOutcome(Disposition.FAIL_BACK, "agent_error")

        verdict = payload.get("review_verdict")
        if verdict is None or verdict.is_error:
            # Reviewer-infrastructure failure: labels untouched, no round
            # burned, RETRY — bounded by the consecutive-failure cap
            # (plan_review pattern), then fail back agent_error.
            reason = "no verdict found" if verdict is None else "reviewer error"
            retries = payload.get("review_error_retries", 0) + 1
            payload["review_error_retries"] = retries
            if retries > REVIEW_ERROR_RETRY_CAP:
                logger.error(
                    "pr_review:%d: %s; %d consecutive reviewer failures (cap %d)"
                    " — failing back to implementation",
                    item.issue,
                    reason,
                    retries,
                    REVIEW_ERROR_RETRY_CAP,
                )
                return StageOutcome(Disposition.FAIL_BACK, "agent_error")
            logger.warning(
                "pr_review:%d: %s; retry %d/%d (no round burned)",
                item.issue,
                reason,
                retries,
                REVIEW_ERROR_RETRY_CAP,
            )
            return StageOutcome(Disposition.RETRY, reason)

        # Real verdict: this round counts. Reset the consecutive-failure
        # cap; advance the cycle-relative gate and the lifetime audit trail.
        payload["review_error_retries"] = 0
        round_done = payload.get("pr_review_round", 0) + 1
        payload["pr_review_round"] = round_done
        item.attempts["pr_review_iter"] = item.attempts.get("pr_review_iter", 0) + 1
        soft_cap = ctx.budget("pr_review_iter")
        hard_cap = ctx.budget("pr_review_hard")
        if round_done > soft_cap:
            # Audit trail of progress-earned extension rounds (4..hard_cap).
            item.attempts["pr_review_hard"] = item.attempts.get("pr_review_hard", 0) + 1

        # Fresh unresolved counts AFTER the address/push leg (the GO gate
        # must see what is open NOW, not the pre-address snapshot).
        automation_unresolved, human_unresolved = ctx.github.count_unresolved_threads(item.pr)
        unresolved = automation_unresolved + human_unresolved

        if verdict.is_go and human_unresolved:
            # A GO cannot stand while a HUMAN review thread is open —
            # automation must not resolve it and cannot fix it. Terminal,
            # with the PR left UNLABELED (no go, no no-go, no skip).
            logger.info(
                "pr_review:%d: GO blocked by %d human thread(s); finishing (unlabeled)",
                item.issue,
                human_unresolved,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "human_blocked")

        if verdict.is_go and unresolved == 0:
            logger.info("pr_review:%d: clean GO; marking PR #%d and arming", item.issue, item.pr)
            self._write_go_and_arm(item.pr, ctx)
            if getattr(ctx.config, "enable_follow_up", True):
                return Continue(next_state=FOLLOWUP_WAIT)
            return StageOutcome(Disposition.ADVANCE, "GO with zero unresolved threads")

        # NOGO/AMBIGUOUS — or a GO downgraded by open automation threads
        # (re-housed downgrade: address + re-review before GO can stand).
        prev_unresolved = payload.get("prev_unresolved")
        payload["prev_unresolved"] = unresolved
        if round_done < soft_cap:
            logger.info(
                "pr_review:%d: %s (round %d/%d, %d unresolved); re-reviewing",
                item.issue,
                verdict.verdict,
                round_done,
                soft_cap,
                unresolved,
            )
            return Continue(next_state=REVIEW_WAIT)
        made_progress = prev_unresolved is not None and unresolved < prev_unresolved
        if round_done < hard_cap and made_progress:
            # #1554 progress-aware extension: rounds soft_cap+1..hard_cap are
            # admitted only while the unresolved count strictly decreases.
            logger.info(
                "pr_review:%d: extension round %d/%d earned (%s -> %d unresolved)",
                item.issue,
                round_done + 1,
                hard_cap,
                prev_unresolved,
                unresolved,
            )
            return Continue(next_state=REVIEW_WAIT)

        logger.warning(
            "pr_review:%d: exhausted at round %d (unresolved %s -> %d); applying %s",
            item.issue,
            round_done,
            prev_unresolved,
            unresolved,
            STATE_SKIP,
        )
        self._write_skip_label(item.issue, ctx)
        return StageOutcome(Disposition.SKIP, "exhaustion")

    @staticmethod
    def _write_go_and_arm(pr_number: int, ctx: StageContext) -> None:
        """Durably mark implementation GO, then arm auto-merge (that order).

        Each write is non-fatal (legacy warn pattern), but arming is SKIPPED
        when the mark write fails: auto-merge must never be armed on a PR
        that did not durably receive ``state:implementation-go`` (the
        pr-policy gate would fail such a PR).

        Args:
            pr_number: GitHub PR number that earned the clean GO.
            ctx: Stage context carrying the GitHub accessor.

        """
        try:
            ctx.github.mark_pr_implementation_go(pr_number)
        except Exception as e:
            logger.warning(
                "pr_review: failed to mark PR #%d implementation-go (non-fatal, "
                "auto-merge NOT armed): %s",
                pr_number,
                e,
            )
            return
        try:
            ctx.github.arm_auto_merge(pr_number)
        except Exception as e:
            logger.warning(
                "pr_review: failed to arm auto-merge on PR #%d (non-fatal): %s", pr_number, e
            )

    @staticmethod
    def _write_skip_label(issue_number: int, ctx: StageContext) -> None:
        """Durably apply ``state:skip``, non-fatally (legacy warn pattern).

        Args:
            issue_number: GitHub issue number.
            ctx: Stage context carrying the GitHub accessor.

        """
        try:
            ctx.github.add_labels(issue_number, [STATE_SKIP])
        except Exception as e:
            logger.warning(
                "pr_review:%d: failed to add label %r (non-fatal): %s",
                issue_number,
                STATE_SKIP,
                e,
            )
