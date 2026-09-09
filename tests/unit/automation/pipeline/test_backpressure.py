"""Coordinator-wide queue-capacity and admission-limit contracts (#2399)."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.jobs import JobHandle
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool, fake_worker_factories
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _RecordingWorkerPool(FakeWorkerPool):
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
        rebase_policy_selector: Any = None,
        evidence_receipt_dir: Path | None = None,
        host_verification_pyxis_image: Path | None = None,
        host_verification_pyxis_sha256: str | None = None,
        host_verification_pyxis_authority: Path | None = None,
        host_verification_pyxis_quota_root: Path | None = None,
        podman_machine: str | None = None,
    ) -> None:
        super().__init__(size=size, shutdown=shutdown, completion_q=completion_q)
        del lock_dir
        self.size = size
        self.shutdown_event = shutdown
        self.completion_q = completion_q
        self.gh_extra_path_root = gh_extra_path_root
        self.github_job_runner = github_job_runner
        self.athena_skill_executor = athena_skill_executor
        self.rebase_policy_selector = rebase_policy_selector
        self.evidence_receipt_dir = evidence_receipt_dir
        self.host_verification_pyxis_image = host_verification_pyxis_image
        self.host_verification_pyxis_sha256 = host_verification_pyxis_sha256
        self.host_verification_pyxis_authority = host_verification_pyxis_authority
        self.host_verification_pyxis_quota_root = host_verification_pyxis_quota_root
        self.podman_machine = podman_machine


def _config(
    tmp_path: Path,
    *,
    org: str = "org",
    parallel_repos: int = 2,
    max_workers: int = 3,
    gh_extra_path_root: Path | None = None,
) -> PipelineConfig:
    """Build a configuration whose global work capacity is easy to inspect."""
    return PipelineConfig(
        org=org,
        repos=["repo-a", "repo-b"],
        parallel_repos=parallel_repos,
        max_workers=max_workers,
        projects_dir=tmp_path,
        gh_extra_path_root=gh_extra_path_root,
        rate_guard_enabled=False,
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
    assert isinstance(coordinator.pool, _RecordingWorkerPool)
    assert coordinator.pool.size == capacity
    assert coordinator.pool.completion_q is coordinator.completion_q
    assert coordinator.pool.gh_extra_path_root is None
    assert coordinator.pool.github_job_runner is not None
    assert coordinator.pool.athena_skill_executor is not None
    assert coordinator.pool.evidence_receipt_dir is None
    assert coordinator.pool.host_verification_pyxis_image == config.host_verification_pyxis_image
    assert coordinator.pool.host_verification_pyxis_sha256 is None
    assert coordinator.pool.host_verification_pyxis_authority is None
    assert coordinator.pool.host_verification_pyxis_quota_root is None
    assert coordinator.pool.podman_machine is None


def test_coordinator_passes_extra_gh_root_to_worker_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI-admitted GitHub root reaches the worker-pool trust boundary."""
    from hephaestus.automation.pipeline import worker_pool as worker_pool_mod

    monkeypatch.setattr(worker_pool_mod, "WorkerPool", _RecordingWorkerPool)
    config = _config(tmp_path, gh_extra_path_root=tmp_path)

    coordinator = Coordinator(config, github=FakeStageGitHub(), install_signals=False)

    assert isinstance(coordinator.pool, _RecordingWorkerPool)
    assert coordinator.pool.gh_extra_path_root == tmp_path


@pytest.mark.parametrize("machine", [None, "hephaestus-ci"])
def test_coordinator_passes_selected_podman_machine_to_worker_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine: str | None
) -> None:
    """The explicit connection reaches the worker without a default override."""
    from hephaestus.automation.pipeline import worker_pool as worker_pool_mod

    monkeypatch.setattr(worker_pool_mod, "WorkerPool", _RecordingWorkerPool)
    config = replace(_config(tmp_path), podman_machine=machine)
    coordinator = Coordinator(config, github=FakeStageGitHub(), install_signals=False)
    assert coordinator.pool.podman_machine == machine


def test_coordinator_passes_bound_rebase_policy_selector_to_recording_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recording pool receives the selector without a capacity change."""
    from hephaestus.automation.pipeline import worker_pool as worker_pool_mod

    monkeypatch.setattr(worker_pool_mod, "WorkerPool", _RecordingWorkerPool)
    config = _config(tmp_path, org="HomericIntelligence")
    capacity = config.parallel_repos * config.max_workers
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        install_signals=False,
    )

    assert isinstance(coordinator.pool, _RecordingWorkerPool)
    assert coordinator.pool.size == capacity
    assert coordinator.completion_q.maxsize == capacity
    selector = coordinator.pool.rebase_policy_selector
    assert callable(selector)
    policy = selector("Hephaestus")
    assert policy is not None
    assert policy.name == "hephaestus-adr-v1"
    assert selector("Comet") is None


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


def test_coordinator_requires_both_worker_factories(tmp_path: Path) -> None:
    """A custom main lane requires an explicit auxiliary lane factory."""
    with pytest.raises(ValueError, match="supplied together"):
        Coordinator(
            _config(tmp_path),
            github=FakeStageGitHub(),
            pool_factory=FakeWorkerPool().factory,
            install_signals=False,
        )


def test_coordinator_supplies_distinct_bounded_factory_channels(tmp_path: Path) -> None:
    """Each worker factory receives one independently bounded channel."""
    main = FakeWorkerPool()
    auxiliary = FakeWorkerPool()
    coordinator = Coordinator(
        _config(tmp_path),
        github=FakeStageGitHub(),
        **fake_worker_factories(main, auxiliary),
        install_signals=False,
    )
    assert main.completion_q is coordinator.completion_q
    assert main.completion_q.maxsize == 6
    assert auxiliary.completion_q is coordinator.auxiliary_completion_q
    assert auxiliary.completion_q.maxsize == 1
    assert main.completion_q is not auxiliary.completion_q
    assert main.shutdown_event is coordinator.worker_shutdown_event
    assert auxiliary.shutdown_event is coordinator.force_shutdown_event


def test_coordinator_rejects_one_pool_for_both_lanes(tmp_path: Path) -> None:
    """One worker object cannot own both lane channels."""
    pool = FakeWorkerPool()
    with pytest.raises(ValueError, match="must be distinct"):
        Coordinator(
            _config(tmp_path),
            github=FakeStageGitHub(),
            pool_factory=pool.factory,
            auxiliary_pool_factory=pool.factory,
            install_signals=False,
        )
