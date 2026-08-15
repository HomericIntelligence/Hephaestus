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
from hephaestus.automation.pipeline.routing import StageName
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
        gh_extra_path_root: Path | None = None,
        github_job_runner: Any = None,
        athena_skill_executor: Any = None,
        rebase_adr_validator: Any = None,
        rebase_structural_test_argv: Any = None,
    ) -> None:
        del lock_dir
        del rebase_adr_validator, rebase_structural_test_argv
        self.size = size
        self.shutdown_event = shutdown
        self.completion_q = completion_q
        self.gh_extra_path_root = gh_extra_path_root
        self.github_job_runner = github_job_runner
        self.athena_skill_executor = athena_skill_executor


def _config(
    tmp_path: Path,
    *,
    parallel_repos: int = 2,
    max_workers: int = 3,
    gh_extra_path_root: Path | None = None,
) -> PipelineConfig:
    """Build a configuration whose global work capacity is easy to inspect."""
    return PipelineConfig(
        org="org",
        repos=["repo-a", "repo-b"],
        parallel_repos=parallel_repos,
        max_workers=max_workers,
        projects_dir=tmp_path,
        gh_extra_path_root=gh_extra_path_root,
    )


def test_coordinator_uses_independent_main_and_learning_capacities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main queues use C while learning uses its own bounded capacity."""
    from hephaestus.automation.pipeline import worker_pool as worker_pool_mod

    monkeypatch.setattr(worker_pool_mod, "WorkerPool", _RecordingWorkerPool)
    config = _config(tmp_path)
    capacity = config.parallel_repos * config.max_workers

    coordinator = Coordinator(config, github=FakeStageGitHub(), install_signals=False)

    main_capacities = {
        queue.capacity for stage, queue in coordinator.queues.items() if stage.value != "learning"
    }
    assert main_capacities == {capacity}
    assert coordinator.queues[StageName.LEARNING].capacity == config.learning_queue_capacity
    assert coordinator.completion_q.maxsize == capacity
    assert coordinator.auxiliary_completion_q.maxsize == config.learning_queue_capacity
    assert coordinator.pool.size == capacity
    assert coordinator.pool.completion_q is coordinator.completion_q
    assert coordinator.pool.gh_extra_path_root is None
    assert coordinator.pool.github_job_runner is not None
    assert coordinator.pool.athena_skill_executor is not None


def test_coordinator_passes_extra_gh_root_to_worker_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI-admitted GitHub root reaches the worker-pool trust boundary."""
    from hephaestus.automation.pipeline import worker_pool as worker_pool_mod

    monkeypatch.setattr(worker_pool_mod, "WorkerPool", _RecordingWorkerPool)
    config = _config(tmp_path, gh_extra_path_root=tmp_path)

    coordinator = Coordinator(config, github=FakeStageGitHub(), install_signals=False)

    assert coordinator.pool.gh_extra_path_root == tmp_path


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
