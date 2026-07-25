"""RED coordinator contract for a full next-stage queue transition (#2399).

The queue object already exposes capacity-reserving leases.  This test pins
the coordinator-level use of that contract: a completed stage action must not
be run again, dropped, or treated as an interrupt merely because its next
stage is temporarily full.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.queues import StageQueue
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _AdvanceAfterJob:
    """Run one durable side effect after a synthetic worker completion."""

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd
        self._submitted = False
        self.side_effects = 0

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        del item, result, ctx

    def step(self, item: WorkItem, ctx: Any) -> JobRequest | StageOutcome:
        del ctx
        if not self._submitted:
            self._submitted = True
            return JobRequest(
                AgentJob(
                    repo=item.repo,
                    issue=item.issue or 0,
                    agent="codex",
                    model="stub",
                    prompt_builder=lambda: "stub",
                    cwd=self._cwd,
                    timeout_s=1,
                ),
                "AFTER_JOB",
            )
        self.side_effects += 1
        return StageOutcome(Disposition.ADVANCE, "durable side effect complete")


class _FinishStage:
    """Make the destination runnable if it is drained after a retry."""

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        del item, result, ctx

    def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
        del item, ctx
        return StageOutcome(Disposition.FINISH_PASS, "destination consumed")


class _AlwaysJob:
    """Submit a worker job for every item without completing it inline."""

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        del item, result, ctx

    def step(self, item: WorkItem, ctx: Any) -> JobRequest:
        del ctx
        return JobRequest(
            AgentJob(
                repo=item.repo,
                issue=item.issue or 0,
                agent="codex",
                model="stub",
                prompt_builder=lambda: "stub",
                cwd=self._cwd,
                timeout_s=1,
            ),
            "AFTER_JOB",
        )


class _PendingPool(FakeWorkerPool):
    """Record submissions while deliberately withholding completions."""

    def submit(self, job: Any, on_done_state: StageName, **kwargs: Any) -> Any:
        from hephaestus.automation.pipeline.jobs import JobHandle

        handle = JobHandle(job=job, on_done_state=on_done_state)
        self.submitted.append(handle)
        self.submitted_claims.append((kwargs.get("claim_key", ""), kwargs.get("claim_stage", "")))
        return handle


def _issue(issue: int, stage: StageName) -> WorkItem:
    """Build an item whose identity is stable in coordinator event assertions."""
    return WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=issue, stage=stage)


def test_full_next_stage_retains_completed_transition_until_retry(tmp_path: Path) -> None:
    """A full destination retains a completed ADVANCE without replaying its side effect."""
    source_stage = _AdvanceAfterJob(tmp_path)
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            parallel_repos=1,
            # Two valid live items are needed for this targeted full-next-stage
            # fallback. Production queues have capacity C, so replace only the
            # destination below with a smaller test double to exercise the
            # defensive handoff path that remains lossless under an unexpected
            # downstream capacity reduction.
            max_workers=2,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        stages={
            StageName.PLANNING: source_stage,
            StageName.PLAN_REVIEW: _FinishStage(),
        },
        install_signals=False,
    )
    source = _issue(1, StageName.PLANNING)
    blocker = _issue(2, StageName.PLAN_REVIEW)
    coordinator.queues[StageName.PLAN_REVIEW] = StageQueue(capacity=1)

    # Dispatch source normally so the coordinator owns its source-stage slot.
    coordinator._push_item(source, StageName.PLANNING, enter=True)
    coordinator._drain_queues()
    assert len(coordinator.in_flight) == 1

    # A different completion can occupy the next stage while source is in flight.
    coordinator._push_item(blocker, StageName.PLAN_REVIEW, enter=True)
    destination = coordinator.queues[StageName.PLAN_REVIEW]
    source_queue = coordinator.queues[StageName.PLANNING]
    assert destination.snapshot() == [blocker]

    coordinator._drain_completions()

    # The completed side effect cannot be dropped, replayed, or turned into an interrupt.
    assert source_stage.side_effects == 1
    assert source.result is None
    assert not coordinator.shutdown.is_set()
    assert not coordinator._fatal
    assert destination.snapshot() == [blocker]
    assert source_queue.occupancy == 1

    # Once the next stage opens, the coordinator retries the retained transition exactly once.
    assert destination.pop() is blocker
    coordinator._drain_queues()

    target_pushes = [
        event
        for event in coordinator.event_log
        if event == ("push", StageName.PLAN_REVIEW.value, "repo-a#1")
    ]
    assert target_pushes == [("push", StageName.PLAN_REVIEW.value, "repo-a#1")]
    assert source_stage.side_effects == 1
    assert source_queue.occupancy == 0
    assert not coordinator.shutdown.is_set()
    assert not coordinator._fatal


def test_stage_leases_allow_parallel_worker_submissions_up_to_capacity(tmp_path: Path) -> None:
    """Two ready items dispatch before either completion when C is two."""
    pool = _PendingPool()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            parallel_repos=1,
            max_workers=2,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=pool,
        stages={StageName.PLANNING: _AlwaysJob(tmp_path)},
        install_signals=False,
    )
    first = _issue(1, StageName.PLANNING)
    second = _issue(2, StageName.PLANNING)
    coordinator._push_item(first, StageName.PLANNING, enter=True)
    coordinator._push_item(second, StageName.PLANNING, enter=True)

    coordinator._drain_queues()

    assert len(pool.submitted) == 2
    assert len(coordinator.in_flight) == 2
    assert not coordinator.queues[StageName.PLANNING].snapshot()
    assert {id(lease.item) for lease in coordinator._leases.values()} == {id(first), id(second)}
