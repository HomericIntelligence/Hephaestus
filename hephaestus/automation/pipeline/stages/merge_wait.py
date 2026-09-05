"""Merge-wait: final head-bound admission and one conditional normal merge.

``pr_review`` owns the loop's automated implementation-eligibility decision.
This stage may perform one ordinary REST squash merge only after it observes
the exact active-run reviewed head, an exclusive implementation-GO label, an
open ``main`` PR, an explicitly absent auto-merge request, no unresolved
review threads, and passing required status evidence for that head. It never
enables, disables, adopts, or polls native auto-merge.

The implemented mini-state graph is:

- ``ENTER -> MERGE``. An open PR is admitted only with the current-process
  reviewed-head proof and the PR-level implementation-GO label. Readiness may
  retry ``MERGE`` within its bounded wait.
- A PR observed as already merged, or a conditional merge freshly confirmed as
  merged, emits one immutable post-merge learning intent. The coordinator
  transfers that intent to the auxiliary lane after the confirmed pass.
- Live admission, readiness, and conditional-merge failures exit safely or
  fail back to fresh PR review when the head-bound proof is absent or stale.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hephaestus.automation.arming_state import LearningJournalStore
from hephaestus.automation.issue_waves import (
    WAVE_LEASE_PAYLOAD,
    IssueWaveError,
    IssueWaveStore,
    WaveLease,
)

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
)

logger = logging.getLogger(__name__)

ENTER = "ENTER"
MERGE = "MERGE"
MERGE_APPLY = "MERGE_APPLY"
MW_FINISH = "MW_FINISH"
FINISH = MW_FINISH

_READINESS_WAIT_INITIAL_S = 5.0
_READINESS_WAIT_TIMEOUT_S = 30 * 60.0
_READINESS_WAIT_DELAY_CAP_S = 60.0
_MERGE_CYCLE_OPERATION_TIMEOUT_S = 120.0
_DECLINED_READINESS_FINGERPRINT = "merge_readiness_declined_fingerprint"
_PENDING_GITHUB_REQUEST = "_pending_github_request"
_MERGE_CYCLE_DEADLINE_S = "_merge_cycle_deadline_s"
_MERGE_CYCLE_RECEIPT = "_merge_wait_cycle_receipt"
_MERGE_CYCLE_RECEIPT_ERROR = "_merge_wait_cycle_receipt_error"


class MergeWaitStage(Stage):
    """Attempt a bounded SHA-conditional merge after final live admission."""

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
        """Freeze one exact proof and dispatch the complete GitHub cycle."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.attempts["merge"] >= ctx.budget("merge"):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
        reviewed_head = item.payload.get("reviewed_pr_head_sha")
        if not isinstance(reviewed_head, str) or not reviewed_head:
            return StageOutcome(Disposition.FAIL_BACK, "reviewed_head_missing")
        deadline = self._matching_readiness_deadline_outcome(item, ctx)
        if deadline is not None:
            return deadline
        proof_generation = item.payload.get("reviewed_pr_proof_generation", 0)
        declined = item.payload.get(_DECLINED_READINESS_FINGERPRINT)
        if (
            isinstance(proof_generation, bool)
            or not isinstance(proof_generation, int)
            or proof_generation < 0
            or (
                declined is not None
                and (
                    not isinstance(declined, (list, tuple))
                    or not all(isinstance(part, str) for part in declined)
                )
            )
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_state_invalid")
        try:
            operation_deadline = item.payload.get(_MERGE_CYCLE_DEADLINE_S)
            if operation_deadline is None:
                operation_deadline = ctx.now() + _MERGE_CYCLE_OPERATION_TIMEOUT_S
                item.payload[_MERGE_CYCLE_DEADLINE_S] = operation_deadline
            request = RunMergeWaitCycleRequest(
                issue_number=item.issue,
                pr_number=item.pr,
                reviewed_head_sha=reviewed_head,
                proof_generation=proof_generation,
                declined_readiness_fingerprint=(tuple(declined) if declined is not None else None),
                deadline_s=operation_deadline,
                cancellation=ctx.cancellation,
            )
        except ValueError:
            return StageOutcome(Disposition.FAIL_BACK, "reviewed_head_missing")
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
        if receipt.readiness_fingerprint is not None:
            item.payload[_DECLINED_READINESS_FINGERPRINT] = list(receipt.readiness_fingerprint)
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
                        Path(str(ctx.paths.repo_root)), ctx.org, item.repo
                    ).record_merge_receipt(
                        lease,
                        issue_number=item.issue,
                        pr_number=item.pr,
                        reviewed_head_sha=receipt.request.reviewed_head_sha,
                        merge_sha=receipt.merge_sha,
                    )
                except IssueWaveError as exc:
                    return StageOutcome(
                        Disposition.FINISH_FAIL, f"wave_merge_receipt_failed: {exc}"
                    )
            return self._route_merged(item, ctx)
        if outcome == "closed":
            return StageOutcome(Disposition.FINISH_FAIL, "closed")
        if outcome == "auto_merge_already_armed":
            return StageOutcome(Disposition.BLOCKED, outcome)
        if outcome == "required_checks_not_green":
            return StageOutcome(Disposition.BLOCKED, outcome)
        if outcome in {"not_implementation_go", "reviewed_head_drift"}:
            return StageOutcome(Disposition.FAIL_BACK, outcome)
        if outcome in {"merge_conflicting", "post_review_rebase_required"}:
            return self._post_review_rebase(item, outcome)
        if outcome == "readiness_wait":
            if receipt.attempted and item.attempts["merge"] >= ctx.budget("merge"):
                return StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
            item.state = MERGE
            return self._park_for_readiness(item, ctx)
        if outcome in {"merge_not_ready", "merge_request_transport_error"} and receipt.retryable:
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
        """Send a reviewed stale/conflicting head to implementation ownership."""
        item.payload["post_review_rebase_required"] = True
        return StageOutcome(Disposition.FAIL_BACK, reason)

    @staticmethod
    def _matching_readiness_deadline_outcome(
        item: WorkItem, ctx: StageContext
    ) -> StageOutcome | None:
        """Fail closed when an existing matching readiness wait has already expired."""
        reviewed_head = item.payload.get("reviewed_pr_head_sha")
        if not isinstance(reviewed_head, str) or not reviewed_head:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_state_invalid")
        proof_generation = item.payload.get("reviewed_pr_proof_generation", 0)
        if isinstance(proof_generation, bool) or not isinstance(proof_generation, int):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_state_invalid")
        if (
            item.payload.get("merge_readiness_head_sha") != reviewed_head
            or item.payload.get("merge_readiness_proof_generation") != proof_generation
        ):
            return None
        deadline = item.payload.get("merge_readiness_deadline_s")
        if isinstance(deadline, bool) or (
            deadline is not None and not isinstance(deadline, (int, float))
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_state_invalid")
        if deadline is None:
            return None
        if ctx.now() >= deadline:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_timeout")
        return None

    @staticmethod
    def _park_for_readiness(item: WorkItem, ctx: StageContext) -> StageOutcome:
        """Record one bounded non-mutating readiness wait on the timer heap."""
        reviewed_head = item.payload.get("reviewed_pr_head_sha")
        if not isinstance(reviewed_head, str) or not reviewed_head:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_state_invalid")
        proof_generation = item.payload.get("reviewed_pr_proof_generation", 0)
        if isinstance(proof_generation, bool) or not isinstance(proof_generation, int):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_state_invalid")
        if (
            item.payload.get("merge_readiness_head_sha") != reviewed_head
            or item.payload.get("merge_readiness_proof_generation") != proof_generation
        ):
            # A fresh review creates a new process-local proof. Do not carry a
            # prior proof's operational waiting deadline onto that new proof.
            item.payload.pop("merge_readiness_deadline_s", None)
            item.payload.pop("merge_readiness_polls", None)
            item.payload["merge_readiness_head_sha"] = reviewed_head
            item.payload["merge_readiness_proof_generation"] = proof_generation

        deadline = item.payload.get("merge_readiness_deadline_s")
        polls = item.payload.get("merge_readiness_polls", 0)
        now = ctx.now()
        if (
            isinstance(deadline, bool)
            or (deadline is not None and not isinstance(deadline, (int, float)))
            or isinstance(polls, bool)
            or not isinstance(polls, int)
            or polls < 0
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_state_invalid")
        if deadline is None:
            poll_max_wait = getattr(ctx.config, "poll_max_wait", _READINESS_WAIT_TIMEOUT_S)
            deadline = now + int(poll_max_wait)
            item.payload["merge_readiness_deadline_s"] = deadline
        if now >= deadline:
            return StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_timeout")
        delay = min(
            _READINESS_WAIT_INITIAL_S * (2**polls),
            _READINESS_WAIT_DELAY_CAP_S,
            deadline - now,
        )
        item.payload["merge_readiness_polls"] = polls + 1
        item.payload["retry_delay_s"] = delay
        return StageOutcome(Disposition.RETRY, "merge_readiness_wait")

    def _route_merged(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Record post-merge learning without changing confirmed merge success."""
        if item.issue is None or not ctx.config.enable_learn:
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if ctx.github.drive_green_learn_terminal(item.issue):
            return StageOutcome(Disposition.FINISH_PASS, "merged")
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "missing_learn_scope")
        intent = LearningIntent.post_merge(repo=item.repo, issue=item.issue, pr=item.pr)
        if intent not in item.learning_intents:
            item.learning_intents.append(intent)
        if isinstance(ctx.learning_journal, LearningJournalStore):
            try:
                record = ctx.learning_journal.ensure_pending(
                    intent.key,
                    kind=intent.kind.value,
                    identity=intent.journal_identity(),
                )
                if (
                    ctx.github.drive_green_learn_inflight(item.issue)
                    and record["status"] == "pending"
                    and ctx.learning_journal.claim(intent.key)
                ):
                    ctx.learning_journal.finish(
                        intent.key,
                        succeeded=False,
                        error="legacy_outcome_unknown",
                    )
                    item.payload.setdefault("learning_failures", []).append(
                        {"key": intent.key, "error": "legacy_outcome_unknown"}
                    )
            except (OSError, RuntimeError, TypeError, ValueError):
                logger.exception("merge_wait:%s: could not persist learning intent", item.issue)
                item.learning_intents.remove(intent)
                item.payload.setdefault("learning_failures", []).append(
                    {"key": intent.key, "error": "learning_intent_persist_failed"}
                )
        elif ctx.github.drive_green_learn_inflight(item.issue):
            item.learning_intents.remove(intent)
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
