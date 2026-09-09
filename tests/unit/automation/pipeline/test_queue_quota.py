"""Tests for worker-owned quota reads before agent admission."""

from pathlib import Path

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.github_jobs import (
    GitHubJob,
    RateBudgetRead,
    ReadRateBudgetRequest,
)
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stage_results import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def test_agent_admission_submits_quota_read_without_coordinator_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent admission queues a quota read and retains the item's source lease."""
    reads: list[object] = []

    def quota_read(*, timeout: int | None = None) -> tuple[int, int]:
        reads.append(timeout)
        return 1000, 2000

    monkeypatch.setattr(
        "hephaestus.automation.pipeline_github_transport.rate_limit_remaining", quota_read
    )
    pool = FakeWorkerPool()
    pool.queue_result(JobResult(ok=True))
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path, rate_guard_enabled=True),
        github=FakeStageGitHub(),
        **fake_worker_factories(pool, FakeWorkerPool()),
        install_signals=False,
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLANNING)
    assert coordinator._push_item(item, item.stage, enter=True)
    assert coordinator._claim_item(item.stage) is item
    request = JobRequest(
        AgentJob(
            repo="repo",
            issue=1,
            agent="codex",
            model="test",
            prompt_builder=lambda: "test",
            cwd=tmp_path,
            timeout_s=1,
        ),
        "RESULT",
    )

    coordinator._submit(claim_test_item(coordinator, item), request)

    assert reads == []
    assert len(pool.submitted) == 1
    assert isinstance(pool.submitted[0].job, GitHubJob)
    assert id(item) in coordinator._leases
    assert coordinator.live_work_count == 1
    assert item.state == "ENTER"


@pytest.mark.parametrize("remaining", [500, None, 10])
def test_quota_receipt_submits_once_or_parks_on_the_timer(
    tmp_path: Path, remaining: int | None
) -> None:
    """Quota facts select one agent submission or a bounded timer delay."""
    pool = FakeWorkerPool()
    pool.queue_result(JobResult(ok=True))
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path, rate_guard_enabled=True),
        github=FakeStageGitHub(),
        **fake_worker_factories(pool, FakeWorkerPool()),
        install_signals=False,
        monotonic=lambda: 50.0,
        wall_time=lambda: 1000.0,
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLANNING)
    assert coordinator._push_item(item, item.stage, enter=True)
    assert coordinator._claim_item(item.stage) is item
    agent_job = AgentJob(
        repo="repo",
        issue=1,
        agent="codex",
        model="test",
        prompt_builder=lambda: "test",
        cwd=tmp_path,
        timeout_s=1,
    )
    coordinator._submit(claim_test_item(coordinator, item), JobRequest(agent_job, "AGENT_DONE"))
    handle, _placeholder = coordinator.completion_q.get_nowait()
    assert isinstance(handle.job, GitHubJob)
    assert isinstance(handle.job.request, ReadRateBudgetRequest)
    assert handle.job.request.deadline_s == 50.0 + coordinator.config.gh_timeout
    receipt = RateBudgetRead(
        request=handle.job.request,
        remaining=remaining,
        reset_epoch=1010 if remaining is not None else None,
    )

    coordinator._handle_completion(handle, JobResult(ok=True, value=receipt))

    assert item.state == "ENTER"
    assert coordinator.live_work_count == 1
    assert "_pending_agent_request" not in item.payload
    if remaining == 10:
        assert len(pool.submitted) == 1
        assert coordinator.timers == [(65.0, 0, item)]
        assert not coordinator.in_flight
        assert id(item) not in coordinator._leases
    else:
        assert len(pool.submitted) == 2
        assert isinstance(pool.submitted[-1].job, AgentJob)
        assert pool.submitted[-1].on_done_state == "AGENT_DONE"
        assert len(coordinator.in_flight) == 1
        assert id(item) in coordinator._leases
        assert not coordinator.timers
