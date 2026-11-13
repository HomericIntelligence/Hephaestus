"""Topological decomposition child admission (coordinator-owned, #2982)."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

PARENT = 8
CHILD_A = 41
CHILD_B = 42
CHILD_C = 43


def make_admission_coordinator(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, dry_run: bool = False
) -> tuple[Coordinator, FakeWorkerPool, FakeStageGitHub]:
    """Build one coordinator wired to fakes for admission-level tests."""
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        issues=[],
        loops=1,
        max_workers=4,
        parallel_repos=1,
        dry_run=dry_run,
        serialize_file_overlap=True,
        enable_learn=True,
        agent="claude",
        projects_dir=tmp_path,
        rate_guard_enabled=False,
    )
    gh = FakeStageGitHub()
    pool = FakeWorkerPool()
    coordinator = Coordinator(config, github=gh, **fake_worker_factories(pool, None))
    return coordinator, pool, gh


def _graph(*, layered: bool = True) -> dict[int, dict[str, Any]]:
    """Return one decomposition graph with two roots and one dependent."""
    graph: dict[int, dict[str, Any]] = {
        CHILD_A: {"node_id": "node-a", "layer": 0, "deps": set()},
        CHILD_B: {"node_id": "node-b", "layer": 0, "deps": set()},
    }
    if layered:
        graph[CHILD_C] = {"node_id": "node-c", "layer": 1, "deps": {CHILD_A}}
    return graph


def _seed_children(gh: FakeStageGitHub) -> None:
    for issue_number in (CHILD_A, CHILD_B, CHILD_C):
        gh.issues[issue_number] = {
            "title": f"Implement child {issue_number}",
            "body": f"Child body for #{issue_number}.",
            "state": "OPEN",
        }


def _child_item(issue_number: int, repo: str = "repo-a") -> WorkItem:
    item = WorkItem(repo=repo, kind=ItemKind.ISSUE, issue=issue_number, stage=StageName.PLANNING)
    item.state = "ENTER"
    return item


def _queued_issues(coordinator: Coordinator) -> list[int]:
    return [
        item.issue
        for item in coordinator.queues[StageName.PLANNING].snapshot()
        if item.issue is not None
    ]


class TestTopologicalAdmission:
    """The coordinator queues ready children exactly once, layer by layer."""

    def test_queues_each_child_once(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A repeated admission pass never queues one child issue twice."""
        coordinator, _pool, gh = make_admission_coordinator(tmp_path, monkeypatch)
        _seed_children(gh)
        coordinator._decomposition_graph[("repo-a", PARENT)] = _graph(layered=False)

        coordinator._queue_ready_children("repo-a", PARENT)
        assert sorted(_queued_issues(coordinator)) == [CHILD_A, CHILD_B]

        coordinator._queue_ready_children("repo-a", PARENT)
        assert sorted(_queued_issues(coordinator)) == [CHILD_A, CHILD_B]
        assert coordinator._decomposition_queued == {
            ("repo-a", CHILD_A),
            ("repo-a", CHILD_B),
        }

    def test_admits_independent_nodes_together(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Layer-0 nodes queue in one pass without ordering between them."""
        coordinator, _pool, gh = make_admission_coordinator(tmp_path, monkeypatch)
        _seed_children(gh)
        coordinator._decomposition_graph[("repo-a", PARENT)] = _graph(layered=False)

        coordinator._queue_ready_children("repo-a", PARENT)

        queued = _queued_issues(coordinator)
        assert sorted(queued) == [CHILD_A, CHILD_B]
        ctx = coordinator._ctx_for_repo("repo-a")
        assert ctx.generated_issue_ids == {CHILD_A, CHILD_B}

    def test_waits_for_prerequisites(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dependent child queues only after its prerequisite passes."""
        coordinator, _pool, gh = make_admission_coordinator(tmp_path, monkeypatch)
        _seed_children(gh)
        coordinator._decomposition_graph[("repo-a", PARENT)] = _graph()

        coordinator._queue_ready_children("repo-a", PARENT)
        assert sorted(_queued_issues(coordinator)) == [CHILD_A, CHILD_B]
        assert CHILD_C not in _queued_issues(coordinator)

        finished = _child_item(CHILD_A)
        finished.result = ItemResult(passed=True, reason="done", final_stage=StageName.FINISHED)
        coordinator._release_decomposition_dependents(finished, passed=True)

        assert _queued_issues(coordinator) == [CHILD_A, CHILD_B, CHILD_C]
        assert coordinator._decomposition_done[("repo-a", PARENT)] == {CHILD_A}

    def test_blocks_descendants_after_failed_prerequisite(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed prerequisite finishes every descendant failed, not queued."""
        coordinator, _pool, gh = make_admission_coordinator(tmp_path, monkeypatch)
        _seed_children(gh)
        coordinator._decomposition_graph[("repo-a", PARENT)] = _graph()

        coordinator._queue_ready_children("repo-a", PARENT)
        assert sorted(_queued_issues(coordinator)) == [CHILD_A, CHILD_B]

        finished = _child_item(CHILD_A)
        finished.result = ItemResult(
            passed=False, reason="implementation failed", final_stage=StageName.FINISHED
        )
        coordinator._release_decomposition_dependents(finished, passed=False)

        # The dependent never enters a work queue and records one failed result.
        assert CHILD_C not in _queued_issues(coordinator)
        assert ("repo-a", CHILD_C) in coordinator._decomposition_queued
        assert CHILD_C not in coordinator._decomposition_graph[("repo-a", PARENT)]
        failed = [result for result in coordinator.ledger if result.passed is False]
        assert any("prerequisite failed" in result.reason for result in failed)
