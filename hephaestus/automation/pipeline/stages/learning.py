"""Auxiliary learning stage with durable, ancillary outcomes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hephaestus.automation.agent_config import learn_claude_timeout
from hephaestus.automation.arming_state import LearningJournalStore
from hephaestus.automation.mnemosyne_delivery import valid_delivery_receipt
from hephaestus.automation.state_labels import STATE_PLAN_GO, is_exclusive_plan_state

from ..athena_skill_jobs import AthenaSkillJob, AthenaSkillRequest, AthenaSkillResult
from ..job_results import JobResult
from ..routing import Disposition, StageName, StageOutcome
from ..stage_results import Continue, JobRequest
from ..work_item import LearningIntent, WorkItem

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

        payload: dict[str, object] = {
            "issue_number": intent.issue,
            "intent_key": intent.key,
            "intent_kind": intent.kind.value,
        }
        delivery = item.payload.get("learn_delivery")
        if isinstance(delivery, dict):
            payload["learn_delivery"] = dict(delivery)
        return JobRequest(
            AthenaSkillJob(
                request=AthenaSkillRequest(
                    kind="learn",
                    repo=item.repo,
                    issue=intent.issue,
                    agent=str(getattr(ctx.config, "agent", "") or "claude"),
                    model=str(
                        getattr(ctx.config, "implementer_model", "")
                        or getattr(ctx.config, "model", "")
                    ),
                    cwd=Path(item.worktree or str(ctx.paths.worktree)),
                    timeout_s=learn_claude_timeout(),
                    payload=payload,
                ),
                descr=f"auxiliary_learn_{intent.kind.value}",
            ),
            on_done_state=RESULT,
        )

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        """Record success or apply the bounded retry policy."""
        intent = self._claimed_intent(item, ctx)
        if intent is None:
            return
        succeeded = bool(
            result.ok
            and isinstance(result.value, AthenaSkillResult)
            and result.value.ok
            and valid_delivery_receipt(result.value.delivery_receipt)
        )
        error = "" if succeeded else (result.error or "invalid Athena learn result")
        receipt = result.value.delivery_receipt if succeeded else None
        journal = self._journal(ctx)
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
        if not succeeded:
            item.payload.setdefault("learning_failures", []).append(
                {"key": intent.key, "error": error[:1000]}
            )

    def on_cancelled_before_start(self, item: WorkItem, ctx: Any) -> None:
        """Return a claim to pending when the host provably did not run."""
        intent = self._claimed_intent(item, ctx)
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
        for intent in item.learning_intents:
            record = journal.load(intent.key)
            if record is None or record["status"] not in {"succeeded", "failed"}:
                return intent
        return None

    def _claimed_intent(self, item: WorkItem, ctx: Any) -> LearningIntent | None:
        journal = self._journal(ctx)
        for intent in item.learning_intents:
            record = journal.load(intent.key)
            if record is not None and record["status"] == "claimed":
                return intent
        return None

    def _claim_or_skip(
        self,
        item: WorkItem,
        intent: LearningIntent,
        record: dict[str, Any],
        journal: LearningJournalStore,
        ctx: Any,
    ) -> Continue | None:
        """Handle terminal, ambiguous, and stale-plan records before claim."""
        if record["status"] == "claimed":
            journal.finish(intent.key, succeeded=False, error="outcome_unknown")
            item.payload.setdefault("learning_failures", []).append(
                {"key": intent.key, "error": "outcome_unknown"}
            )
            return Continue(next_state=CLAIM)
        if record["status"] in {"succeeded", "failed"}:
            return Continue(next_state=CLAIM)
        if intent.kind.value != "approved_plan":
            return None
        plan_state = self._approved_plan_state(intent, ctx)
        if plan_state is True:
            return None
        error = "plan_state_changed" if plan_state is False else "plan_state_unverified"
        if journal.claim(intent.key):
            journal.finish(intent.key, succeeded=False, error=error)
        if plan_state is False:
            item.learning_resume_stage = StageName.PLAN_REVIEW
        item.payload.setdefault("learning_failures", []).append({"key": intent.key, "error": error})
        return Continue(next_state=CLAIM)

    @staticmethod
    def _approved_plan_state(intent: LearningIntent, ctx: Any) -> bool | None:
        """Return live approval, confirmed change, or an unavailable read."""
        try:
            issue = ctx.github.gh_issue_json(intent.issue)
        except Exception:
            return None
        labels = [
            str(label.get("name", ""))
            for label in issue.get("labels", [])
            if isinstance(label, dict)
        ]
        return is_exclusive_plan_state(labels, STATE_PLAN_GO)
