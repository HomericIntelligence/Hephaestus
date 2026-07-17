"""Merge-wait stage: fail closed while #2055 adds the strict-review gate.

Re-houses ``ci_driver._arm_and_wait_for_merge`` (:584) /
``_wait_for_pr_terminal`` (:1492) / ``_resolve_dirty_pr`` (:923) /
``_resolve_blocked_pr`` (:986) as a pipeline stage
(docs/AUTOMATION_LOOP_ARCHITECTURE.md section "7. merge_wait" is the
binding contract).

For every open PR, ARM verifies auto-merge is disabled
(``ctx.github.defer_auto_merge``) and finishes ``strict_gate_unavailable``. A
disable read-back failure is terminal. The only active POLL path is for an
already-merged PR, which preserves the existing exactly-once post-merge learn
bookkeeping via the arming record (``ctx.github.drive_green_learn_terminal``
— a terminal learn record finishes PASS immediately, firing ``/learn`` at
most once per merged PR, the #848 contract).

The former CI/dirty/blocked arming flow remains dormant compatibility code
(the ``_poll``/``_route_dirty``/``_route_blocked`` methods and their
DIRTY_REBASE_WAIT / DIRTY_PUSH_WAIT / BLOCKED_ADDRESS_WAIT /
BLOCKED_PUSH_WAIT states below) until #2055 reintroduces it behind a
head-bound strict-review proof:

- States: ENTER -> ARM -> POLL -> DIRTY_REBASE_WAIT -> DIRTY_PUSH_WAIT |
  BLOCKED_ADDRESS_WAIT -> BLOCKED_PUSH_WAIT -> (POLL) -> LEARN_WAIT ->
  MW_FINISH. Budgets: ``blocked_address`` = 2, ``rebase`` = 2 (both consumed
  in ``on_job_done``, read from ROUTES via ``ctx.budget``); the ``merge``
  poll-window bound stays wall-clock (below), untouched by the pipeline.
- POLL [M], non-blocking: one PR-state read (``ctx.github.gh_pr_state`` +
  the required-check name reads, fetched with the legacy laziness) fed to
  the pure
  :func:`~hephaestus.automation.ci_run_coordinator.classify_pr_merge_state`
  (the sleep-free extraction of ``_wait_for_pr_terminal``'s branch
  logic — the legacy loop itself now delegates to the same classifier):

  - MERGED -> the post-merge ``/learn`` leg, DEDUPED via the arming
    record;
  - FAILING -> FAIL_BACK(``ci_red``) (routes to ci);
  - DIRTY -> mechanical rebase (op="rebase", never pushes on its own) then
    an explicit push of the clean result (budget ``rebase``), then
    re-POLL; exhaustion -> FINISH_FAIL(``rebase_exhausted``) (the legacy
    "unresolved merge conflict" terminal — never "timeout");
  - BLOCKED -> address unresolved threads (budget ``blocked_address``)
    then push and re-POLL; exhaustion -> FAIL_BACK(``blocked_exhausted``)
    (routes to pr_review) unless ``ctx.github.pr_is_genuinely_stuck``
    holds (a BLOCKED-awaiting-review PR is NOT stuck, #1576), in which
    case ``state:skip`` is durably applied and the item SKIPs;
  - CLOSED -> FINISH_FAIL(``closed``);
  - PENDING -> timer-park: the backoff delay (legacy ``min(2**n, 60)``)
    is recorded in ``payload["retry_delay_s"]`` and the stage returns
    ``StageOutcome(RETRY)`` (base.py coordinator convention). The
    wall-clock bound is preserved via ``ctx.now()`` against
    ``payload["merge_wait_started_at"]`` (stamped at ARM; missing in POLL
    is an invariant failure, not a new stamp) and ``HEPH_PR_MERGE_MAX_WAIT``
    (default 1800s, exactly the legacy ``_wait_for_pr_terminal`` budget).
    The queue CLI's ``--drive-green-loops`` feeds the ``merge`` budget:
    once pending polls reach that count, the issue is durably tagged
    ``state:skip`` and the item SKIPs.
- LEARN_WAIT [W:A]: the drive-green learnings session (re-housed
  ``post_merge_processor.run_drive_green_learnings``), prompt composed
  in-worker by :func:`build_drive_green_learn_prompt` reusing
  ``learn.build_learn_prompt`` verbatim. Best-effort: ``on_job_done``
  durably marks the learn result on the arming record
  (``ctx.github.mark_drive_green_learn_result``) BEFORE MW_FINISH — success
  or failure alike, so a restart never replays ``/learn`` — and a failed
  learn never flips a merged PR to failure (MW_FINISH is FINISH_PASS
  regardless).
- Owned labels: none (merge state is PR state); ``state:skip`` only on the
  genuinely-stuck exhaustion path above.
- Zero ``time.sleep`` / ``import time`` in this module (AC1) — enforced by
  ``tests/unit/automation/pipeline/test_pipeline_architecture.py``.
"""

from __future__ import annotations

import logging

from hephaestus.automation.agent_config import implementer_model, learn_claude_timeout
from hephaestus.automation.learn import build_learn_prompt
from hephaestus.automation.session_naming import AGENT_CI_DRIVER

from .base import (
    AgentJob,
    Continue,
    Disposition,
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
)

logger = logging.getLogger(__name__)

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
ARM = "ARM"
POLL = "POLL"
LEARN_WAIT = "LEARN_WAIT"
MW_FINISH = "MW_FINISH"
FINISH = MW_FINISH


def build_drive_green_learn_prompt(issue_number: int, pr_number: int) -> str:
    """Compose the post-merge drive-green /learn prompt (built in-worker).

    Module-level composed builder (NOT a closure): :class:`AgentJob` is frozen
    and prompt builders run in-worker, so the builder must be a top-level
    function receiving everything via ``prompt_kwargs`` (mirrors
    :func:`..ci.build_ci_fix_prompt`). Reuses ``learn.build_learn_prompt``
    verbatim with the drive-green context string re-housed from
    ``post_merge_processor.run_drive_green_learnings`` so the learnings are
    scoped to what made CI fail and how it was fixed.

    Args:
        issue_number: GitHub issue number the merged PR closed.
        pr_number: GitHub PR number that reached green and merged.

    Returns:
        The full /learn prompt string.

    """
    return build_learn_prompt(
        f"You just drove PR #{pr_number} (issue #{issue_number}) "
        "to green CI. Capture concise learnings about what made CI fail and how "
        "you fixed it, scoped to this issue/PR."
    )


class MergeWaitStage(Stage):
    """Stage: contain auto-merge until the strict-review gate is available."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Initialize the mini-state; the durable arming lives in ARM.

        ARM is an [M] step of this stage so a restart re-runs it
        idempotently via step() (an armed item skips re-arming). Nothing
        durable is written here.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
        if not item.state:
            item.state = ENTER
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next merge-wait action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if item.state == ENTER:
            return Continue(next_state=ARM)
        if item.state == ARM:
            return self._arm(item, ctx)
        if item.state == POLL:
            return self._poll(item, ctx)
        if item.state == LEARN_WAIT:
            return self._request_learn(item, ctx)
        if item.state == MW_FINISH:
            # The PR merged; /learn already ran (best-effort) and its result
            # was durably marked in on_job_done. A failed learn never flips
            # a merged PR to failure.
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        logger.warning("merge_wait:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _arm(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Contain an open PR until #2055 provides a qualifying strict proof."""
        if item.pr is None:
            logger.warning("merge_wait:%s: no PR on item; finishing failed", item.issue)
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_state = ctx.github.gh_pr_state(item.pr)
        if ((pr_state or {}).get("state") or "").upper() == "MERGED":
            item.payload.setdefault("merge_wait_started_at", ctx.now())
            return Continue(next_state=POLL)
        try:
            ctx.github.defer_auto_merge(item.pr)
        except Exception as e:
            logger.error(
                "merge_wait: failed to verify auto-merge disabled on PR #%d: %s", item.pr, e
            )
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        item.armed = False
        return StageOutcome(Disposition.FINISH_FAIL, "strict_gate_unavailable")

    def _poll(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """POLL only the post-merge state needed for the deduped learn path."""
        if item.pr is None:  # guarded by ARM; kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        gh_state = ctx.github.gh_pr_state(item.pr)
        pr_state_str = ((gh_state or {}).get("state") or "").upper()
        if pr_state_str not in {"MERGED", "CLOSED"}:
            try:
                ctx.github.defer_auto_merge(item.pr)
            except Exception as exc:
                logger.error(
                    "merge_wait:%s: failed to verify auto-merge disabled for PR #%d: %s",
                    item.issue,
                    item.pr,
                    exc,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
            item.armed = False
            return StageOutcome(Disposition.FINISH_FAIL, "strict_gate_unavailable")

        started = item.payload.get("merge_wait_started_at")
        if started is None:
            logger.error(
                "merge_wait:%s: PR #%d reached POLL without merge_wait_started_at",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "missing_merge_wait_started_at")

        if pr_state_str == "MERGED":
            return self._route_merged(item, ctx)
        if pr_state_str == "CLOSED":
            logger.info("merge_wait:%s: PR #%d closed without merging", item.issue, item.pr)
            return StageOutcome(Disposition.FINISH_FAIL, "closed")
        return StageOutcome(Disposition.FINISH_FAIL, "strict_gate_unavailable")

    def _route_merged(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dedupe the MERGED PR's post-merge /learn via the arming record.

        The /learn leg fires at most once per merged PR (#848): a terminal
        learn record (``ctx.github.drive_green_learn_terminal``) — or learn
        disabled by config — finishes PASS immediately without dispatching
        the session.
        """
        if not getattr(ctx.config, "enable_learn", True):
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if item.issue is not None and ctx.github.drive_green_learn_terminal(item.issue):
            logger.info(
                "merge_wait:%d: /learn already terminal for PR #%s; deduped",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        return Continue(next_state=LEARN_WAIT)

    def _request_learn(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """LEARN_WAIT [W:A]: dispatch the deduped post-merge /learn session.

        Prompt composed in-worker by :func:`build_drive_green_learn_prompt`.
        The dedupe already held at ``_route_merged`` (a terminal record never
        reaches here); ``on_job_done`` durably marks the outcome on the
        arming record BEFORE MW_FINISH, closing the exactly-once loop.
        """
        job = AgentJob(
            repo=item.repo,
            issue=item.issue if item.issue is not None else 0,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=build_drive_green_learn_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=learn_claude_timeout(),
            session_agent=AGENT_CI_DRIVER,
            prompt_kwargs={
                "issue_number": item.issue if item.issue is not None else 0,
                "pr_number": item.pr,
            },
            descr="drive_green_learn",
        )
        return JobRequest(job, on_done_state=MW_FINISH)

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Record the post-merge learn result without changing a merged PR's outcome."""
        if item.state != LEARN_WAIT:
            return
        if not result.ok:
            logger.warning(
                "merge_wait:%s: post-merge /learn failed (non-fatal): %s",
                item.issue,
                result.error,
            )
        if item.issue is not None:
            try:
                ctx.github.mark_drive_green_learn_result(item.issue, succeeded=bool(result.ok))
            except Exception as exc:
                logger.warning(
                    "merge_wait:%d: failed to mark /learn result (non-fatal): %s",
                    item.issue,
                    exc,
                )
