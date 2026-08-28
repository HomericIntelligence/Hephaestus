"""Durable learning-intent recovery for coordinator source items."""

from __future__ import annotations

import logging

from hephaestus.automation.arming_state import LearningJournalStore

from .coordinator_types_ns import ct
from .coordinator_contract import _CoordinatorHost
from .work_item import LearningIntent

Any = ct.Any
ItemResult = ct.ItemResult
StageName = ct.StageName
WorkItem = ct.WorkItem

logger = logging.getLogger("hephaestus.automation.pipeline.coordinator")


class LearningRecoveryCoordinator(_CoordinatorHost):
    """Restore durable learning work before an item's primary route."""

    def _restore_learning_intents(  # noqa: C901 - recovery validates durable mixed states
        self,
        item: WorkItem,
        primary_stage: StageName | None,
        primary_reason: str,
    ) -> None:
        """Route durable nonterminal learning records before normal work."""
        if item.issue is None or primary_stage is None:
            return
        journal = self._ctx_for_repo(item.repo).learning_journal
        if not isinstance(journal, LearningJournalStore):
            return
        records = journal.incomplete_for_issue(repo=item.repo, issue=item.issue)
        if not records and primary_stage is StageName.FINISHED and item.pr is not None:
            records = self._adopt_legacy_post_merge_intent(item, journal)
        if not records:
            return
        restored: list[LearningIntent] = []
        for record in records:
            try:
                restored.append(LearningIntent.from_journal(record))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "learning:%s#%s: invalid journal identity ignored: %s",
                    item.repo,
                    item.issue,
                    exc,
                )
        if not restored:
            return
        item.learning_intents = restored
        if not self.config.enable_learn:
            self._disable_valid_records(records, journal)
            if primary_stage is StageName.FINISHED:
                terminal_record = next(
                    (record for record in records if "post_processing" in record),
                    records[0],
                )
                if "post_processing" in terminal_record and not self._restore_post_processing(
                    item, terminal_record
                ):
                    return
            item.learning_intents.clear()
            return
        item.learning_resume_stage = primary_stage
        if primary_stage is StageName.FINISHED:
            item.payload["_learning_primary_reason"] = primary_reason
            terminal_record = next(
                (record for record in records if "post_processing" in record),
                records[0],
            )
            restored_terminal = self._restore_post_processing(item, terminal_record)
            if item.result is not None and not item.result.passed:
                return
            if item.result is not None and not restored_terminal:
                item.compact_for_post_processing(item.result)
        item.stage = StageName.LEARNING

    @staticmethod
    def _restore_post_processing(item: WorkItem, record: dict[str, Any]) -> bool:
        """Restore one cleanup receipt or quarantine only its source item."""
        try:
            return item.restore_post_processing(record)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "learning:%s#%s: invalid post-processing state quarantined: %s",
                item.repo,
                item.issue,
                exc,
            )
            item.learning_intents = []
            item.learning_resume_stage = None
            item.post_processing = None
            item.result = ItemResult(
                passed=False,
                reason="invalid durable learning recovery state",
                final_stage=StageName.LEARNING,
            )
            item.stage = StageName.FINISHED
            return False

    @staticmethod
    def _disable_valid_records(
        records: list[dict[str, Any]], journal: LearningJournalStore
    ) -> bool:
        """Disable records that contain a usable deterministic key."""
        all_disabled = True
        for record in records:
            key = record.get("key")
            if isinstance(key, str) and key and journal.disable(key) is None:
                logger.info("learning:%s: active claim belongs to another loop", key)
                all_disabled = False
                continue
        return all_disabled

    def _adopt_legacy_post_merge_intent(
        self,
        item: WorkItem,
        journal: LearningJournalStore,
    ) -> list[dict[str, Any]]:
        """Convert a merged legacy learning state into the new journal once."""
        assert item.issue is not None and item.pr is not None  # noqa: S101
        github = self._ctx_for_repo(item.repo).github
        pr_state = github.gh_pr_state(item.pr) or {}
        if str(pr_state.get("state") or "").upper() != "MERGED" and not pr_state.get("mergedAt"):
            return []
        if github.drive_green_learn_terminal(item.issue):
            return []
        intent = LearningIntent.post_merge(repo=item.repo, issue=item.issue, pr=item.pr)
        record = journal.ensure_pending(
            intent.key,
            kind=intent.kind.value,
            identity=intent.journal_identity(),
        )
        if record["status"] in {"succeeded", "failed"}:
            return []
        if github.drive_green_learn_inflight(item.issue):
            if journal.claim(intent.key):
                journal.finish(
                    intent.key,
                    succeeded=False,
                    error="legacy_outcome_unknown",
                )
            return []
        return [record]
