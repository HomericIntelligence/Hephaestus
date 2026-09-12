"""Auxiliary learning stage with durable, ancillary outcomes."""

from __future__ import annotations

from typing import Any

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.agent_config import learn_claude_timeout, learn_model
from hephaestus.automation.learning_journal import LearningJournalStore
from hephaestus.automation.mnemosyne_delivery import valid_delivery_receipt

from ..athena_skill_jobs import AthenaSkillJob, AthenaSkillRequest, AthenaSkillResult
from ..job_results import JobResult
from ..routing import Disposition, StageName, StageOutcome
from ..stage_results import Continue, JobRequest
from ..summary import record_summary_action
from ..work_item import LearningIntent, WorkItem
from .base import source_workspace_binding, stage_timeout

StepResult = Continue | JobRequest | StageOutcome

ENTER = "ENTER"
CLAIM = "CLAIM"
RESULT = "RESULT"


class LearningStage:
    """Claim and execute learning intents outside the main worker lane."""

    kind = StageName.LEARNING

    def on_enter(self, item: WorkItem, ctx: Any) -> StageOutcome | None:
        """Persist all in-memory intents before any host call."""
        journal = self._journal(ctx)
        for intent in item.learning_intents:
            record = journal.load(intent.key)
            if record is not None and record["status"] in {"succeeded", "failed", "deferred"}:
                continue
            journal.ensure_pending(
                intent.key,
                kind=intent.kind.value,
                identity=intent.journal_identity(),
            )
        if not item.state:
            item.state = ENTER
        return None

    def step(self, item: WorkItem, ctx: Any) -> StepResult:
        """Run one durable intent at a time."""
        if item.state == ENTER:
            return Continue(next_state=CLAIM)
        if item.state == RESULT:
            return Continue(next_state=CLAIM)
        if item.state != CLAIM:
            return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

        intent = self._next_intent(item, ctx)
        if intent is None:
            if item.learning_resume_stage is StageName.IMPLEMENTATION:
                return StageOutcome(Disposition.FAIL_BACK, "resume_implementation")
            if item.learning_resume_stage is StageName.PLAN_REVIEW:
                return StageOutcome(Disposition.FAIL_BACK, "resume_plan_review")
            return StageOutcome(Disposition.ADVANCE, "learning terminal")

        journal = self._journal(ctx)
        record = journal.load(intent.key)
        if record is None:
            record = journal.ensure_pending(intent.key, kind=intent.kind.value)
        skip = self._claim_or_skip(item, intent, record, journal, ctx)
        if skip is not None:
            return skip
        if not journal.claim(intent.key):
            return Continue(next_state=CLAIM)
        item.payload["_learning_claimed_intent_key"] = intent.key

        try:
            delivery_payload = intent.to_payload(owner=ctx.org)
        except ValueError:
            error = "learning_repository_identity_rejected"
            journal.finish(intent.key, succeeded=False, error=error)
            item.payload.pop("_learning_claimed_intent_key", None)
            item.payload.setdefault("learning_failures", []).append(
                {"key": intent.key, "error": error}
            )
            record_summary_action(item, error)
            return Continue(next_state=CLAIM)

        return self._prepare_host_job(item, ctx, intent, delivery_payload)

    def _prepare_host_job(
        self,
        item: WorkItem,
        ctx: Any,
        intent: LearningIntent,
        delivery_payload: dict[str, object],
    ) -> StepResult:
        """Prepare one claimed host job or release its known failed claim."""
        payload: dict[str, object] = {
            "issue_number": intent.issue,
            "learning_intent": delivery_payload,
        }
        revision = str(
            item.payload.get("_worktree_cleanup_head_sha")
            or item.payload.get("_impl_source_revision")
            or item.payload.get("_synced_default_branch_sha")
            or item.payload.get("_direct_scope_base_sha")
            or ""
        )
        try:
            workspace = source_workspace_binding(
                item,
                ctx,
                SourceLane.REVIEW,
                revision=revision or None,
                branch=None,
            )
        except InterruptedError:
            self.on_cancelled_before_start(item, ctx)
            return StageOutcome(Disposition.RETRY, "learning preparation interrupted")
        except AttributeError as exc:
            self.on_job_done(
                item,
                JobResult(ok=False, error=f"learning source preparation failed: {exc}"),
                ctx,
            )
            return Continue(next_state=CLAIM)
        except RuntimeError as exc:
            error = (
                "learning_source_preparation_unavailable"
                if str(exc) == "source workspace manager is required"
                else f"learning source preparation failed: {exc}"
            )
            self.on_job_done(
                item,
                JobResult(ok=False, error=error),
                ctx,
            )
            return Continue(next_state=CLAIM)
        return JobRequest(
            AthenaSkillJob(
                request=AthenaSkillRequest(
                    kind="learn",
                    repo=item.repo,
                    issue=intent.issue,
                    agent=str(getattr(ctx.config, "agent", "") or "claude"),
                    model=str(
                        getattr(ctx.config, "learn_model", "")
                        or getattr(ctx.config, "model", "")
                        or learn_model()
                    ),
                    cwd=workspace.cwd,
                    timeout_s=stage_timeout(ctx, "learn", learn_claude_timeout),
                    workspace=workspace,
                    payload=payload,
                ),
                descr=f"auxiliary_learn_{intent.kind.value}",
            ),
            on_done_state=RESULT,
        )

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        """Record success or apply the bounded retry policy."""
        intent = self._locally_claimed_intent(item)
        if intent is None:
            return
        succeeded = bool(
            isinstance(result.value, AthenaSkillResult)
            and result.value.ok
            and valid_delivery_receipt(result.value.delivery_receipt)
        )
        cleanup_failed = bool(
            result.error and result.error.startswith("learning_workspace_cleanup_failed:")
        )
        error = (
            "learning_workspace_cleanup_failed"
            if succeeded and cleanup_failed
            else ""
            if succeeded
            else (result.error or "invalid Athena learn result")
        )
        receipt = result.value.delivery_receipt if succeeded else None
        journal = self._journal(ctx)
        if not succeeded and error.startswith("learning_deferred:"):
            journal.defer(intent.key, error=error)
            item.payload.setdefault("learning_deferred", []).append(
                {"key": intent.key, "reason": error}
            )
            record_summary_action(item, error)
            return
        record = journal.load(intent.key)
        attempts = int(record.get("attempts", 0)) if record is not None else 0
        if not succeeded and attempts < ctx.budget("learn"):
            journal.retry(intent.key, error=error)
            return
        journal.finish(
            intent.key,
            succeeded=succeeded,
            error=error,
            receipt_summary=(
                {"pr_number": receipt.get("pr_number"), "pr_url": receipt.get("pr_url")}
                if isinstance(receipt, dict)
                else None
            ),
        )
        if succeeded and cleanup_failed:
            item.payload.setdefault("learning_failures", []).append(
                {"key": intent.key, "error": error}
            )
            return
        if not succeeded:
            item.payload.setdefault("learning_failures", []).append(
                {"key": intent.key, "error": error[:1000]}
            )

    def on_cancelled_before_start(self, item: WorkItem, ctx: Any) -> None:
        """Return a claim to pending when the host provably did not run."""
        intent = self._locally_claimed_intent(item)
        if intent is not None:
            self._journal(ctx).retry(intent.key, error="interrupted_before_start")

    @staticmethod
    def _journal(ctx: Any) -> LearningJournalStore:
        journal = ctx.learning_journal
        if not isinstance(journal, LearningJournalStore):
            raise RuntimeError("learning stage requires a LearningJournalStore")
        return journal

    def _next_intent(self, item: WorkItem, ctx: Any) -> LearningIntent | None:
        journal = self._journal(ctx)
        external_claims = set(item.payload.get("learning_external_claims", []))
        for intent in item.learning_intents:
            if intent.key in external_claims:
                continue
            record = journal.load(intent.key)
            if record is None or record["status"] not in {"succeeded", "failed", "deferred"}:
                return intent
        return None

    @staticmethod
    def _locally_claimed_intent(item: WorkItem) -> LearningIntent | None:
        key = item.payload.pop("_learning_claimed_intent_key", None)
        if not isinstance(key, str):
            return None
        return next((intent for intent in item.learning_intents if intent.key == key), None)

    def _claim_or_skip(
        self,
        item: WorkItem,
        intent: LearningIntent,
        record: dict[str, Any],
        journal: LearningJournalStore,
        ctx: Any,
    ) -> Continue | StageOutcome | None:
        """Handle terminal records, active claims, and rejected plan intents."""
        if record["status"] == "claimed":
            if journal.claim_is_active(intent.key):
                return StageOutcome(
                    Disposition.EJECT,
                    "learning_claim_owned_elsewhere",
                )
            abandoned = journal.fail_abandoned_claim(intent.key, error="outcome_unknown")
            if abandoned is None:
                return StageOutcome(
                    Disposition.EJECT,
                    "learning_claim_owned_elsewhere",
                )
            if abandoned.get("status") == "failed" and abandoned.get("error") == "outcome_unknown":
                item.payload.setdefault("learning_failures", []).append(
                    {"key": intent.key, "error": "outcome_unknown"}
                )
            return Continue(next_state=CLAIM)
        if record["status"] in {"succeeded", "failed", "deferred"}:
            return Continue(next_state=CLAIM)
        if intent.kind.value != "approved_plan":
            return None
        error = "plan_only_learning_rejected"
        if journal.claim(intent.key):
            journal.finish(intent.key, succeeded=False, error=error)
        item.payload.setdefault("learning_failures", []).append({"key": intent.key, "error": error})
        record_summary_action(item, error)
        return Continue(next_state=CLAIM)
