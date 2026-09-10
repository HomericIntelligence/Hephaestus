"""Keep pre-host learning failures separate from uncertain delivery outcomes."""

from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import WorkspaceBinding, WorkspaceBindingError
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages import learning
from hephaestus.automation.pipeline.work_item import ItemKind, LearningIntent, WorkItem
from hephaestus.automation.source_worktree import (
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
)
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


@pytest.mark.parametrize("failure", ["binding", "deadline", "shutdown"])
def test_pre_host_learning_failure_releases_claim_and_keeps_merge_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """No host call means that recovery must not record an unknown delivery."""
    main = FakeWorkerPool()
    auxiliary = FakeWorkerPool()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            rate_guard_enabled=False,
            budget_overrides={"learn": 2},
        ),
        github=FakeStageGitHub(),
        **fake_worker_factories(main, auxiliary),
        install_signals=False,
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, pr=7, stage=StageName.MERGE_WAIT)
    intent = LearningIntent.post_merge(repo="repo", issue=1, pr=7)
    item.learning_intents.append(intent)
    item.payload["_impl_source_revision"] = "a" * 40
    coordinator._route(
        claim_test_item(coordinator, item), StageOutcome(Disposition.FINISH_PASS, "merged")
    )
    primary = item.result
    assert primary is not None and primary.passed
    journal = coordinator._ctx_for(item).learning_journal
    preparations: list[str] = []

    def prepare(*_args: Any, **_kwargs: Any) -> WorkspaceBinding:
        preparations.append(failure)
        if failure == "binding":
            raise WorkspaceBindingError("source receipt changed")
        if failure == "deadline":
            raise SourceWorkspacePreparationError(SourceWorkspacePreparationCause.GIT_TIMEOUT)
        coordinator.shutdown.set()
        raise InterruptedError("source workspace operation cancelled")

    monkeypatch.setattr(learning, "source_workspace_binding", prepare)
    coordinator._run_item(claim_test_item(coordinator, item))

    record = journal.load(intent.key)
    assert record is not None
    assert record["status"] in {"pending", "failed"}
    assert record.get("error") != "outcome_unknown"
    assert not journal.claim_is_active(intent.key)
    assert 1 <= len(preparations) <= 2
    assert not main.submitted and not auxiliary.submitted
    assert item.post_processing is not None
    assert item.post_processing.result == primary
    if failure == "shutdown" and item.result != primary:
        assert item.result is not None and item.result.reason.startswith("resumable at ")
    else:
        assert item.result == primary
