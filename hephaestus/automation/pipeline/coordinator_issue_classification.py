"""Isolate repository issue classification failures from source cursors."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import hephaestus.automation.issue_waves as issue_waves_mod
import hephaestus.automation.pipeline.coordinator_types as ct
import hephaestus.automation.pipeline.seeding as _seeding
from hephaestus.automation.state_labels import STATE_PLAN_BLOCKED

from .coordinator_contract import _CoordinatorHost
from .diagnostics import redact_diagnostic_text
from .stages import StageGitHub
from .work_item import ItemKind

logger = logging.getLogger("hephaestus.automation.pipeline.coordinator")


class IssueClassificationCoordinator(_CoordinatorHost):
    """Own the failure boundary for one repository issue classification."""

    def _classify_repo_issue_entry(
        self,
        repo: str,
        source: ct.RepoIssueSource,
        number: int,
        github: StageGitHub,
    ) -> _seeding.SeedEntry | None:
        """Classify one row, or record its safe terminal failure."""
        try:
            facts = _seeding.seed_issue_from_github(number, github)
            if source.wave_lease is None and STATE_PLAN_BLOCKED in facts.labels:
                github.ensure_blocked_audit(number)
            entry = _seeding.seed_entry_from_facts(facts)
            if source.wave_lease is not None:
                entry = issue_waves_mod.wave_entry_from_facts(
                    source.wave_lease,
                    facts,
                    entry,
                    repo_root=Path(str(self._ctx_for_repo(repo).paths.repo_root)),
                    org=self.config.org,
                    repo=repo,
                )
            scope_stages = self.config.scope.stages if self.config.scope is not None else None
            if source.wave_lease is None or entry.stage is not ct.StageName.FINISHED:
                stage, reason, passed = self._scope_seed_decision(
                    number, entry.stage, entry.reason, scope_stages
                )
                entry = replace(entry, stage=stage, reason=reason, passed=passed)
            return entry
        except Exception as exc:
            detail = " ".join(redact_diagnostic_text(str(exc)).split())[:300]
            logger.warning(
                "repo:%s: issue #%d classification failed (%s): %s",
                repo,
                number,
                type(exc).__name__,
                detail or "no diagnostic",
            )
            self._record_issue_classification_failure(repo, number, exc)
            source.pending = None
            source.seeded_count += 1
            self._progress = True
            return None

    def _record_issue_classification_failure(
        self, repo: str, number: int, error: Exception
    ) -> None:
        """Retain one safe terminal result for a permanent issue failure."""
        detail = " ".join(redact_diagnostic_text(str(error)).split())[:300]
        reason = (
            f"classification failed ({type(error).__name__}): "
            f"{detail or 'no diagnostic'}; manual recovery required"
        )
        item = ct.WorkItem(
            repo=repo,
            kind=ItemKind.ISSUE,
            issue=number,
            stage=ct.StageName.FINISHED,
        )
        item.result = ct.ItemResult(
            passed=False,
            reason=reason,
            final_stage=ct.StageName.REPO,
        )
        item.payload["entry_stage"] = ct.StageName.REPO.value
        self.items.append(item)
        self._record_terminal_result(item)
