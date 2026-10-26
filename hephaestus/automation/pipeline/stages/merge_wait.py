"""Merge-wait: arm native auto-merge for a GO pull request and let GitHub merge it.

``pr_review`` applies ``state:implementation-go`` when no review thread is
unresolved. This stage arms GitHub native auto-merge for an open pull request
that carries the label, unless an agent already armed it. GitHub owns
everything after that: branch protection, the required checks, any merge
queue, and a rebase when the merge reports a real conflict. The stage compares
no head, checks no status evidence, and does not rebase before arming. It
polls the pull request, with a bounded wait, until GitHub reports it merged or
closed.

The implemented mini-state graph is:

- ``ENTER -> MERGE``. An open PR with the PR-level implementation-GO label is
  armed once. The stage then polls with a bounded backoff.
- A PR observed as already merged, or confirmed as merged, emits one
  immutable post-merge learning intent. The coordinator transfers that intent
  to the auxiliary lane after the confirmed pass.
- A PR that lost its GO label fails back to PR review.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hephaestus.automation.issue_waves import (
    WAVE_LEASE_PAYLOAD,
    IssueWaveError,
    IssueWaveStore,
    WaveLease,
)
from hephaestus.automation.learning_journal import LearningJournalStore

from ..github_jobs import (
    GitHubJob,
    MergeWaitCycleCompleted,
    RunMergeWaitCycleRequest,
)
from ..work_item import LearningIntent
from .base import (
    Continue,
    Disposition,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageName,
    StageOutcome,
    StepResult,
    WorkItem,
    _repo_state_root,
)

logger = logging.getLogger(__name__)

ENTER = "ENTER"
MERGE = "MERGE"
MERGE_APPLY = "MERGE_APPLY"
MW_FINISH = "MW_FINISH"
FINISH = MW_FINISH

_POLL_INITIAL_S = 5.0
_POLL_TIMEOUT_S = 30 * 60.0
_POLL_DELAY_CAP_S = 60.0
_MERGE_CYCLE_OPERATION_TIMEOUT_S = 120.0
_PENDING_GITHUB_REQUEST = "_pending_github_request"
_MERGE_CYCLE_DEADLINE_S = "_merge_cycle_deadline_s"
_MERGE_CYCLE_RECEIPT = "_merge_wait_cycle_receipt"
_MERGE_CYCLE_RECEIPT_ERROR = "_merge_wait_cycle_receipt_error"
_AUTO_MERGE_ARMED = "merge_auto_merge_armed"


class MergeWaitStage(Stage):
    """Arm native auto-merge after the GO label and wait for GitHub to merge."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Reject an unscoped PR without touching any GitHub state."""
        del ctx
        if item.issue is None:
            logger.warning(
                "merge_wait: PR #%s has no requirements context; operator action required", item.pr
            )
            return StageOutcome(Disposition.FINISH_FAIL, "merge_wait_orphan")
        if not item.state:
            item.state = ENTER
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the merge-wait state machine."""
        if item.state == ENTER:
            return Continue(next_state=MERGE)
        if item.state == MERGE:
            return self._merge(item, ctx)
        if item.state == MERGE_APPLY:
            return self._merge_apply(item, ctx)
        if item.state == MW_FINISH:
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        logger.warning("merge_wait:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _merge(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dispatch one auto-merge enablement cycle.

        The pull request number and the approval label are the whole contract.
        No head is compared, so a new commit between review and merge does not
        restart this stage.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.attempts["merge"] >= ctx.budget("merge"):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
        operation_deadline = item.payload.get(_MERGE_CYCLE_DEADLINE_S)
        if operation_deadline is None:
            operation_deadline = ctx.now() + _MERGE_CYCLE_OPERATION_TIMEOUT_S
            item.payload[_MERGE_CYCLE_DEADLINE_S] = operation_deadline
        request = RunMergeWaitCycleRequest(
            issue_number=item.issue,
            pr_number=item.pr,
            deadline_s=operation_deadline,
            cancellation=ctx.cancellation,
            auto_merge_armed=bool(item.payload.get(_AUTO_MERGE_ARMED)),
        )
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        if pending is None:
            item.payload[_PENDING_GITHUB_REQUEST] = request
        elif pending != request:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_cycle_receipt_invalid")
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)).resolve(),
                request=request,
                descr="merge_wait_admission_cycle",
            ),
            on_done_state=MERGE_APPLY,
        )

    def _merge_apply(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901
        """Apply one correlated immutable merge-cycle receipt locally."""
        error = item.payload.pop(_MERGE_CYCLE_RECEIPT_ERROR, None)
        if error is not None:
            item.payload.pop(_PENDING_GITHUB_REQUEST, None)
            item.payload.pop(_MERGE_CYCLE_DEADLINE_S, None)
            return StageOutcome(Disposition.FINISH_FAIL, "merge_cycle_failed")
        receipt = item.payload.pop(_MERGE_CYCLE_RECEIPT, None)
        if not isinstance(receipt, MergeWaitCycleCompleted) or receipt.request != item.payload.get(
            _PENDING_GITHUB_REQUEST
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_cycle_receipt_invalid")
        item.payload.pop(_PENDING_GITHUB_REQUEST, None)
        item.payload.pop(_MERGE_CYCLE_DEADLINE_S, None)
        if receipt.attempted:
            item.attempts["merge"] += 1
        outcome = receipt.outcome
        if outcome == "merged":
            lease = item.payload.get(WAVE_LEASE_PAYLOAD)
            if lease is not None:
                if (
                    not isinstance(lease, WaveLease)
                    or item.issue is None
                    or item.pr is None
                    or receipt.merge_sha is None
                ):
                    return StageOutcome(Disposition.FINISH_FAIL, "wave_merge_receipt_missing")
                try:
                    item.payload[WAVE_LEASE_PAYLOAD] = IssueWaveStore(
                        _repo_state_root(ctx, item.repo), ctx.org, item.repo
                    ).record_merge_receipt(
                        lease,
                        issue_number=item.issue,
                        pr_number=item.pr,
                        merge_sha=receipt.merge_sha,
                    )
                except IssueWaveError as exc:
                    return StageOutcome(
                        Disposition.FINISH_FAIL, f"wave_merge_receipt_failed: {exc}"
                    )
            return self._route_merged(item, ctx)
        if outcome == "closed":
            return StageOutcome(Disposition.FINISH_FAIL, "closed")
        if outcome == "auto_merge_enabled":
            # GitHub owns the merge from here. Wait for the terminal result.
            item.payload[_AUTO_MERGE_ARMED] = True
            item.state = MERGE
            return self._poll(item, ctx)
        if outcome == "not_implementation_go":
            item.payload.pop(_AUTO_MERGE_ARMED, None)
            return StageOutcome(Disposition.FAIL_BACK, outcome)
        if outcome == "merge_conflicting":
            return self._post_review_rebase(item, outcome)
        if (
            outcome
            in {
                "auto_merge_transport_error",
                "auto_merge_not_enabled",
                "auto_merge_malformed",
                "auto_merge_identifier_unavailable",
            }
            and receipt.retryable
        ):
            item.state = MERGE
            return self._retry(item, ctx)
        return StageOutcome(Disposition.FINISH_FAIL, outcome)

    @staticmethod
    def _retry(item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Timer-park a bounded retry without issuing another merge in this step."""
        if item.attempts["merge"] >= ctx.budget("merge"):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
        item.payload["retry_delay_s"] = float(min(2 ** max(0, item.attempts["merge"] - 1), 60))
        return StageOutcome(Disposition.RETRY, "merge_not_ready")

    @staticmethod
    def _post_review_rebase(item: WorkItem, reason: str) -> StageOutcome:
        """Send a merge conflict to the implementation stage."""
        item.payload["post_review_rebase_required"] = True
        item.payload["rebase_reason"] = "review_conflict"
        return StageOutcome(Disposition.FAIL_BACK, reason)

    @staticmethod
    def _poll(item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Wait on the timer heap for GitHub to merge, within a bounded time."""
        deadline = item.payload.get("merge_poll_deadline_s")
        polls = item.payload.get("merge_poll_count", 0)
        now = ctx.now()
        if deadline is None:
            poll_max_wait = getattr(ctx.config, "poll_max_wait", _POLL_TIMEOUT_S)
            deadline = now + int(poll_max_wait)
            item.payload["merge_poll_deadline_s"] = deadline
        if now >= deadline:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_poll_timeout")
        delay = min(_POLL_INITIAL_S * (2**polls), _POLL_DELAY_CAP_S, deadline - now)
        item.payload["merge_poll_count"] = polls + 1
        item.payload["retry_delay_s"] = delay
        return StageOutcome(Disposition.RETRY, "merge_wait_poll")

    def _route_merged(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Record post-merge learning without changing confirmed merge success."""
        if item.issue is None or not ctx.config.enable_learn:
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "missing_learn_scope")
        intent = LearningIntent.post_merge(repo=item.repo, issue=item.issue, pr=item.pr)
        if intent not in item.learning_intents:
            item.learning_intents.append(intent)
        if isinstance(ctx.learning_journal, LearningJournalStore):
            try:
                ctx.learning_journal.ensure_pending(
                    intent.key,
                    kind=intent.kind.value,
                    identity=intent.journal_identity(),
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                logger.exception("merge_wait:%s: could not persist learning intent", item.issue)
                item.learning_intents.remove(intent)
                item.payload.setdefault("learning_failures", []).append(
                    {"key": intent.key, "error": "learning_intent_persist_failed"}
                )
        item.learning_resume_stage = StageName.FINISHED
        return StageOutcome(Disposition.FINISH_PASS, "merged")

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store one immutable merge-cycle receipt."""
        if item.state == MERGE:
            if not result.ok:
                item.payload[_MERGE_CYCLE_RECEIPT_ERROR] = result.error or "merge cycle failed"
                return
            receipt = result.value
            if not isinstance(
                receipt, MergeWaitCycleCompleted
            ) or receipt.request != item.payload.get(_PENDING_GITHUB_REQUEST):
                item.payload[_MERGE_CYCLE_RECEIPT_ERROR] = "invalid"
                return
            item.payload[_MERGE_CYCLE_RECEIPT] = receipt
            return
