"""Merge-wait stage: arm auto-merge durably, poll non-blocking, learn on merge.

Re-houses ``ci_driver._arm_and_wait_for_merge`` (:584) /
``_wait_for_pr_terminal`` (:1492) / ``_resolve_dirty_pr`` (:923) /
``_resolve_blocked_pr`` (:986) as a pipeline stage
(docs/AUTOMATION_LOOP_ARCHITECTURE.md section "7. merge_wait" is the
binding contract):

- States: ENTER -> ARM -> POLL -> DIRTY_REBASE_WAIT -> DIRTY_PUSH_WAIT |
  BLOCKED_ADDRESS_WAIT -> BLOCKED_PUSH_WAIT -> (POLL) -> LEARN_WAIT ->
  FINISH. Budgets: ``blocked_address`` = 2, ``rebase`` = 2 (both consumed
  in ``on_job_done``, read from ROUTES via ``ctx.budget``); the ``merge``
  poll-window bound stays wall-clock (below), untouched by the pipeline.
- ARM [M, durable]: arm squash auto-merge (``ctx.github.arm_auto_merge``)
  and durably persist the drive-green arming record
  (``ctx.github.arm_drive_green`` — the ``ArmingStateStore`` mirror)
  BEFORE the first POLL, so a crash between arming and polling can never
  lose the record the post-merge ``/learn`` dedupe keys off. Idempotent:
  an already-armed item (``item.armed``) skips straight to POLL.
- POLL [M], non-blocking: one PR-state read (``ctx.github.gh_pr_state`` +
  the required-check name reads, fetched with the legacy laziness) fed to
  the pure
  :func:`~hephaestus.automation.ci_run_coordinator.classify_pr_merge_state`
  (the sleep-free extraction of ``_wait_for_pr_terminal``'s branch
  logic — the legacy loop itself now delegates to the same classifier):

  - MERGED -> the post-merge ``/learn`` leg, DEDUPED via the arming
    record (``ctx.github.drive_green_learn_terminal`` — a terminal learn
    record finishes PASS immediately, firing ``/learn`` at most once per
    merged PR, the #848 contract);
  - FAILING -> FAIL_BACK(``ci_red``) (routes to ci);
  - DIRTY -> mechanical rebase+push (budget ``rebase``), then re-POLL;
    exhaustion -> FINISH_FAIL(``rebase_exhausted``) (the legacy
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
    The queue CLI's ``--max-merge-attempts`` feeds the ``merge`` budget:
    once pending polls reach that count, the issue is durably tagged
    ``state:skip`` and the item SKIPs.
- LEARN_WAIT [W:A]: the drive-green learnings session (re-housed
  ``post_merge_processor.run_drive_green_learnings``), prompt composed
  in-worker by :func:`build_drive_green_learn_prompt` reusing
  ``learn.build_learn_prompt`` verbatim. Best-effort: ``on_job_done``
  durably marks the learn result on the arming record
  (``ctx.github.mark_drive_green_learn_result``) BEFORE FINISH — success
  or failure alike, so a restart never replays ``/learn`` — and a failed
  learn never flips a merged PR to failure (FINISH is FINISH_PASS
  regardless).
- Owned labels: none (merge state is PR state); ``state:skip`` only on the
  genuinely-stuck exhaustion path above.
- Zero ``time.sleep`` / ``import time`` in this module (AC1) — enforced by
  ``tests/unit/automation/pipeline/test_pipeline_architecture.py``.
"""

from __future__ import annotations

import logging

from hephaestus.automation.agent_config import (
    address_review_claude_timeout,
    implementer_model,
    learn_claude_timeout,
)
from hephaestus.automation.auto_merge_coordinator import without_auto_merge_policy
from hephaestus.automation.ci_run_coordinator import PrMergeState, classify_pr_merge_state
from hephaestus.automation.learn import build_learn_prompt
from hephaestus.automation.prompts.address_review import get_address_review_prompt
from hephaestus.automation.session_naming import AGENT_ADDRESS_REVIEW, AGENT_CI_DRIVER
from hephaestus.constants import read_timeout_env

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
ARM = "ARM"
POLL = "POLL"
DIRTY_REBASE_WAIT = "DIRTY_REBASE_WAIT"
DIRTY_PUSH_WAIT = "DIRTY_PUSH_WAIT"
BLOCKED_ADDRESS_WAIT = "BLOCKED_ADDRESS_WAIT"
BLOCKED_PUSH_WAIT = "BLOCKED_PUSH_WAIT"
LEARN_WAIT = "LEARN_WAIT"
FINISH = "FINISH"

#: Poll backoff cap in seconds (legacy ``min(2**attempt, 60)`` —
#: ``ci_driver._wait_for_pr_terminal`` :1583).
BACKOFF_CAP_S = 60

#: Env var bounding the merge wait (legacy ``_wait_for_pr_terminal`` budget).
MERGE_MAX_WAIT_ENV = "HEPH_PR_MERGE_MAX_WAIT"

#: Default wall-clock merge-wait bound in seconds (legacy default 1800).
MERGE_MAX_WAIT_DEFAULT_S = 1800


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
    """Stage: arm durably, poll to terminal, resolve dirty/blocked, learn."""

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
        if item.state == DIRTY_REBASE_WAIT:
            return self._request_dirty_rebase(item, ctx)
        if item.state == DIRTY_PUSH_WAIT:
            return self._request_dirty_push(item, ctx)
        if item.state == BLOCKED_ADDRESS_WAIT:
            return self._request_blocked_address(item, ctx)
        if item.state == BLOCKED_PUSH_WAIT:
            return self._request_blocked_push(item, ctx)
        if item.state == LEARN_WAIT:
            return self._request_learn(item, ctx)
        if item.state == FINISH:
            # The PR merged; /learn already ran (best-effort) and its result
            # was durably marked in on_job_done. A failed learn never flips
            # a merged PR to failure.
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        logger.warning("merge_wait:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _arm(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ARM [M, durable]: arm auto-merge + persist the arming record, then POLL.

        Durable-order contract (the queue-push rule of :mod:`.base`): BOTH
        writes — ``arm_auto_merge`` and the ``arm_drive_green`` arming
        record — happen BEFORE the item ever reaches POLL, so a crash
        between arming and polling cannot lose the record the post-merge
        ``/learn`` dedupe keys off. Idempotent: ``item.armed`` short-circuits
        (restart = re-run, no duplicate mutations); the wall-clock anchor
        ``payload["merge_wait_started_at"]`` is stamped once (``ctx.now()``,
        the injectable clock).

        Failure semantics: a failed ``arm_auto_merge`` finishes
        ``arm_failed`` (the legacy ``auto-merge failed`` terminal). A failed
        arming-record write finishes ``arm_record_failed`` WITHOUT flipping
        ``item.armed`` — the PR must never sit armed with no durable dedupe
        record, or a crash could double-fire ``/learn``.
        """
        if item.pr is None:
            logger.warning("merge_wait:%s: no PR on item; finishing failed", item.issue)
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if "merge_wait_started_at" not in item.payload:
            item.payload["merge_wait_started_at"] = ctx.now()
        if item.armed or ctx.dry_run:
            return Continue(next_state=POLL)
        try:
            ctx.github.arm_auto_merge(item.pr)
        except Exception as e:
            logger.warning("merge_wait: failed to arm auto-merge on PR #%d: %s", item.pr, e)
            return StageOutcome(Disposition.FINISH_FAIL, "arm_failed")
        pr_state = ctx.github.gh_pr_state(item.pr)
        head_oid = str((pr_state or {}).get("headRefOid") or "")
        if item.issue is not None:
            try:
                ctx.github.arm_drive_green(item.issue, item.pr, head_oid)
            except Exception as e:
                logger.warning(
                    "merge_wait: failed to write arming record for PR #%d: %s", item.pr, e
                )
                return StageOutcome(Disposition.FINISH_FAIL, "arm_record_failed")
        item.armed = True
        return Continue(next_state=POLL)

    def _poll(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """POLL [M]: one non-blocking PR-state read -> classify -> route.

        The reads mirror ``_wait_for_pr_terminal``'s laziness exactly: the
        check-name reads are skipped for MERGED/CLOSED PRs, and the
        pending-check read happens only for a BLOCKED merge state with no
        failing checks (the legacy in-flight-checks guard). The pure
        classifier then owns every branch decision.
        """
        if item.pr is None:  # guarded by ARM; kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        started = item.payload.get("merge_wait_started_at")
        if started is None:
            logger.error(
                "merge_wait:%s: PR #%d reached POLL without merge_wait_started_at",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "missing_merge_wait_started_at")

        gh_state = ctx.github.gh_pr_state(item.pr)
        pr_state_str = ((gh_state or {}).get("state") or "").upper()
        failing: list[str] = []
        fixable_failing: list[str] = []
        pending: list[str] = []
        if pr_state_str not in ("MERGED", "CLOSED"):
            failing = ctx.github.failing_required_check_names(item.pr)
            fixable_failing = without_auto_merge_policy(failing)
            merge_status = ((gh_state or {}).get("mergeStateStatus") or "").upper()
            if merge_status == "BLOCKED" and not failing:
                pending = ctx.github.pending_required_check_names(item.pr)
        state = classify_pr_merge_state(gh_state, failing, fixable_failing, pending)

        if state is PrMergeState.MERGED:
            return self._route_merged(item, ctx)
        if state is PrMergeState.CLOSED:
            logger.info("merge_wait:%s: PR #%d closed without merging", item.issue, item.pr)
            return StageOutcome(Disposition.FINISH_FAIL, "closed")
        if state is PrMergeState.FAILING:
            logger.warning(
                "merge_wait:%s: PR #%d went red while awaiting merge (%s); regressing to ci",
                item.issue,
                item.pr,
                ", ".join(fixable_failing),
            )
            return StageOutcome(Disposition.FAIL_BACK, "ci_red")
        if state is PrMergeState.DIRTY:
            return self._route_dirty(item, ctx, gh_state)
        if state is PrMergeState.BLOCKED:
            return self._route_blocked(item, ctx)
        return self._route_pending(item, ctx, float(started))

    def _route_pending(self, item: WorkItem, ctx: StageContext, started: float) -> StageOutcome:
        """PENDING: timer-park with exponential backoff, bounded by time and budget."""
        now = ctx.now()
        max_wait = read_timeout_env(MERGE_MAX_WAIT_ENV, MERGE_MAX_WAIT_DEFAULT_S)
        if now - started > max_wait:
            logger.warning(
                "merge_wait:%s: PR #%d still OPEN after %ds (limit %ds); timing out",
                item.issue,
                item.pr,
                int(now - started),
                max_wait,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "timeout")
        polls = item.payload.get("merge_poll_count", 0)
        if polls >= ctx.budget("merge"):
            if item.issue is not None:
                logger.warning(
                    "merge_wait:%s: PR #%d exhausted merge pending budget (%d); skipping",
                    item.issue,
                    item.pr,
                    ctx.budget("merge"),
                )
                write_skip_label(item.issue, ctx)
                return StageOutcome(Disposition.SKIP, "merge_attempts_exhausted")
            return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
        delay = min(2**polls, BACKOFF_CAP_S)
        item.payload["merge_poll_count"] = polls + 1
        # Timer-park contract (base.py): the coordinator (#1817) reads the
        # delay from the payload — StageOutcome has no delay field.
        item.payload["retry_delay_s"] = delay
        return StageOutcome(Disposition.RETRY, "merge_pending")

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

    def _route_dirty(
        self, item: WorkItem, ctx: StageContext, gh_state: dict[str, object] | None
    ) -> StepResult:
        """DIRTY: mechanical rebase+push while the ``rebase`` budget remains.

        An armed-but-DIRTY (merge-conflict) PR can never merge while armed
        (#838), so waiting out the timeout is pointless. Exhaustion is the
        legacy ``_resolve_dirty_pr`` terminal — an unresolved merge conflict
        (``rebase_exhausted``), NOT a timeout. The PR's real base ref is
        captured for the rebase target (mirrors the legacy ``baseRefName``
        read, defaulting to ``main``).
        """
        if item.attempts.get("rebase", 0) >= ctx.budget("rebase"):
            logger.warning(
                "merge_wait:%s: PR #%d still conflicting after rebase budget; stopping",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "rebase_exhausted")
        item.payload["base_branch"] = str((gh_state or {}).get("baseRefName") or "main")
        return Continue(next_state=DIRTY_REBASE_WAIT)

    def _route_blocked(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """BLOCKED: address threads while budget remains; gate skip on stuck.

        Exhaustion fails back ``blocked_exhausted`` (routes to pr_review) —
        UNLESS the PR is genuinely stuck (``ctx.github.pr_is_genuinely_stuck``,
        the #1576 single source of truth), in which case ``state:skip`` is
        durably applied BEFORE the SKIP outcome. A BLOCKED-awaiting-review PR
        returns False there and is never skip-tagged.
        """
        if item.attempts.get("blocked_address", 0) >= ctx.budget("blocked_address"):
            if item.issue is not None and ctx.github.pr_is_genuinely_stuck(item.pr or 0):
                logger.warning(
                    "merge_wait:%d: PR #%s genuinely stuck after address budget; skipping",
                    item.issue,
                    item.pr,
                )
                write_skip_label(item.issue, ctx)
                return StageOutcome(Disposition.SKIP, "blocked_stuck")
            logger.warning(
                "merge_wait:%s: PR #%s still BLOCKED after address budget; regressing",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FAIL_BACK, "blocked_exhausted")
        return Continue(next_state=BLOCKED_ADDRESS_WAIT)

    def _request_dirty_rebase(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """DIRTY_REBASE_WAIT [W:G]: mechanical rebase onto the PR's base.

        The cheap deterministic path of the legacy ``_resolve_dirty_pr``:
        rebase the PR-head worktree onto ``origin/<base_branch>`` (worker
        ``op="rebase"`` = ``git_utils.rebase_worktree_onto``). The stale
        result flag is cleared at submission; ``on_job_done`` counts the
        ``rebase`` budget and records whether the rebase landed cleanly, and
        DIRTY_PUSH_WAIT pushes only a clean result.
        """
        item.payload.pop("rebase_clean", None)
        rebase_job = GitJob(
            repo=item.repo,
            op="rebase",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={
                "cwd": _worktree_path(item, ctx),
                "base_branch": str(item.payload.get("base_branch") or "main"),
            },
            descr="resolve_dirty_rebase",
        )
        return JobRequest(rebase_job, on_done_state=DIRTY_PUSH_WAIT)

    def _request_dirty_push(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """DIRTY_PUSH_WAIT [W:G]: push the clean rebase; a conflicted one re-polls.

        A clean rebase must be pushed (lease-guarded worker ``op="push"``)
        to re-trigger CI on the rebased head — the legacy rebase+push pair.
        A still-conflicting rebase has nothing to push: re-POLL re-classifies
        DIRTY and the ``rebase`` budget (already counted) bounds the loop to
        its ``rebase_exhausted`` terminal.
        """
        if not item.payload.pop("rebase_clean", None):
            return Continue(next_state=POLL)
        push_job = GitJob(
            repo=item.repo,
            op="push",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={
                "cwd": _worktree_path(item, ctx),
                "branch": item.branch or None,
            },
            descr="push_rebased_head",
        )
        return JobRequest(push_job, on_done_state=POLL)

    def _request_blocked_address(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """BLOCKED_ADDRESS_WAIT [W:A]: address the unresolved review threads.

        An armed PR sitting BLOCKED behind branch protection (unresolved
        threads with ``required_review_thread_resolution``) dispatches the
        address-review session — the same
        :func:`~hephaestus.automation.prompts.address_review.get_address_review_prompt`
        builder the pr_review existing-PR address leg uses (kwargs mirror its
        verified call shape). The unresolved-thread JSON / difficulty todo
        block are seeded into ``item.payload`` by the coordinator (#1817),
        which owns those gh reads. ``on_job_done`` counts the
        ``blocked_address`` budget; the push leg then re-POLLs.
        """
        item.payload.pop("address_failed", None)
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
                "issue_number": item.issue if item.issue is not None else 0,
                "worktree_path": str(_worktree_path(item, ctx)),
                "threads_json": item.payload.get("threads_json", "[]"),
                "todo_block": item.payload.get("difficulty_tiers", ""),
            },
            descr="blocked_address",
        )
        return JobRequest(job, on_done_state=BLOCKED_PUSH_WAIT)

    def _request_blocked_push(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """BLOCKED_PUSH_WAIT [W:G]: push the address fixes, or re-poll a dead turn.

        A hard-failed address session (``payload["address_failed"]``) has
        nothing to push — re-POLL re-classifies the live thread state and the
        ``blocked_address`` budget (already counted) bounds the loop.
        Otherwise commit+push the addressing changes so resolved threads can
        clear the branch-protection gate and the armed PR can merge.
        """
        if item.payload.pop("address_failed", None):
            return Continue(next_state=POLL)
        push_job = GitJob(
            repo=item.repo,
            op="commit_push",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={
                "issue_number": item.issue if item.issue is not None else 0,
                "worktree_path": _worktree_path(item, ctx),
                "branch": item.branch,
                "agent": AGENT_ADDRESS_REVIEW,
            },
            descr="push_blocked_address",
        )
        return JobRequest(push_job, on_done_state=POLL)

    def _request_learn(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """LEARN_WAIT [W:A]: dispatch the deduped post-merge /learn session.

        Prompt composed in-worker by :func:`build_drive_green_learn_prompt`.
        The dedupe already held at ``_route_merged`` (a terminal record never
        reaches here); ``on_job_done`` durably marks the outcome on the
        arming record BEFORE FINISH, closing the exactly-once loop.
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
        return JobRequest(job, on_done_state=FINISH)

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Consume budgets and record result flags (state is still the WAIT state).

        The coordinator contract (:mod:`.base`): ``item.state`` is still the
        WAIT state that submitted the job; the coordinator advances it to
        ``on_done_state`` AFTER this returns, so routing decisions are
        recorded as ``item.payload`` flags, never ``item.state`` writes.
        Budgets are consumed HERE, on completion, success or hard failure
        alike (sibling pattern; interrupted results never reach this method,
        so an interrupt never burns budget).

        - ``DIRTY_REBASE_WAIT``: count ``rebase``; record ``rebase_clean``
          (the worker's rebase result) so DIRTY_PUSH_WAIT pushes only a
          clean rebase.
        - ``DIRTY_PUSH_WAIT`` / ``BLOCKED_PUSH_WAIT``: best-effort — a
          failed push is logged and POLL re-classifies the live PR state
          (the budgets already counted bound the loop).
        - ``BLOCKED_ADDRESS_WAIT``: count ``blocked_address``; a hard job
          failure flags ``address_failed`` so the push leg re-polls instead
          of pushing a turn that never ran.
        - ``LEARN_WAIT``: durably mark the learn outcome on the arming
          record (``mark_drive_green_learn_result``, success or failure
          alike) BEFORE the FINISH_PASS outcome — the exactly-once /learn
          contract (#848). Non-fatal: a failed mark (or a failed learn) is
          logged and never flips the merged PR to failure.

        Args:
            item: The work item whose job completed.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.state == DIRTY_REBASE_WAIT:
            item.attempts["rebase"] = item.attempts.get("rebase", 0) + 1
            item.payload["rebase_clean"] = bool(result.ok and result.value)
            if not result.ok:
                logger.warning(
                    "merge_wait:%s: mechanical rebase failed (re-polling): %s",
                    item.issue,
                    result.error,
                )
            return
        if item.state == BLOCKED_ADDRESS_WAIT:
            item.attempts["blocked_address"] = item.attempts.get("blocked_address", 0) + 1
            if not result.ok:
                logger.warning(
                    "merge_wait:%s: BLOCKED address turn failed (re-polling): %s",
                    item.issue,
                    result.error,
                )
                item.payload["address_failed"] = True
            return
        if item.state in (DIRTY_PUSH_WAIT, BLOCKED_PUSH_WAIT):
            if not result.ok:
                logger.warning(
                    "merge_wait:%s: push failed (non-fatal, re-polling): %s",
                    item.issue,
                    result.error,
                )
            return
        if item.state == LEARN_WAIT:
            if not result.ok:
                logger.warning(
                    "merge_wait:%s: post-merge /learn failed (non-fatal): %s",
                    item.issue,
                    result.error,
                )
            if item.issue is not None:
                try:
                    ctx.github.mark_drive_green_learn_result(item.issue, succeeded=bool(result.ok))
                except Exception as e:
                    logger.warning(
                        "merge_wait:%d: failed to mark /learn result (non-fatal): %s",
                        item.issue,
                        e,
                    )
            return
