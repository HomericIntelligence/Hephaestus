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
  -> ADDRESS_WAIT -> PUSH_WAIT -> EVAL -> (loop to REVIEW_WAIT) or terminal
  ``strict_gate_unavailable``. ``FOLLOWUP_WAIT`` remains only to drain legacy
  persisted work; a #2054 clean GO never enters it.
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
  HUMAN_BLOCKED: an explanatory PR comment is posted [durable, before the
  outcome] naming the blocking human thread count and that automation
  stands down, then finish failed with the PR left UNLABELED (a human must
  act; automation may not resolve their thread). GO + open automation
  thread -> downgraded to NOGO (address + re-review). Clean GO ->
  ``_write_internal_go`` performs one final human-thread live-read, verifies
  auto-merge is disabled, and posts an informational artifact. It does not
  apply ``state:implementation-go`` or arm auto-merge until #2055 adds the
  head-bound strict-review gate. Every
  real non-GO round durably writes ``state:implementation-no-go`` (doc
  section 5 owned label, "NOGO verdict, before retry/regress"; legacy
  ``_apply_impl_review_verdict`` -> ``mark_pr_implementation_no_go``
  :248) before looping/regressing, non-fatally. Exhaustion -> durably
  apply ``state:skip`` [durable] -> SKIP.
- Downgraded-GO cost (DELIBERATE 2-round divergence from legacy): legacy
  downgraded a GO with open automation threads and ran the address step in
  the SAME iteration; this stage records the downgrade in EVAL and lets
  the NEXT round's POST re-count the live threads before dispatching the
  address leg, so a downgraded GO costs one extra review round. Chosen
  because POST live-checks the unresolved counts (a thread resolved
  out-of-band between rounds skips the address leg entirely) and the
  budget/extension gate stays a single chokepoint in EVAL.
- Progress metric (#1554 parity): the extension gate compares AUTOMATION
  unresolved counts only — a human resolving their own thread is not
  automation progress and must not earn extension rounds.
- POST posts only SURVIVING threads: the round's reviewer threads are
  filtered through the validation job's verdict
  (:func:`_surviving_threads`, re-housed ``review_validator`` semantics —
  ``wont_fix`` findings are accepted and dropped, ``unaddressed`` prior
  findings are re-opened as new postable threads; an unparseable
  validator output filters nothing, the legacy fail-open).
- Real-commit gating (#1575): PUSH_WAIT's commit_push result is inspected
  in EVAL. A push that produced NO commit (the fix agent punted or
  self-reported a phantom fix) is NOT treated as addressed: the address
  step is retried ONCE with the ``build_unaddressed_directive`` block
  (via ``get_address_review_prompt``'s ``unaddressed_findings``), and a
  second consecutive no-commit turn is evaluated as an unaddressed round.
- If the one-shot no-commit retry's address/push leg hard-fails, EVAL treats
  that as an explicit agent infrastructure failure, not as a second no-commit
  review round: it consumes the retry sentinel/directive, fails back
  ``agent_error`` without burning ``pr_review_iter``, and relies on the bounded
  implementation re-adoption path to run a fresh REVIEW->VALIDATE cycle.
- agent_error fail-backs (address failure, reviewer-error cap, missing
  PR/worktree) set ``payload["agent_error_failback"]`` so the
  implementation GATE consumes the ``implement`` budget on re-adoption —
  the cross-stage ping-pong bound (M1). ``review_error_retries`` is reset
  by ``on_enter`` on each fresh implementation cycle.
- Zero-thread NOGO (``_handle_zero_thread_nogo``) is the ONE exception to
  the agent_error fail-back above: it retries in-stage (bounded by
  ``REVIEW_ERROR_RETRY_CAP``, no round burned), but at the cap it escalates
  directly with ``state:skip`` instead of failing back. A NOGO with no
  postable/unresolved thread is often a deterministic reviewer verdict
  (prose-only, no line-anchored findings); failing back would re-adopt the
  same PR through implementation with nothing new to address, exhausting
  the ``implement`` budget for a dead end the retry could never resolve
  (#2079).
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
- Legacy FOLLOWUP_WAIT intentionally stores nothing in ``on_job_done``: the
  follow-up job's output is a side effect (follow-up issues filed by the
  agent), not a payload value any later state consumes.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from html import escape
from typing import Any, cast

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

from ..events import PrReviewZeroThreadNogoEvent, ZeroThreadNogoAction
from .base import (
    GIT_JOB_TIMEOUT_S,
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
    _worktree_path,
    agent_provider,
    stage_model,
    write_skip_label,
)

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
PR_FINISH = "PR_FINISH"
FINISH = PR_FINISH

_STEP_HANDLER_NAMES: dict[str, str] = {
    ENTER: "_enter",
    REVIEW_WAIT: "_review_wait",
    VALIDATE_WAIT: "_validate_wait",
    POST: "_post",
    DIFFICULTY_WAIT: "_difficulty_wait",
    ADDRESS_WAIT: "_address",
    PUSH_WAIT: "_push_wait",
    EVAL: "_eval",
    FOLLOWUP_WAIT: "_followup_wait",
    PR_FINISH: "_finish",
}


def _issue_number(item: WorkItem) -> int:
    """Return the issue number after the stage-level guard has run."""
    if item.issue is None:
        raise RuntimeError("pr_review stage reached without an issue number")
    return item.issue


#: Max CONSECUTIVE reviewer-infrastructure failures (ERROR verdicts or
#: failed/valueless review jobs) tolerated before failing back
#: ``agent_error``. Bounds the in-stage ERROR retry loop without burning
#: ``pr_review_iter`` or stamping labels (#911/#1554; mirrors
#: plan_review.REVIEW_ERROR_RETRY_CAP). Reset whenever a real verdict
#: arrives.
REVIEW_ERROR_RETRY_CAP = 2

_ZERO_THREAD_NOGO_MARKER = "<!-- hephaestus-pr-review-zero-thread-nogo -->"
_NO_STRUCTURED_SUMMARY = "No structured reviewer summary was provided."

#: Round-scoped payload keys cleared at REVIEW_WAIT submission so a failed
#: later round can never replay an earlier round's results.
_ROUND_PAYLOAD_KEYS = (
    "review_verdict",
    "review_text",
    "review_failed",
    "validation_result",
    "review_threads",
    "raw_review_threads",
    "posted_thread_ids",
    "difficulty_tiers",
    "address_error",
    "address_output",
    "push_no_commit",
    "no_commit_retry_done",
    "unaddressed_findings",
)


def _parse_validation_result(raw: Any) -> dict[str, Any] | None:
    """Parse the validator job's output into its verdict dict, tolerantly.

    The validation prompt asks for a single fenced JSON block at the END of
    the response (``{"unaddressed": [...], "wont_fix": [...]}``); the parser
    takes the LAST parseable block (legacy last-block-wins convention), then
    falls back to treating the whole output as JSON. Returns None when
    nothing parses — callers fail open.

    Args:
        raw: The validation job's stored output (str, dict, or anything).

    Returns:
        The parsed verdict dict, or None when unparseable/absent.

    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL)
    for candidate in (*reversed(blocks), raw):
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _thread_ids(entries: Any) -> set[str]:
    """Collect the ``thread_id``/``id`` strings from a validator bucket."""
    ids: set[str] = set()
    if not isinstance(entries, list):
        return ids
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        thread_id = entry.get("thread_id") or entry.get("id")
        if thread_id:
            ids.add(str(thread_id))
    return ids


def _surviving_threads(
    threads: list[dict[str, Any]], validation_result: Any
) -> list[dict[str, Any]]:
    """Filter the round's reviewer threads through the validator's verdict.

    Re-housed ``review_validator`` consumption semantics (m1):

    - ``wont_fix`` entries are documented-by-design decisions — accepted,
      so any reviewer thread re-raising one of those thread ids is DROPPED
      (never re-posted; the legacy recurrence-acceptance path, #1329).
    - ``unaddressed`` entries are prior findings the current diff does NOT
      resolve — RE-OPENED as new postable threads (the legacy
      ``_classify_unaddressed_findings`` -> post path), unless the reviewer
      already re-raised the same thread id this round.

    Fail-open: a missing/unparseable validator output filters nothing — a
    validator blip must never suppress the reviewer's own findings (the
    legacy fail-open pattern).

    Args:
        threads: The round's reviewer-produced thread dicts.
        validation_result: The validation job's stored output.

    Returns:
        The surviving thread list to durably post.

    """
    surviving = [dict(t) for t in threads]
    parsed = _parse_validation_result(validation_result)
    if parsed is None:
        return surviving
    wont_fix_ids = _thread_ids(parsed.get("wont_fix"))
    if wont_fix_ids:
        surviving = [
            t for t in surviving if str(t.get("thread_id") or t.get("id") or "") not in wont_fix_ids
        ]
    present_ids = {str(t.get("thread_id") or t.get("id") or "") for t in surviving}
    unaddressed = parsed.get("unaddressed")
    if isinstance(unaddressed, list):
        for entry in unaddressed:
            if not isinstance(entry, dict):
                continue
            thread_id = str(entry.get("thread_id") or entry.get("id") or "")
            if thread_id and thread_id in present_ids:
                continue  # reviewer already re-raised it this round
            detail = (
                str(entry.get("detail") or "").strip()
                or str(entry.get("original_body") or "").strip()
                or "prior review comment not addressed"
            )
            surviving.append(
                {
                    "path": entry.get("path") or "",
                    "line": entry.get("line"),
                    "body": f"Reopened (prior round, still unaddressed): {detail}",
                }
            )
    return surviving


class PrReviewStage(Stage):
    """Stage: review -> validate -> post -> address -> EVAL.

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
    - FOLLOWUP_WAIT (legacy persisted work only): submit the follow-up job,
      then PR_FINISH -> FINISHED. A #2054 clean GO terminates at EVAL with
      ``strict_gate_unavailable`` instead.
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Contain an existing arm, then reset the cycle-relative round counter.

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
        if item.pr is not None:
            try:
                ctx.github.defer_auto_merge(item.pr)
            except Exception as exc:
                logger.error(
                    "pr_review:%s: failed to verify auto-merge disabled for PR #%d: %s",
                    item.issue,
                    item.pr,
                    exc,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
            item.armed = False
        cycle = item.attempts.get("implement", 0)
        if item.payload.get("pr_review_cycle") != cycle:
            item.payload["pr_review_cycle"] = cycle
            item.payload["pr_review_round"] = 0
            # Fresh implementation cycle: the consecutive reviewer-failure
            # streak restarts too (M1 — a re-entry after an agent_error
            # fail-back gets a fresh error budget; the implement budget,
            # consumed at the GATE, bounds the total number of cycles).
            item.payload.pop("review_error_retries", None)
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
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
            return self._fail_back_agent_error(item)

        handler_name = _STEP_HANDLER_NAMES.get(item.state)
        if handler_name is not None:
            handler = cast(
                Callable[[WorkItem, StageContext], StepResult],
                getattr(self, handler_name),
            )
            return handler(item, ctx)

        logger.warning("pr_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _enter(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ENTER advances to REVIEW_WAIT."""
        return Continue(next_state=REVIEW_WAIT)

    def _review_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """REVIEW_WAIT submits the inline review job with parsed verdicts."""
        issue = _issue_number(item)
        # Clear ALL round-scoped payload at submission (stale-result
        # guard, M3 pattern): a failed later round must never replay an
        # earlier round's verdict, threads, or address output.
        for key in _ROUND_PAYLOAD_KEYS:
            item.payload.pop(key, None)
        round_index = item.payload.get("pr_review_round", 0)
        logger.info(
            "pr_review:%d: requesting review job (round %d, PR #%d)",
            issue,
            round_index,
            item.pr,
        )
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_pr_review_analysis_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=AGENT_PR_REVIEWER,
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
                "include_nitpicks": bool(
                    getattr(
                        ctx.config,
                        "nitpick",
                        getattr(ctx.config, "include_nitpicks", False),
                    )
                ),
            },
            parse=parse_review_verdict,  # verdict parsed in-worker
            descr="review",
        )
        return JobRequest(job, on_done_state=VALIDATE_WAIT)

    def _validate_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """VALIDATE_WAIT either skips the dead round or submits validation."""
        issue = _issue_number(item)
        if item.payload.pop("review_failed", None):
            # The review job itself failed: skip the validate/post/
            # address leg — EVAL's missing-verdict ERROR path handles it
            # without burning a round.
            return Continue(next_state=EVAL)
        logger.info("pr_review:%d: requesting validation job", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_review_validation_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=AGENT_PR_REVIEWER,
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": item.issue,
                "prior_comments_json": item.payload.get("prior_comments_json", "[]"),
                "diff_text": item.payload.get("pr_diff", ""),
            },
            descr="validate",
        )
        return JobRequest(job, on_done_state=POST)

    def _difficulty_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """DIFFICULTY_WAIT submits the comment-difficulty job."""
        issue = _issue_number(item)
        logger.info("pr_review:%d: requesting difficulty job", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_comment_difficulty_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=AGENT_COMMENT_CLASSIFIER,
            prompt_kwargs={
                "issue_number": item.issue,
                "comments_json": json.dumps(item.payload.get("review_threads", [])),
            },
            descr="difficulty",
        )
        return JobRequest(job, on_done_state=ADDRESS_WAIT)

    def _push_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """PUSH_WAIT submits the commit+push job for the addressing changes."""
        issue = _issue_number(item)
        logger.info("pr_review:%d: requesting push job", issue)
        git_job = GitJob(
            repo=item.repo,
            op="commit_push",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={
                "issue_number": issue,
                "worktree_path": item.worktree,
                "branch": item.branch,
                "agent": agent_provider(ctx),
            },
            descr="push_fixes",
        )
        return JobRequest(git_job, on_done_state=EVAL)

    def _followup_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Drain a legacy follow-up item without granting merge eligibility."""
        issue = _issue_number(item)
        logger.info("pr_review:%d: requesting follow-up job", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=get_follow_up_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=follow_up_claude_timeout(),
            session_agent=AGENT_IMPLEMENTER,  # resume implementer session (legacy parity)
            prompt_kwargs={"issue_number": item.issue},
            descr="follow_up",
        )
        return JobRequest(job, on_done_state=PR_FINISH)

    def _finish(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Finish a legacy follow-up item after its side-effect job completes."""
        issue = _issue_number(item)
        logger.info("pr_review:%d: legacy follow-up completed; advancing to finished", issue)
        return StageOutcome(Disposition.ADVANCE, "legacy_followup_completed")

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

        if item.state == PUSH_WAIT:
            # Real-commit gate (#1575): commit_push reports whether a commit
            # was actually produced (value/changed True). A no-commit push
            # means the address turn was a phantom fix — EVAL must NOT treat
            # the round as addressed.
            item.payload["push_no_commit"] = not bool(result.value)
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
        """POST [M]: durably post SURVIVING threads, refresh unresolved counts.

        The thread post is the round's durable write (doc step 3). The
        reviewer's threads (parsed by the worker/coordinator (#1817) into
        ``payload["review_threads"]``) are first filtered through the
        validation job's verdict (:func:`_surviving_threads`, m1): wont_fix
        findings are dropped, unaddressed prior findings are re-opened.
        Zero open automation threads skip the address leg straight to EVAL
        (the legacy zero-thread guard — nothing to classify or address).
        """
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        raw_threads = [dict(t) for t in item.payload.get("review_threads") or []]
        threads = _surviving_threads(raw_threads, item.payload.get("validation_result"))
        item.payload["raw_review_threads"] = raw_threads
        # The surviving set is what gets posted, classified, and addressed.
        item.payload["review_threads"] = threads
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
        against the PR's unresolved threads (``get_address_review_prompt``,
        with any carried ``unaddressed_findings`` rendering the
        ``build_unaddressed_directive`` retry block, #1575).

        Fail-closed worktree guard: address jobs EDIT code, so they must
        never run in the shared checkout (wrong branch — it would commit
        fixes onto whatever the shared tree has checked out). Without a
        worktree the item fails back to implementation, whose GATE/worktree
        leg is the designated recovery (bounded by the M1 agent_error
        budget consumption).
        """
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if not item.worktree:
            logger.warning(
                "pr_review:%s: no worktree for the address step; failing back "
                "(never edit in the shared checkout)",
                item.issue,
            )
            return self._fail_back_agent_error(item)
        verdict = item.payload.get("review_verdict")
        if item.payload.get("existing_pr"):
            job = AgentJob(
                repo=item.repo,
                issue=item.issue if item.issue is not None else 0,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "implementer", implementer_model),
                prompt_builder=get_address_review_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=address_review_claude_timeout(),
                session_agent=AGENT_ADDRESS_REVIEW,
                prompt_kwargs={
                    "pr_number": item.pr,
                    "issue_number": item.issue,
                    "worktree_path": item.worktree,
                    "threads_json": json.dumps(item.payload.get("review_threads", [])),
                    "todo_block": item.payload.get("difficulty_tiers", ""),
                    # No-commit retry directive (#1575): non-empty ONLY on
                    # the one retry after a no-commit address turn;
                    # get_address_review_prompt renders it via
                    # build_unaddressed_directive.
                    "unaddressed_findings": list(item.payload.get("unaddressed_findings") or []),
                },
                descr="address",
            )
            return JobRequest(job, on_done_state=PUSH_WAIT)
        job = AgentJob(
            repo=item.repo,
            issue=item.issue if item.issue is not None else 0,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=get_impl_resume_feedback_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=implementer_claude_timeout(),
            session_agent=AGENT_IMPLEMENTER,
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
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        payload = item.payload

        address_error = self._handle_address_error(item)
        if address_error is not None:
            return address_error

        # Real-commit gate (#1575, M4): a no-commit push retries the address
        # once with the directive; the second no-commit turn falls through
        # and is evaluated as an unaddressed round.
        no_commit_retry = self._gate_no_commit(item)
        if no_commit_retry is not None:
            return no_commit_retry

        verdict = payload.get("review_verdict")
        if verdict is None or verdict.is_error:
            return self._handle_error_verdict(item, verdict)

        # Fresh counts AFTER the address/push leg, split by severity so a GO is
        # downgraded only by BLOCKING automation threads (#1856 / re-introduced #1554).
        blocking_auto, minor_auto, human_unresolved = (
            ctx.github.count_unresolved_threads_by_severity(item.pr)
        )
        automation_unresolved = blocking_auto + minor_auto  # progress-trail parity (#1554)
        unresolved = automation_unresolved + human_unresolved

        if self._is_zero_thread_nogo(verdict, payload, unresolved):
            return self._handle_zero_thread_nogo(item, ctx, verdict)

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

        if verdict.is_go and human_unresolved:
            # Unchanged human-blocked guard (pr_review.py:690-701).
            logger.info(
                "pr_review:%d: GO blocked by %d human thread(s); finishing (unlabeled)",
                item.issue,
                human_unresolved,
            )
            return self._handle_human_blocked(item.pr, human_unresolved, ctx)

        if verdict.is_go and blocking_auto == 0:
            return self._handle_clean_go(item, ctx, minor_auto)

        return self._handle_non_go(
            item,
            ctx,
            verdict,
            automation_unresolved,
            unresolved,
            round_done,
            soft_cap,
            hard_cap,
        )

    def _handle_non_go(
        self,
        item: WorkItem,
        ctx: StageContext,
        verdict: Any,
        automation_unresolved: int,
        unresolved: int,
        round_done: int,
        soft_cap: int,
        hard_cap: int,
    ) -> StepResult:
        """Persist a non-GO round and choose its bounded retry or terminal route."""
        if item.pr is None or item.issue is None:  # guarded by _eval; type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if not self._write_no_go(item.pr, ctx):
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        # #1554 parity (m2): the progress trail counts AUTOMATION threads
        # only — a human resolving their own thread is not automation
        # progress and must not earn extension rounds.
        # #1863: prev_unresolved is THIS round's pre-address snapshot
        # (POST's unresolved_auto) so the extension gate compares
        # pre-address vs post-address WITHIN the round being evaluated —
        # progress landing on the soft-cap round is no longer invisible
        # to a stale cross-round comparison.
        prev_unresolved = item.payload.get("unresolved_auto")
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
        made_progress = prev_unresolved is not None and automation_unresolved < prev_unresolved
        if round_done < hard_cap and made_progress:
            # #1554 progress-aware extension: rounds soft_cap+1..hard_cap are
            # admitted only while the AUTOMATION unresolved count strictly
            # decreases.
            logger.info(
                "pr_review:%d: extension round %d/%d earned (%s -> %d automation unresolved)",
                item.issue,
                round_done + 1,
                hard_cap,
                prev_unresolved,
                automation_unresolved,
            )
            return Continue(next_state=REVIEW_WAIT)

        logger.warning(
            "pr_review:%d: exhausted at round %d (automation unresolved %s -> %d); applying %s",
            item.issue,
            round_done,
            prev_unresolved,
            automation_unresolved,
            STATE_SKIP,
        )
        write_skip_label(item.issue, ctx)
        return StageOutcome(Disposition.SKIP, "exhaustion")

    @staticmethod
    def _is_zero_thread_nogo(verdict: Any, payload: dict[str, Any], total_unresolved: int) -> bool:
        """Return whether an explicit NOGO has no durable actionable threads."""
        return (
            getattr(verdict, "verdict", "").upper() == "NOGO"
            and total_unresolved == 0
            and not payload.get("posted_thread_ids")
        )

    @staticmethod
    def _structured_review_summary(raw: object) -> str:
        """Extract and bound the final JSON summary without exposing raw prose."""
        if not isinstance(raw, str):
            return _NO_STRUCTURED_SUMMARY
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
        for candidate in reversed(blocks):
            try:
                parsed = json.loads(candidate.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            summary = parsed.get("summary") if isinstance(parsed, dict) else None
            if isinstance(summary, str) and (compact := " ".join(summary.split())):
                escaped = escape(compact, quote=False)
                return escaped if len(escaped) <= 200 else f"{escaped[:197]}..."
        return _NO_STRUCTURED_SUMMARY

    def _handle_zero_thread_nogo(
        self, item: WorkItem, ctx: StageContext, verdict: Any
    ) -> Continue | StageOutcome:
        """Persist the anomaly, retry fresh, then escalate rather than fail back.

        A zero-thread NOGO can be a transient parse/tooling artifact (worth a
        bounded fresh-review retry) or a deliberate reviewer verdict with no
        line-anchored findings, which is deterministic: re-review cannot
        change it. Fail-back would re-adopt the same PR through
        implementation with nothing concrete to address, exhausting the
        ``implement`` budget for a dead end (#2079). Once the retry cap is
        exhausted, escalate directly with ``state:skip`` instead — matching
        the existing exhaustion convention at :func:`_eval`'s hard-cap path
        and never re-entering implementation for an unchanged input.
        """
        if item.pr is None:
            return self._fail_back_agent_error(item)
        pr_number = item.pr
        retries = int(item.payload.get("review_error_retries", 0)) + 1
        item.payload["review_error_retries"] = retries
        action = (
            ZeroThreadNogoAction.RETRY_FRESH_REVIEW
            if retries <= REVIEW_ERROR_RETRY_CAP
            else ZeroThreadNogoAction.ESCALATE_SKIP
        )
        artifact_written = self._upsert_zero_thread_nogo_comment(
            item,
            ctx,
            summary=self._structured_review_summary(getattr(verdict, "raw", "")),
            retry_attempt=retries,
            action=action,
        )
        ctx.emit_event(
            PrReviewZeroThreadNogoEvent(
                repo=item.repo,
                issue=_issue_number(item),
                pr=pr_number,
                completed_rounds=int(item.payload.get("pr_review_round", 0)),
                retry_attempt=retries,
                retry_cap=REVIEW_ERROR_RETRY_CAP,
                action=action,
                artifact_written=artifact_written,
            )
        )
        if action is ZeroThreadNogoAction.RETRY_FRESH_REVIEW:
            logger.warning(
                "pr_review:%s: zero-thread NOGO; retry %d/%d without consuming a round",
                item.issue,
                retries,
                REVIEW_ERROR_RETRY_CAP,
            )
            return Continue(next_state=REVIEW_WAIT)
        logger.error(
            "pr_review:%s: zero-thread NOGO retry cap exhausted with the same "
            "artifactless verdict; escalating %s (re-adopting cannot fix a "
            "deterministic input)",
            item.issue,
            STATE_SKIP,
        )
        write_skip_label(_issue_number(item), ctx)
        return StageOutcome(Disposition.SKIP, "zero_thread_nogo_exhausted")

    @staticmethod
    def _upsert_zero_thread_nogo_comment(
        item: WorkItem,
        ctx: StageContext,
        *,
        summary: str,
        retry_attempt: int,
        action: ZeroThreadNogoAction,
    ) -> bool:
        """Upsert one bounded PR artifact describing the anomaly."""
        if item.pr is None:
            return False
        pr_number = item.pr
        action_text = (
            "A fresh reviewer invocation will be requested without consuming a review round."
            if action is ZeroThreadNogoAction.RETRY_FRESH_REVIEW
            else (
                "The reviewer retry cap is exhausted with the same artifactless verdict on "
                "every attempt; automation is standing down (`state:skip`) instead of "
                "re-adopting this PR through implementation, since a deterministic verdict "
                "cannot be changed by re-review."
            )
        )
        body = (
            f"{_ZERO_THREAD_NOGO_MARKER}\n"
            "**Automated PR review anomaly: NOGO without actionable threads.**\n\n"
            f"Reviewer summary:\n> {summary}\n\n"
            "No postable or unresolved review thread accompanied this verdict, so "
            "`state:implementation-no-go` is not being applied. "
            f"{action_text}\n\n"
            f"Artifactless reviewer attempt: {retry_attempt}/{REVIEW_ERROR_RETRY_CAP}."
        )
        try:
            artifact_written = ctx.github.upsert_pr_comment(
                pr_number, _ZERO_THREAD_NOGO_MARKER, body
            )
        except Exception as error:
            logger.warning(
                "pr_review:%s: failed to upsert zero-thread NOGO artifact (%s)",
                item.issue,
                type(error).__name__,
            )
            return False
        return artifact_written

    def _handle_address_error(self, item: WorkItem) -> StageOutcome | None:
        """Fail back hard address/push errors with explicit retry cleanup."""
        payload = item.payload
        if not payload.pop("address_error", None):
            return None

        if payload.get("no_commit_retry_done") or payload.get("unaddressed_findings"):
            payload.pop("push_no_commit", None)
            payload.pop("no_commit_retry_done", None)
            payload.pop("unaddressed_findings", None)
            logger.warning(
                "pr_review:%d: no-commit retry address/push leg failed; "
                "consuming retry directive and failing back agent_error without "
                "burning a review round",
                item.issue,
            )
            return self._fail_back_agent_error(item)

        # The address/push leg hard-failed: the doc's agent_error route —
        # back to implementation for a fresh implement pass (bounded by
        # the implement budget). No labels, no round burned.
        logger.warning("pr_review:%d: address step failed; failing back", item.issue)
        return self._fail_back_agent_error(item)

    def _handle_clean_go(self, item: WorkItem, ctx: StageContext, minor_auto: int) -> StepResult:
        """Resolve advisory threads, record internal GO, and route onward."""
        if item.pr is None or item.issue is None:  # guarded by caller; narrowing
            return self._fail_back_agent_error(item)
        if minor_auto:
            # Automation owns these waved minor/nitpick threads; resolve them so
            # required_review_thread_resolution does not re-block at merge_wait.
            logger.info(
                "pr_review:%d: GO with %d advisory minor thread(s); resolving before strict gate",
                item.issue,
                minor_auto,
            )
            ctx.github.resolve_automation_threads(item.pr)
        logger.info(
            "pr_review:%d: clean GO; strict review remains pending for PR #%d",
            item.issue,
            item.pr,
        )
        blocked_reason = self._write_internal_go(item.pr, ctx)
        if blocked_reason is not None:
            return StageOutcome(Disposition.FINISH_FAIL, blocked_reason)
        return StageOutcome(Disposition.FINISH_FAIL, "strict_gate_unavailable")

    @staticmethod
    def _gate_no_commit(item: WorkItem) -> Continue | None:
        """Apply the real-commit gate (#1575): a no-commit push is never "addressed".

        A push that produced NO commit means the address turn self-reported
        a phantom fix. The FIRST such turn retries the address once, carrying
        the still-open threads as ``unaddressed_findings`` (rendered by
        ``build_unaddressed_directive`` inside ``get_address_review_prompt``)
        to re-ground the resumed session. A SECOND consecutive no-commit turn
        returns None so EVAL treats it as an unaddressed round. A real commit
        spends/clears the retry directive (legacy: "a progress round clears
        the retry directive").

        Args:
            item: The work item under evaluation.

        Returns:
            ``Continue(ADDRESS_WAIT)`` for the one retry, else None.

        """
        payload = item.payload
        no_commit = payload.pop("push_no_commit", None)
        if no_commit:
            if not payload.get("no_commit_retry_done"):
                payload["no_commit_retry_done"] = True
                retry_threads = (
                    payload.get("raw_review_threads") or payload.get("review_threads") or []
                )
                payload["unaddressed_findings"] = [dict(t) for t in retry_threads]
                logger.warning(
                    "pr_review:%s: address turn produced NO commit; retrying the "
                    "address once with the unaddressed-findings directive (#1575)",
                    item.issue,
                )
                return Continue(next_state=ADDRESS_WAIT)
            logger.warning(
                "pr_review:%s: address retry still produced no commit; "
                "treating this as an unaddressed round",
                item.issue,
            )
        elif no_commit is False:
            payload.pop("no_commit_retry_done", None)
            payload.pop("unaddressed_findings", None)
        return None

    def _handle_error_verdict(self, item: WorkItem, verdict: Any) -> StageOutcome:
        """Handle a missing/ERROR verdict: bounded RETRY, then fail back.

        Reviewer-infrastructure failure: labels untouched, no round burned,
        RETRY — bounded by the consecutive-failure cap (plan_review
        pattern), then fail back ``agent_error`` (#911/#1554/#1794).

        Args:
            item: The work item under evaluation.
            verdict: The stored verdict (None or an ERROR verdict).

        Returns:
            RETRY below the cap; the flagged agent_error fail-back at it.

        """
        payload = item.payload
        reason = "no verdict found" if verdict is None else "reviewer error"
        retries = payload.get("review_error_retries", 0) + 1
        payload["review_error_retries"] = retries
        if retries > REVIEW_ERROR_RETRY_CAP:
            logger.error(
                "pr_review:%s: %s; %d consecutive reviewer failures (cap %d)"
                " — failing back to implementation",
                item.issue,
                reason,
                retries,
                REVIEW_ERROR_RETRY_CAP,
            )
            return self._fail_back_agent_error(item)
        logger.warning(
            "pr_review:%s: %s; retry %d/%d (no round burned)",
            item.issue,
            reason,
            retries,
            REVIEW_ERROR_RETRY_CAP,
        )
        return StageOutcome(Disposition.RETRY, reason)

    @staticmethod
    def _fail_back_agent_error(item: WorkItem) -> StageOutcome:
        """FAIL_BACK ``agent_error``, flagging the re-entry for the M1 bound.

        Every agent_error fail-back marks
        ``payload["agent_error_failback"]`` so the implementation GATE's
        existing-PR adoption consumes the ``implement`` budget — without a
        moving counter the fail-back -> adopt -> ADVANCE cycle would
        ping-pong forever.

        Args:
            item: The work item failing back.

        Returns:
            The FAIL_BACK(``agent_error``) outcome.

        """
        item.payload["agent_error_failback"] = True
        return StageOutcome(Disposition.FAIL_BACK, "agent_error")

    @staticmethod
    def _write_no_go(pr_number: int, ctx: StageContext) -> bool:
        """Durably mark implementation NO-GO and verify auto-merge is deferred.

        Doc section 5 owned label ("NOGO verdict, before retry/regress"):
        written on EVERY real non-GO round so the PR durably reflects the
        latest converged verdict even across restarts (legacy
        ``mark_pr_implementation_no_go``, ``_review_phase.py:248``).

        Args:
            pr_number: GitHub PR number that earned the non-GO round.
            ctx: Stage context carrying the GitHub accessor.

        Returns:
            ``True`` when auto-merge is known disabled, else ``False``.

        """
        try:
            ctx.github.mark_pr_implementation_no_go(pr_number)
        except Exception as e:
            logger.warning(
                "pr_review: failed to mark PR #%d implementation-no-go (non-fatal): %s",
                pr_number,
                e,
            )
        return PrReviewStage._ensure_auto_merge_deferred(pr_number, ctx)

    @staticmethod
    def _ensure_auto_merge_deferred(pr_number: int, ctx: StageContext) -> bool:
        """Return whether the accessor verified an open PR has auto-merge disabled."""
        try:
            ctx.github.defer_auto_merge(pr_number)
        except Exception as e:
            logger.error(
                "pr_review: failed to verify auto-merge disabled on PR #%d: %s",
                pr_number,
                e,
            )
            return False
        return True

    @staticmethod
    def _post_human_blocked_comment(
        pr_number: int, human_unresolved: int, ctx: StageContext
    ) -> None:
        """Post the HUMAN_BLOCKED stand-down comment, non-fatally [durable].

        Written BEFORE the FINISH_FAIL outcome so the reason automation
        stood down is durably visible on the PR (M3): without it, an
        unlabeled PR that automation stops touching looks abandoned.

        Args:
            pr_number: GitHub PR number blocked by human threads.
            human_unresolved: Count of unresolved human-owned review threads.
            ctx: Stage context carrying the GitHub accessor.

        """
        body = (
            "**Automation stand-down: human review thread(s) block GO.**\n\n"
            f"The implementation review reached GO, but {human_unresolved} "
            "unresolved review thread(s) opened by a human remain on this PR. "
            "Automation will not resolve human threads and cannot act on them, "
            "so it is standing down: the PR is left unlabeled (no "
            "`state:implementation-go` / `state:implementation-no-go`) and "
            "auto-merge stays unarmed. Once the human thread(s) are resolved, "
            "the next automation pass will re-review this PR."
        )
        try:
            ctx.github.post_pr_comment(pr_number, body)
        except Exception as e:
            logger.warning(
                "pr_review: failed to post HUMAN_BLOCKED comment on PR #%d (non-fatal): %s",
                pr_number,
                e,
            )

    @staticmethod
    def _handle_human_blocked(
        pr_number: int, human_unresolved: int, ctx: StageContext
    ) -> StageOutcome:
        """Contain an armed PR before recording a human-review stand-down."""
        if not PrReviewStage._ensure_auto_merge_deferred(pr_number, ctx):
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        PrReviewStage._post_human_blocked_comment(pr_number, human_unresolved, ctx)
        return StageOutcome(Disposition.FINISH_FAIL, "human_blocked")

    @staticmethod
    def _write_internal_go(pr_number: int, ctx: StageContext) -> str | None:
        """Record clean internal review while preserving the strict-review gate.

        The temporary #2054 baseline neither applies ``state:implementation-go``
        nor arms auto-merge. It proves the PR is unarmed before publishing the
        internal result, so a stale label cannot merge it before #2055 lands.

        Args:
            pr_number: GitHub PR number that earned the clean GO.
            ctx: Stage context carrying the GitHub accessor.

        Returns:
            ``None`` on success, otherwise a terminal outcome reason.

        """
        _, _, human_unresolved = ctx.github.count_unresolved_threads_by_severity(pr_number)
        if human_unresolved:
            logger.info(
                "pr_review: clean GO recheck found %d late human thread(s) on PR #%d; "
                "not advancing",
                human_unresolved,
                pr_number,
            )
            if not PrReviewStage._ensure_auto_merge_deferred(pr_number, ctx):
                return "auto_merge_disable_failed"
            PrReviewStage._post_human_blocked_comment(pr_number, human_unresolved, ctx)
            return "human_blocked"

        if not PrReviewStage._ensure_auto_merge_deferred(pr_number, ctx):
            return "auto_merge_disable_failed"
        PrReviewStage._upsert_clean_go_comment(pr_number, ctx)
        return None

    @staticmethod
    def _upsert_clean_go_comment(pr_number: int, ctx: StageContext) -> None:
        """Leave a durable internal-GO artifact without granting merge eligibility."""
        body = (
            "<!-- hephaestus-pr-review-go -->\n"
            "Automated PR review result: GO.\n\n"
            "No unresolved blocking review threads were found by the automation reviewer. "
            "This is an internal review result only; independent strict review remains "
            "required and auto-merge remains disabled."
        )
        try:
            ctx.github.upsert_pr_comment(pr_number, "<!-- hephaestus-pr-review-go -->", body)
        except Exception as e:
            logger.warning(
                "pr_review: failed to upsert clean-GO review comment on PR #%d (non-fatal): %s",
                pr_number,
                e,
            )
