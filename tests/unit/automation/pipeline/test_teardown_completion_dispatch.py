"""Keep completed work separate from new dispatch during fatal teardown."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hephaestus.agents.workspace import WorkspaceBinding
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.github_jobs import (
    GitHubJob,
    RateBudgetRead,
    ReadRateBudgetRequest,
)
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import AgentJob, BuildTestJob
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stage_results import JobRequest
from hephaestus.automation.pipeline.stages.base import Stage, StageContext
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


@dataclass
class _NextJobStage(Stage):
    """Request one more job after the initial completion."""

    job: BuildTestJob
    completions: list[JobResult] = field(default_factory=list)
    steps: int = 0

    def on_enter(self, item: WorkItem, ctx: StageContext) -> None:
        """Keep the test's current state."""

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Retain the result that the coordinator delivered."""
        self.completions.append(result)

    def step(self, item: WorkItem, ctx: StageContext) -> JobRequest | StageOutcome:
        """Finish after one additional job, without an unbounded test loop."""
        self.steps += 1
        if item.state == "NEXT":
            return JobRequest(self.job, on_done_state="DONE")
        return StageOutcome(Disposition.FINISH_PASS, "completed")


@pytest.mark.parametrize("completion_kind", ["stage", "quota"])
@pytest.mark.parametrize("fatal", [True, False], ids=["fatal_teardown", "active_drain"])
def test_ready_completion_does_not_dispatch_after_fatal_pool_shutdown(
    tmp_path: Path, completion_kind: str, fatal: bool
) -> None:
    """Fatal cleanup parks ready work; an active drain can submit its next job."""
    main = FakeWorkerPool()
    auxiliary = FakeWorkerPool()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            rate_guard_enabled=False,
            enable_learn=False,
            no_advise=True,
        ),
        github=FakeStageGitHub(),
        **fake_worker_factories(main, auxiliary),
        install_signals=False,
    )
    job = BuildTestJob("repo", tmp_path, ("unused",), 5)
    stage = _NextJobStage(job)
    coordinator.stages[StageName.IMPLEMENTATION] = stage
    item = WorkItem(
        repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.IMPLEMENTATION, state="WAIT"
    )
    claim_test_item(coordinator, item)
    if completion_kind == "quota":
        pending = AgentJob(
            "repo",
            1,
            "claude",
            "default",
            lambda: "unused",
            tmp_path,
            5,
            workspace=WorkspaceBinding.external(tmp_path),
        )
        item.payload["_pending_agent_request"] = JobRequest(pending, "NEXT")
        quota_request = ReadRateBudgetRequest(coordinator._monotonic() + 5)
        main.queue_result(JobResult(ok=True, value=RateBudgetRead(quota_request, 5000, 0)))
        coordinator._submit_ready_job(
            item,
            JobRequest(
                GitHubJob("repo", tmp_path, quota_request, descr="read_rate_budget"),
                "RATE_BUDGET",
            ),
        )
    else:
        coordinator._submit_ready_job(item, JobRequest(job, "NEXT"))
    assert len(main.submitted) == 1
    assert coordinator.completion_q.qsize() == 1
    assert not coordinator.shutdown.is_set()
    try:
        if fatal:
            coordinator._fatal = True
            coordinator._shutdown_pool()
            assert len(main.submitted) == 1, "Fatal cleanup submitted work to the closed pool"
            assert stage.steps == 0
            assert item.result is not None and item.result.reason.startswith("resumable at ")
            assert not coordinator.shutdown.is_set()
            assert not coordinator.in_flight
        else:
            coordinator._drain_completions()
            assert len(main.submitted) == (3 if completion_kind == "quota" else 2)
            assert stage.steps == 2
            assert item.result is not None and item.result.passed
            assert main.shutdown_calls == 0
    finally:
        coordinator._shutdown_pool()
