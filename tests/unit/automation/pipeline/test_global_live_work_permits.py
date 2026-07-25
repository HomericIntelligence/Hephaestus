"""Coordinator-wide live-work permit regression coverage (#2399)."""

from __future__ import annotations

from pathlib import Path

from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _coordinator(tmp_path: Path) -> Coordinator:
    """Build a coordinator with a deliberately minimal global capacity."""
    return Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            parallel_repos=1,
            max_workers=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )


def _issue(number: int, stage: StageName) -> WorkItem:
    """Create one distinct live issue item."""
    return WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=number, stage=stage)


def _complete_finished_item(coordinator: Coordinator, item: WorkItem) -> None:
    """Drive a finished item through its terminal coordinator release path."""
    assert coordinator._claim_item(StageName.FINISHED) is item
    coordinator._route(item, StageOutcome(Disposition.FINISH_PASS, "recorded"))


def test_global_permit_refuses_c_plus_one_across_distinct_stage_queues(tmp_path: Path) -> None:
    """C=1 applies across stages, not once independently to each stage queue.

    Before the permit pool, the two ``StageQueue(capacity=1)`` instances
    accepted one item each.  That made two nonterminal WorkItems live even
    though the configured work window was one.
    """
    coordinator = _coordinator(tmp_path)
    first = _issue(1, StageName.PLANNING)
    second = _issue(2, StageName.PLAN_REVIEW)

    assert coordinator._push_item(first, StageName.PLANNING, enter=True) is True
    assert coordinator.live_work_count == 1

    assert coordinator._push_item(second, StageName.PLAN_REVIEW, enter=True) is False
    assert coordinator.queues[StageName.PLANNING].snapshot() == [first]
    assert coordinator.queues[StageName.PLAN_REVIEW].snapshot() == []
    assert coordinator.live_work_count == 1
    assert id(second) not in coordinator._seen_item_ids

    # Routing to the finished sink retains the permit: cleanup/ledger work is
    # still nonterminal. Only the sink's terminal outcome makes a slot free.
    assert coordinator._claim_item(StageName.PLANNING) is first
    coordinator._finish(first, passed=True, reason="done")
    assert coordinator.live_work_count == 1
    _complete_finished_item(coordinator, first)
    assert coordinator.live_work_count == 0

    assert coordinator._push_item(second, StageName.PLAN_REVIEW, enter=True) is True
    assert coordinator.live_work_count == 1


def test_global_permit_survives_a_lease_and_timer_park(tmp_path: Path) -> None:
    """A timer-parked item keeps its permit until terminal completion."""
    coordinator = _coordinator(tmp_path)
    parked = _issue(1, StageName.PLANNING)
    later = _issue(2, StageName.PR_REVIEW)

    assert coordinator._push_item(parked, StageName.PLANNING, enter=True) is True
    assert coordinator._claim_item(StageName.PLANNING) is parked
    coordinator._timer_park(parked, delay_s=60)

    assert coordinator.queues[StageName.PLANNING].snapshot() == []
    assert [entry[2] for entry in coordinator.timers] == [parked]
    assert coordinator.live_work_count == 1
    assert coordinator._push_item(later, StageName.PR_REVIEW, enter=True) is False
    assert coordinator.live_work_count == 1


def test_global_permit_survives_an_inflight_job(tmp_path: Path) -> None:
    """An item remains globally live while its completion is outstanding."""
    coordinator = _coordinator(tmp_path)
    in_flight = _issue(1, StageName.PLANNING)
    later = _issue(2, StageName.MERGE_WAIT)

    assert coordinator._push_item(in_flight, StageName.PLANNING, enter=True) is True
    assert coordinator._claim_item(StageName.PLANNING) is in_flight
    coordinator._submit(
        in_flight,
        JobRequest(
            GitJob(repo="repo-a", op="push", timeout_s=1),
            on_done_state="AFTER_JOB",
        ),
    )

    assert len(coordinator.in_flight) == 1
    assert coordinator.live_work_count == 1
    assert coordinator._push_item(later, StageName.MERGE_WAIT, enter=True) is False
    assert coordinator.live_work_count == 1
