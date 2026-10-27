"""Queue capacity and saturation facts in the coordinator observability snapshot."""

from __future__ import annotations

from pathlib import Path

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool, fake_worker_factories
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _coordinator(tmp_path: Path) -> Coordinator:
    """Create one C=1 coordinator with a bounded learning queue."""
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        projects_dir=tmp_path,
        max_workers=1,
        learning_queue_capacity=2,
        rate_guard_enabled=False,
    )
    return Coordinator(
        config,
        github=FakeStageGitHub(),
        **fake_worker_factories(FakeWorkerPool(), None),
        install_signals=False,
    )


def _item(issue: int = 1) -> WorkItem:
    """Build one issue item for a stage queue."""
    return WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=issue)


def test_snapshot_reports_each_stage_capacity(tmp_path: Path) -> None:
    """The snapshot exposes the bounded capacity of every stage queue."""
    coordinator = _coordinator(tmp_path)

    snapshot = coordinator._observability_snapshot()

    assert snapshot["queue_capacities"] == {
        name.value: coordinator.queues[name].capacity for name in coordinator.queues
    }
    assert snapshot["queue_capacities"][StageName.LEARNING.value] == 2
    assert snapshot["queue_capacities"][StageName.PLANNING.value] == 1
    assert snapshot["saturated_queues"] == []


def test_snapshot_reports_a_queue_at_capacity(tmp_path: Path) -> None:
    """A queue that reserved all of its capacity is reported as saturated."""
    coordinator = _coordinator(tmp_path)
    assert coordinator.queues[StageName.PLANNING].offer(_item())

    snapshot = coordinator._observability_snapshot()

    assert snapshot["queue_depths"][StageName.PLANNING.value] == 1
    assert snapshot["saturated_queues"] == [StageName.PLANNING.value]


def test_snapshot_reports_leased_occupancy_as_saturation(tmp_path: Path) -> None:
    """A leased item reserves capacity without appearing in the ready depth."""
    coordinator = _coordinator(tmp_path)
    queue = coordinator.queues[StageName.PLANNING]
    assert queue.offer(_item())
    queue.claim()

    snapshot = coordinator._observability_snapshot()

    assert queue.occupancy == queue.capacity
    assert snapshot["queue_depths"][StageName.PLANNING.value] == 0
    assert snapshot["saturated_queues"] == [StageName.PLANNING.value]


def test_snapshot_reports_the_latched_completion_fault(tmp_path: Path) -> None:
    """The existing completion latch joins the same saturation list."""
    coordinator = _coordinator(tmp_path)

    coordinator._completion_saturation.set()

    assert coordinator._observability_snapshot()["saturated_queues"] == ["completion"]


def test_health_snapshot_carries_queue_capacity_and_saturation(tmp_path: Path) -> None:
    """/health exposes the same queue facts the alert rules read."""
    coordinator = _coordinator(tmp_path)
    assert coordinator.queues[StageName.PLANNING].offer(_item())

    health = coordinator._health_snapshot()

    assert health["queue_capacities"][StageName.PLANNING.value] == 1
    assert health["saturated_queues"] == [StageName.PLANNING.value]
    assert health["status"] == "degraded"  # the critical saturation alert is active
