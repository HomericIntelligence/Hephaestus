"""Tests for file reservations across a review return to implementation."""

from pathlib import Path
from typing import Any, Never

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages.base import Stage
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool, fake_worker_factories
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def test_review_return_keeps_its_file_claim_ahead_of_an_aged_waiter(tmp_path: Path) -> None:
    """A waiting item cannot take the files of a nonterminal PR owner."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            max_workers=2,
            projects_dir=tmp_path,
            rate_guard_enabled=False,
        ),
        github=FakeStageGitHub(),
        **fake_worker_factories(FakeWorkerPool(), FakeWorkerPool()),
        install_signals=False,
    )
    claims = frozenset({(("org", "repo"), "src/worker.py")})
    owner = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=1,
        pr=11,
        stage=StageName.PR_REVIEW,
        branch="auto-1-impl",
    )
    owner.payload["_implementation_file_claims"] = claims
    waiter = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.IMPLEMENTATION)
    waiter.payload["_implementation_file_claims"] = claims
    waiter.payload["file_overlap_deferrals"] = 20
    assert coordinator._push_item(owner, owner.stage, enter=True)
    assert coordinator._claim_item(owner.stage) is owner
    assert coordinator._push_item(waiter, waiter.stage, enter=True)
    assert coordinator._handoff_item(owner, StageName.IMPLEMENTATION, enter=True)

    selected = coordinator._select_implementation_dispatch(
        coordinator.queues[StageName.IMPLEMENTATION].snapshot()
    )

    assert selected == [owner]
    assert owner.payload["_implementation_file_claims"] == claims
    assert waiter.payload["file_overlap_deferrals"] == 21


def test_idle_recovery_uses_the_same_file_admission_as_normal_queue_draining(
    tmp_path: Path,
) -> None:
    """Idle recovery cannot run a waiter ahead of an existing file owner."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            max_workers=2,
            projects_dir=tmp_path,
            rate_guard_enabled=False,
        ),
        github=FakeStageGitHub(),
        **fake_worker_factories(FakeWorkerPool(), FakeWorkerPool()),
        install_signals=False,
    )
    ran: list[int | None] = []

    class RecordingStage(Stage):
        def on_enter(self, item: WorkItem, _ctx: Any) -> StageOutcome:
            ran.append(item.issue)
            return StageOutcome(Disposition.FINISH_PASS, "test item completed")

        def step(self, _item: WorkItem, _ctx: Any) -> Never:
            raise AssertionError("on_enter completes this test stage")

        def on_job_done(self, _item: WorkItem, _result: object, _ctx: Any) -> None:
            raise AssertionError("this test stage does not submit a job")

    claims = frozenset({(("org", "repo"), "src/worker.py")})
    waiter = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.IMPLEMENTATION)
    owner = WorkItem(
        repo="repo", kind=ItemKind.ISSUE, issue=1, pr=11, stage=StageName.IMPLEMENTATION
    )
    waiter.payload["_implementation_file_claims"] = claims
    owner.payload["_implementation_file_claims"] = claims
    assert coordinator._push_item(waiter, waiter.stage, enter=True)
    assert coordinator._push_item(owner, owner.stage, enter=True)
    coordinator.stages[StageName.IMPLEMENTATION] = RecordingStage()
    coordinator._progress = False
    coordinator._stalled_ticks = 2

    coordinator._idle_wait()

    assert ran == [1]
    assert waiter.result is None
    assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == [waiter]
