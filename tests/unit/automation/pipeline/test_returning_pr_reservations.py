"""Test file reservations when accepted PRs return to implementation."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages.base import JobRequest, Stage, StageContext
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _ImplementationStage(Stage):
    """Submit one job, then return the PR to review after completion."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> None:
        """Keep the state set by queue admission."""

    def step(self, item: WorkItem, ctx: StageContext) -> JobRequest | StageOutcome:
        """Use the worker result to complete this implementation attempt."""
        if item.state == "DONE":
            return StageOutcome(Disposition.ADVANCE, "implementation complete")
        return JobRequest(
            GitJob(repo=item.repo, op="fetch_main", timeout_s=10),
            "DONE",
        )

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Require a successful result from the controlled worker."""
        assert result.ok


def _coordinator(tmp_path: Path, *, max_workers: int) -> tuple[Coordinator, FakeWorkerPool]:
    """Create separate worker lanes with bounded completion channels."""
    pool = FakeWorkerPool()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a", "repo-b"],
            max_workers=max_workers,
            projects_dir=tmp_path,
            rate_guard_enabled=False,
            enable_learn=False,
        ),
        github=FakeStageGitHub(),
        **fake_worker_factories(pool, FakeWorkerPool()),
        install_signals=False,
    )
    coordinator.stages[StageName.IMPLEMENTATION] = _ImplementationStage()
    return coordinator, pool


def _returning_pr(
    coordinator: Coordinator,
    tmp_path: Path,
    issue: int,
    paths: set[str],
    *,
    repo: str = "repo-a",
) -> WorkItem:
    """Accept a PR with verified paths, then route it back to implementation."""
    item = WorkItem(
        repo=repo,
        kind=ItemKind.PR,
        issue=issue,
        pr=issue + 100,
        worktree=str(tmp_path / repo / f"writer-{issue}"),
        stage=StageName.PR_REVIEW,
        payload={
            "_implementation_file_claims": frozenset(),
            "review_changed_paths": sorted(paths),
        },
    )
    assert coordinator._push_item(item, StageName.PR_REVIEW, enter=True)
    coordinator._active_implementation_file_claims()
    coordinator._route(
        claim_test_item(coordinator, item),
        StageOutcome(Disposition.FAIL_BACK, "agent_error"),
    )
    assert item.stage is StageName.IMPLEMENTATION
    return item


@pytest.mark.parametrize(
    "paths_by_item",
    [
        pytest.param([{"shared.py"}, {"shared.py"}], id="two-overlapping-prs"),
        pytest.param(
            [{"ab.py", "ca.py"}, {"ab.py", "bc.py"}, {"bc.py", "ca.py"}],
            id="three-pr-cycle",
        ),
    ],
)
def test_returning_prs_make_serial_progress_through_review_and_merge(
    tmp_path: Path, paths_by_item: list[set[str]]
) -> None:
    """One queued owner runs; its claims block peers until it leaves merge."""
    coordinator, pool = _coordinator(tmp_path, max_workers=len(paths_by_item))
    items = [
        _returning_pr(coordinator, tmp_path, index + 1, paths)
        for index, paths in enumerate(paths_by_item)
    ]
    assert coordinator.items == items
    assert coordinator.live_work_count == len(items)

    for index, item in enumerate(items):
        coordinator._drain_implementation()

        assert list(coordinator.in_flight.values()) == [item]
        assert len(pool.submitted) == index + 1
        assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == items[index + 1 :]
        for _ in range(3):
            coordinator._drain_implementation()
        assert len(pool.submitted) == index + 1

        coordinator._drain_completions()
        assert item.stage is StageName.PR_REVIEW
        coordinator._drain_implementation()
        assert len(pool.submitted) == index + 1
        coordinator._route(claim_test_item(coordinator, item), StageOutcome(Disposition.ADVANCE))
        assert item.stage is StageName.MERGE_WAIT
        coordinator._drain_implementation()
        assert len(pool.submitted) == index + 1
        assert item.payload["_implementation_file_claims"] == frozenset(
            (("org", item.repo), path) for path in paths_by_item[index]
        )

        coordinator._route(
            claim_test_item(coordinator, item), StageOutcome(Disposition.FINISH_PASS)
        )
        assert item.result is not None and item.result.passed
        assert "_implementation_file_claims" not in item.payload

    assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == []


@pytest.mark.parametrize("owner_stage", [StageName.PR_REVIEW, StageName.MERGE_WAIT])
def test_returning_prs_wait_for_an_owner_outside_the_implementation_queue(
    tmp_path: Path, owner_stage: StageName
) -> None:
    """Queued PRs cannot take paths from an accepted review or merge owner."""
    coordinator, pool = _coordinator(tmp_path, max_workers=3)
    owner = WorkItem(
        repo="repo-a",
        kind=ItemKind.PR,
        issue=3,
        pr=103,
        stage=owner_stage,
        payload={"review_changed_paths": ["shared.py"]},
    )
    assert coordinator._push_item(owner, owner_stage, enter=True)
    first = _returning_pr(coordinator, tmp_path, 1, {"shared.py"})
    second = _returning_pr(coordinator, tmp_path, 2, {"shared.py"})

    for _ in range(3):
        coordinator._drain_implementation()

    assert pool.submitted == []
    assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == [first, second]
    coordinator._route(claim_test_item(coordinator, owner), StageOutcome(Disposition.FINISH_PASS))
    coordinator._drain_implementation()

    assert list(coordinator.in_flight.values()) == [first]
    assert len(pool.submitted) == 1
    assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == [second]


def test_returning_prs_in_distinct_repositories_can_run_together(tmp_path: Path) -> None:
    """Equal paths and issue numbers in separate repositories do not conflict."""
    coordinator, pool = _coordinator(tmp_path, max_workers=2)
    first = _returning_pr(coordinator, tmp_path, 1, {"shared.py"})
    second = _returning_pr(coordinator, tmp_path, 1, {"shared.py"}, repo="repo-b")

    coordinator._drain_implementation()

    assert list(coordinator.in_flight.values()) == [first, second]
    assert len(pool.submitted) == 2
    assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == []
