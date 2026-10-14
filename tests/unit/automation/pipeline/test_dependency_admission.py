"""Coordinator acceptance tests for live implementation dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.dependency_parser import DependencyFact
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _coordinator(
    tmp_path: Path,
    github: FakeStageGitHub,
) -> tuple[Coordinator, FakeWorkerPool]:
    """Build one coordinator with the real implementation stage."""
    pool = FakeWorkerPool()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            projects_dir=tmp_path,
            rate_guard_enabled=False,
        ),
        github=github,
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.IMPLEMENTATION] = ImplementationStage()
    return coordinator, pool


def _dependent() -> WorkItem:
    """Build one dependent item at an implementation-agent boundary."""
    return WorkItem(
        repo="repo-a",
        kind=ItemKind.ISSUE,
        issue=1,
        stage=StageName.IMPLEMENTATION,
        state="IMPLEMENT_WAIT",
        payload={"issue_body": "Depends on #10", "dependencies": [10]},
    )


def _agent_request(
    tmp_path: Path,
) -> Any:
    """Return a handler that exposes one observable agent submission."""

    def request(_stage: Any, item: WorkItem, _ctx: Any) -> JobRequest:
        assert item.issue is not None
        return JobRequest(
            AgentJob(
                repo=item.repo,
                issue=item.issue,
                agent="test-agent",
                model="test-model",
                prompt_builder=lambda **_kwargs: "test prompt",
                cwd=tmp_path,
                timeout_s=1,
                descr="dependent implementation",
            ),
            on_done_state="TEST_WAIT",
        )

    return request


def test_external_dependency_completion_wakes_same_item_and_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry timer wakes the same item after its dependency closes."""
    github = FakeStageGitHub(
        labels=[STATE_PLAN_GO],
        issue_body="Depends on #10",
        dependency_fact_batches=[
            (DependencyFact(10, "Issue", "OPEN"),),
            (DependencyFact(10, "Issue", "CLOSED"),),
        ],
    )
    coordinator, pool = _coordinator(tmp_path, github)
    item = _dependent()
    attempts_before = dict(item.attempts)
    monkeypatch.setattr(
        ImplementationStage,
        "_implement_wait",
        _agent_request(tmp_path),
    )

    coordinator._run_item(claim_test_item(coordinator, item))

    assert pool.submitted == []
    assert item.result is None
    assert item.attempts == attempts_before
    assert len(coordinator.timers) == 1
    assert coordinator.timers[0][2] is item
    assert github.labels[1] == {STATE_PLAN_GO}

    _deadline, sequence, parked = coordinator.timers[0]
    coordinator.timers[0] = (0.0, sequence, parked)
    coordinator._wake_timers()
    queued = coordinator._claim_item(StageName.IMPLEMENTATION)
    assert queued is item
    coordinator._run_item(claim_test_item(coordinator, item))

    assert len(pool.submitted) == 1
    assert isinstance(pool.submitted[0].job, AgentJob)
    assert github.dependency_fact_requests == [(10,), (10,)]
    assert item.result is None
    assert item.attempts == attempts_before
    assert github.labels[1] == {STATE_PLAN_GO}


@pytest.mark.parametrize("outcome", ["BLOCKED", "FAIL", "FAIL_BACK", "SKIP"])
def test_in_wave_unsatisfied_outcomes_keep_dependent_parked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """A terminal prerequisite result cannot replace its open live state."""
    github = FakeStageGitHub(
        labels=[STATE_PLAN_GO],
        issue_body="Depends on #10",
        dependency_fact_batches=[(DependencyFact(10, "Issue", "OPEN"),)],
    )
    coordinator, pool = _coordinator(tmp_path, github)
    prerequisite = WorkItem(
        repo="repo-a",
        kind=ItemKind.ISSUE,
        issue=10,
        stage=StageName.FINISHED,
        state=outcome,
        result=ItemResult(False, outcome, StageName.IMPLEMENTATION),
    )
    coordinator.items.append(prerequisite)
    item = _dependent()
    monkeypatch.setattr(
        ImplementationStage,
        "_implement_wait",
        _agent_request(tmp_path),
    )

    coordinator._run_item(claim_test_item(coordinator, item))

    assert pool.submitted == []
    assert item.result is None
    assert item.state == "IMPLEMENT_WAIT"
    assert len(coordinator.timers) == 1
    assert coordinator.timers[0][2] is item
    assert github.labels[1] == {STATE_PLAN_GO}
    assert github.mutation_log == []


def test_dependency_wait_is_nonterminal_and_preserves_plan_go(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending dependency produces no skip result or label mutation."""
    github = FakeStageGitHub(
        labels=[STATE_PLAN_GO],
        issue_body="Depends on #10",
        dependency_fact_batches=[(DependencyFact(10, "Issue", "OPEN"),)],
    )
    coordinator, pool = _coordinator(tmp_path, github)
    item = _dependent()
    monkeypatch.setattr(
        ImplementationStage,
        "_implement_wait",
        _agent_request(tmp_path),
    )

    coordinator._run_item(claim_test_item(coordinator, item))

    assert pool.submitted == []
    assert item.result is None
    assert item.state == "IMPLEMENT_WAIT"
    assert item.payload["dependency_blocked_reason"] == "dependency #10 is still open"
    assert github.labels[1] == {STATE_PLAN_GO}
    assert github.mutation_log == []
    assert not any(event.state == Disposition.SKIP.value for event in item.history)
