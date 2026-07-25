"""Coordinator-wide queue-capacity and admission-limit contracts (#2399)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Any, cast

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import JobHandle
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _RecordingWorkerPool:
    """Worker-pool stand-in that records the coordinator's production wiring."""

    def __init__(
        self,
        size: int,
        shutdown: Event,
        completion_q: Any,
        lock_dir: Path | None = None,
    ) -> None:
        del lock_dir
        self.size = size
        self.shutdown_event = shutdown
        self.completion_q = completion_q


def _config(tmp_path: Path, *, parallel_repos: int = 2, max_workers: int = 3) -> PipelineConfig:
    """Build a configuration whose global work capacity is easy to inspect."""
    return PipelineConfig(
        org="org",
        repos=["repo-a", "repo-b"],
        parallel_repos=parallel_repos,
        max_workers=max_workers,
        projects_dir=tmp_path,
    )


def test_coordinator_uses_one_capacity_for_all_queues_and_worker_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every coordinator-owned queue and its worker pool use C exactly."""
    from hephaestus.automation.pipeline import worker_pool as worker_pool_mod

    monkeypatch.setattr(worker_pool_mod, "WorkerPool", _RecordingWorkerPool)
    config = _config(tmp_path)
    capacity = config.parallel_repos * config.max_workers

    coordinator = Coordinator(config, github=FakeStageGitHub(), install_signals=False)

    assert {queue.capacity for queue in coordinator.queues.values()} == {capacity}
    assert coordinator.completion_q.maxsize == capacity
    assert coordinator.pool.size == capacity
    assert coordinator.pool.completion_q is coordinator.completion_q


def test_admission_rejects_when_global_worker_capacity_is_live(tmp_path: Path) -> None:
    """A fifth cross-repo job cannot enter a four-worker executor backlog."""
    coordinator = object.__new__(Coordinator)
    coordinator.config = _config(tmp_path, parallel_repos=2, max_workers=2)
    live_repos = ("repo-a", "repo-b", "repo-c", "repo-d")
    coordinator.in_flight = {
        cast(JobHandle, object()): WorkItem(repo=repo, kind=ItemKind.ISSUE, issue=index)
        for index, repo in enumerate(live_repos, start=1)
    }
    coordinator.inflight_per_repo = Counter(dict.fromkeys(live_repos, 1))

    assert coordinator._admit(WorkItem(repo="repo-e", kind=ItemKind.ISSUE, issue=5)) is False


def test_coordinator_rejects_injected_completion_queue_with_wrong_capacity(
    tmp_path: Path,
) -> None:
    """An injected completion queue cannot weaken the coordinator's C bound."""
    config = _config(tmp_path, parallel_repos=2, max_workers=2)
    incompatible_pool = _RecordingWorkerPool(
        size=4,
        shutdown=Event(),
        completion_q=Queue(maxsize=5),
    )

    with pytest.raises(ValueError):
        Coordinator(
            config,
            github=FakeStageGitHub(),
            pool=incompatible_pool,
            install_signals=False,
        )


def test_coordinator_replaces_an_injected_unbounded_completion_queue(tmp_path: Path) -> None:
    """A zero-maxsize test double cannot silently bypass the global C bound."""
    config = _config(tmp_path, parallel_repos=2, max_workers=2)
    unbounded_pool = _RecordingWorkerPool(
        size=4,
        shutdown=Event(),
        completion_q=Queue(),
    )

    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        pool=unbounded_pool,
        install_signals=False,
    )

    assert coordinator.completion_q.maxsize == 4
    assert unbounded_pool.completion_q is coordinator.completion_q
