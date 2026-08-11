"""Durable learning-intent recovery for coordinator source items."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.arming_state import LearningJournalStore

from .coordinator_contract import _CoordinatorHost
from .coordinator_types import *
from .work_item import LearningIntent

# This collaborator consumes the facade's shared type namespace by design.
# ruff: noqa: F403, F405


class LearningRecoveryCoordinator(_CoordinatorHost):
    """Restore durable learning work before an item's primary route."""

    def _restore_learning_intents(
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
        if not self.config.enable_learn:
            for record in records:
                journal.disable(str(record["key"]))
            return
        restored: list[LearningIntent] = []
        for record in records:
            try:
                restored.append(LearningIntent.from_journal(record))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "learning:%s#%s: invalid journal identity disabled: %s",
                    item.repo,
                    item.issue,
                    exc,
                )
                journal.disable(str(record["key"]))
        if not restored:
            return
        item.learning_intents = restored
        item.learning_resume_stage = primary_stage
        if primary_stage is StageName.FINISHED:
            item.payload["_learning_primary_reason"] = primary_reason
            if item.result is not None:
                item.compact_for_post_processing(item.result)
        item.stage = StageName.LEARNING

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
