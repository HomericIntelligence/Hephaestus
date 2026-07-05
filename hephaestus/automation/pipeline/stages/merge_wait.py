"""Merge-wait pipeline stage (issue #1816).

Non-blocking re-housing of ci_driver._arm_and_wait_for_merge /
_wait_for_pr_terminal / _resolve_dirty_pr / _resolve_blocked_pr. The single
PR-state read per POLL replaces the _wait_for_pr_terminal sleep loop; pending
-> RETRY(delay). NO time.sleep / import time in this module (AC1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hephaestus.automation.agent_config import (
    address_review_claude_timeout,
    implementer_model,
    learn_claude_timeout,
)
from hephaestus.automation.auto_merge_coordinator import without_auto_merge_policy
from hephaestus.automation.ci_run_coordinator import PrMergeState, classify_pr_merge_state
from hephaestus.automation.learn import build_learn_prompt
from hephaestus.automation.pr_manager import pr_is_genuinely_stuck
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
    Stage,
    StageContext,
    StageOutcome,
    _worktree_path,
    write_skip_label,
)

if TYPE_CHECKING:
    from hephaestus.automation.pipeline.work_item import WorkItem

logger = logging.getLogger(__name__)

_BACKOFF_CAP = 60


def build_drive_green_learn_prompt(issue_number: int, pr_number: int) -> str:
    """Compose the post-merge drive-green /learn prompt (built in-worker).

    Module-level composed builder (NOT a closure): :class:`AgentJob` is frozen
    and prompt builders run in-worker, so the builder must be a top-level
    function receiving everything via ``prompt_kwargs`` (mirrors
    :func:`..ci.build_ci_fix_prompt`). Reuses
    ``learn.build_learn_prompt`` verbatim with the drive-green context string
    re-housed from ``post_merge_processor.run_drive_green_learnings`` so the
    learnings are scoped to what made CI fail and how it was fixed.

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
    """Pipeline stage for merge-wait after CI green."""

    name = "merge_wait"

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Idempotent entry: initialize state if needed."""
        if not item.state:
            item.state = "ARM"
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest | StageOutcome:
        """Execute the next action for the item's current state."""
        if item.state == "ARM":
            return self._arm(item, ctx)
        if item.state == "POLL":
            return self._poll(item, ctx)
        if item.state == "DIRTY_REBASE_READY":
            return self._request_dirty_resolve(item, ctx)
        if item.state == "BLOCKED_ADDRESS_READY":
            return self._request_blocked_address(item, ctx)
        if item.state == "LEARN_READY":
            return self._request_learn(item, ctx)
        if item.state == "LEARN_WAIT":
            return self._finish_after_learn(item, ctx)
        raise AssertionError(f"unreachable merge_wait state {item.state!r}")

    def _arm(self, item: WorkItem, ctx: StageContext) -> Continue | StageOutcome:
        """Step 1 [M]: arm auto-merge if unarmed + write arming record [durable].

        Idempotent: an already-armed item (``item.armed``) skips straight to
        POLL without re-arming or re-writing the record. Otherwise arms
        squash auto-merge (``ctx.github.arm_auto_merge``), reads back the
        PR's current head OID, and durably persists the arming record
        (``ctx.github.arm_drive_green``) BEFORE flipping ``item.armed`` and
        advancing to POLL — the durable write precedes the state advance so
        a crash between arming and POLL cannot lose the arming record. If the
        arming-record write itself fails (AC3), the item is NOT flipped to
        armed/POLL: the stage finishes ``FINISH_FAIL("arm_record_failed")`` so
        the drive retries from a clean, unarmed state rather than leaving the
        PR armed with no durable dedupe record for the post-merge ``/learn``.
        """
        if item.armed:
            item.state = "POLL"
            return Continue("POLL")
        if ctx.dry_run:
            item.state = "POLL"
            return Continue("POLL")
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
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
                    "merge_wait: failed to write arming record for PR #%d: %s",
                    item.pr,
                    e,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "arm_record_failed")
        item.armed = True
        item.state = "POLL"
        return Continue("POLL")

    def _poll(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest | StageOutcome:
        """Step 2 [M]: single PR-state read → classify (non-blocking)."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        gh_state = ctx.github.gh_pr_state(item.pr)
        failing = ctx.github.failing_required_check_names(item.pr)
        pending = ctx.github.pending_required_check_names(item.pr)
        fixable_failing = without_auto_merge_policy(failing)
        state = classify_pr_merge_state(gh_state, failing, fixable_failing, pending)

        if state is PrMergeState.MERGED:
            item.state = "LEARN_READY"
            return Continue("LEARN_READY")
        if state is PrMergeState.CLOSED:
            return StageOutcome(Disposition.FINISH_FAIL, "closed")
        if state is PrMergeState.FAILING:
            return StageOutcome(Disposition.FAIL_BACK, "ci_red")
        if state is PrMergeState.DIRTY:
            rebase_budget = ctx.budget("rebase")
            rebase_attempts = item.attempts.get("rebase", 0)
            if rebase_attempts >= rebase_budget:
                return StageOutcome(Disposition.FINISH_FAIL, "timeout")
            # Capture the PR's base ref (mirrors _resolve_dirty_pr's baseRefName
            # read) so the mechanical rebase targets the correct base branch.
            item.payload["base_branch"] = str((gh_state or {}).get("baseRefName") or "main")
            item.state = "DIRTY_REBASE_READY"
            return Continue("DIRTY_REBASE_READY")
        if state is PrMergeState.BLOCKED:
            blocked_budget = ctx.budget("blocked_address")
            blocked_attempts = item.attempts.get("blocked_address", 0)
            if blocked_attempts >= blocked_budget:
                if item.issue and pr_is_genuinely_stuck(item.pr):
                    write_skip_label(item.issue, ctx)
                    return StageOutcome(Disposition.SKIP, "blocked_stuck")
                return StageOutcome(Disposition.FAIL_BACK, "blocked_exhausted")
            item.state = "BLOCKED_ADDRESS_READY"
            return Continue("BLOCKED_ADDRESS_READY")
        # PENDING: park with exponential backoff, honor wall-clock deadline.
        max_wait = read_timeout_env("HEPH_PR_MERGE_MAX_WAIT", 1800)
        delay = min(2 ** item.attempts.get("merge_poll", 0), _BACKOFF_CAP)
        elapsed = item.attempts.get("merge_elapsed", 0)
        if elapsed + delay > max_wait:
            return StageOutcome(Disposition.FINISH_FAIL, "timeout")
        item.attempts["merge_poll"] = item.attempts.get("merge_poll", 0) + 1
        item.attempts["merge_elapsed"] = elapsed + delay
        return JobRequest(None, "POLL")  # type: ignore

    def _request_dirty_resolve(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 3 DIRTY [W:G]+[W:A]: re-housed ``_resolve_dirty_pr`` (cheap path).

        An armed-but-DIRTY (merge-conflict) PR can never merge while armed, so
        this dispatches the cheap deterministic path of the legacy
        ``ci_driver._resolve_dirty_pr``: a mechanical rebase of the PR head
        worktree onto ``origin/<base_branch>`` (``git_utils.rebase_worktree_onto``
        via the worker pool's ``op="rebase"`` handler). ``base_branch`` was
        captured from the PR's ``baseRefName`` at POLL (defaulting to ``main``).
        On completion the coordinator advances to POLL, which re-classifies:
        a clean rebase pushes and re-arms; a still-DIRTY state re-enters here
        until the ``rebase`` budget is exhausted (then POLL routes ``timeout``).
        The agent-driven conflict-resolution fallback (legacy step 2) is
        dispatched by ``on_job_done`` routing when the mechanical rebase fails.
        """
        item.attempts["rebase"] = item.attempts.get("rebase", 0) + 1
        rebase_job = GitJob(
            repo=item.repo,
            op="rebase",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={
                "cwd": _worktree_path(item, ctx),
                "base_branch": str(item.payload.get("base_branch") or "main"),
            },
            descr="resolve_dirty_pr",
        )
        return JobRequest(rebase_job, on_done_state="POLL")

    def _request_blocked_address(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 3 BLOCKED [W:A]: address unresolved threads.

        An armed PR sitting in a BLOCKED branch-protection state (unresolved
        review threads gating the merge) dispatches the address-review agent
        session, whose prompt is built in-worker by
        :func:`..address_review.get_address_review_prompt` (the same builder the
        pr_review existing-PR address leg uses). The unresolved-thread JSON /
        difficulty todo block are seeded into ``item.payload`` by the
        coordinator (#1817), which owns the gh reads. On completion the item
        returns to POLL, which re-classifies the PR: threads resolved by the
        address turn clear the BLOCKED state and the armed PR merges; a still
        BLOCKED state re-enters here until the ``blocked_address`` budget is
        exhausted (then POLL routes it stuck/fail-back).
        """
        item.attempts["blocked_address"] = item.attempts.get("blocked_address", 0) + 1
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
                "worktree_path": str(_worktree_path(item, ctx)),
                "threads_json": item.payload.get("threads_json", "[]"),
                "todo_block": item.payload.get("difficulty_tiers", ""),
            },
            descr="merge_wait_address",
        )
        return JobRequest(job, on_done_state="POLL")

    def _request_learn(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 3 MERGED [W:A]: post-merge /learn, deduped.

        Dispatches the drive-green learnings agent session (re-housed
        ``post_merge_processor.run_drive_green_learnings``): the prompt is built
        in-worker by :func:`build_drive_green_learn_prompt`, scoped to what made
        CI fail and how it was fixed. On completion the item advances to
        ``LEARN_WAIT``, which finishes the drive successfully — the /learn step
        is best-effort and never flips a merged PR to failure.
        """
        job = AgentJob(
            repo=item.repo,
            issue=item.issue if item.issue is not None else 0,
            agent=AGENT_CI_DRIVER,
            model=implementer_model(),
            prompt_builder=build_drive_green_learn_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=learn_claude_timeout(),
            prompt_kwargs={
                "issue_number": item.issue if item.issue is not None else 0,
                "pr_number": item.pr,
            },
            descr="drive_green_learn",
        )
        return JobRequest(job, on_done_state="LEARN_WAIT")

    def _finish_after_learn(self, item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Step 4 [M]: terminal success after the post-merge /learn job.

        The PR has already MERGED (POLL routed here via LEARN_READY), so the
        drive is a success regardless of the /learn outcome — the learnings
        session is best-effort (``run_drive_green_learnings`` swallows any
        failure so a flaky learnings step never flips a successful drive). A
        failed learn job is recorded by ``on_job_done`` for observability only.
        """
        return StageOutcome(Disposition.FINISH_PASS, "merged")

    def on_job_done(self, item: WorkItem, result: Any, ctx: StageContext) -> None:
        """Route a completed worker job back into the merge-wait state machine.

        Called with ``item.state`` still at the WAIT/READY state that submitted
        the job (the coordinator contract, :mod:`.base`): the coordinator sets
        ``item.state = on_done_state`` *after* this returns, so ``on_job_done``
        never routes by writing ``item.state``. The submitting state is the
        job-kind discriminator:

        - ``resolve_dirty_pr`` (state ``DIRTY_REBASE_READY``,
          ``on_done_state="POLL"``): best-effort — nothing recorded; POLL
          re-classifies unconditionally.
        - ``merge_wait_address`` (state ``BLOCKED_ADDRESS_READY``,
          ``on_done_state="POLL"``): best-effort — nothing recorded, POLL
          re-reads the PR state and re-classifies unconditionally
          (``classify_pr_merge_state`` decides BLOCKED/PENDING/MERGED from the
          live thread/check state; threads still open keep it BLOCKED until the
          ``blocked_address`` budget is exhausted). A failed address turn is
          logged for observability so a silently-failing session is visible,
          but it never short-circuits the re-classification.
        - ``drive_green_learn`` (state ``LEARN_READY``,
          ``on_done_state="LEARN_WAIT"``): best-effort /learn; a failure is
          logged for observability but never fails the drive (the PR merged) —
          ``LEARN_WAIT`` finishes the item ``FINISH_PASS`` regardless.
        """
        if item.state == "BLOCKED_ADDRESS_READY" and not result.ok:
            # Best-effort address turn: POLL re-classifies unconditionally, so
            # a failure only needs surfacing for observability (mirrors the
            # ci.py best-effort sibling; classify_pr_merge_state re-reads the
            # threads and keeps the PR BLOCKED until the budget is exhausted).
            logger.warning(
                "merge_wait:%s: BLOCKED address turn failed (non-fatal, re-polling): %s",
                item.issue,
                result.error,
            )
        elif item.state == "LEARN_READY" and not result.ok:
            logger.warning(
                "merge_wait:%s: post-merge /learn failed (non-fatal): %s",
                item.issue,
                result.error,
            )
