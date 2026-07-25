"""Completion-channel saturation and signal-wake safety contracts (#2399)."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import admission as admission_mod, seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _coordinator(tmp_path: Path) -> tuple[Coordinator, FakeWorkerPool]:
    """Return a one-slot coordinator with a synchronous worker-pool stand-in."""
    pool = FakeWorkerPool()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            max_workers=1,
            parallel_repos=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=pool,
        install_signals=False,
    )
    return coordinator, pool


def test_signal_wake_never_blocks_on_a_full_completion_queue(tmp_path: Path) -> None:
    """Signal wake-up is a latch, not a queue write that can deadlock a handler."""
    coordinator, _pool = _coordinator(tmp_path)
    occupied = (object(), JobResult(ok=True, value="already queued"))
    coordinator.completion_q.put_nowait(occupied)
    callback = threading.Thread(target=coordinator._wake_completion_wait)

    callback.start()
    callback.join(timeout=0.2)
    blocked = callback.is_alive()
    try:
        still_queued = coordinator.completion_q.get_nowait()
    finally:
        if callback.is_alive():
            callback.join(timeout=1)

    assert not blocked
    assert still_queued is occupied
    assert coordinator.completion_q.empty()
    assert coordinator._completion_wakeup.is_set()


def test_completion_saturation_exits_failed_without_claiming_signal_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An internal completion fault is exit 1, never an exit-130 cancellation."""
    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda _repos, _issues, _prs: [])
    coordinator, pool = _coordinator(tmp_path)
    coordinator._completion_saturation.set()

    assert coordinator.run() == 1
    assert coordinator._fatal is True
    assert not coordinator.shutdown.is_set()
    assert pool.shutdown_calls == 1


class _BlockingWorkerPool(WorkerPool):
    """Real callback path with test-controlled worker completion."""

    def __init__(self, release: threading.Event, finished: threading.Event) -> None:
        super().__init__(size=1, shutdown=threading.Event(), completion_q=queue.Queue())
        self._release = release
        self._finished = finished

    def _run(self, job: Any, claim_key: str = "", claim_stage: str = "") -> JobResult:
        del job, claim_key, claim_stage
        assert self._release.wait(timeout=2)
        self._finished.set()
        return JobResult(ok=True)


class _OneBlockingJobStage:
    """Submit exactly one inert job so WorkerPool owns its real callback."""

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


class _PassStage:
    """Complete a freshly reseeded item without submitting an external job."""

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        del item, result, ctx

    def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
        del ctx
        return StageOutcome(Disposition.FINISH_PASS, "fresh source recovery")


def test_real_worker_saturation_is_durable_resumable_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A C=1 callback overflow fails safely and a fresh source can resume work."""
    release = threading.Event()
    finished = threading.Event()
    pool = _BlockingWorkerPool(release, finished)
    event_log = tmp_path / "events.jsonl"
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=[],
            parallel_repos=1,
            max_workers=1,
            event_log_path=event_log,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=pool,
        stages={StageName.PLANNING: _OneBlockingJobStage(tmp_path)},
        install_signals=False,
    )
    item = WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=71, stage=StageName.PLANNING)
    coordinator._push_item(item, StageName.PLANNING, enter=True)
    coordinator._drain_queues()
    assert len(coordinator.in_flight) == 1

    # Consume the only C=1 completion slot with an unrelated stale payload,
    # then let the real WorkerPool callback attempt publication.
    active_handle = next(iter(coordinator.in_flight))
    stale_handle = JobHandle(job=active_handle.job, on_done_state=active_handle.on_done_state)
    coordinator.completion_q.put_nowait((stale_handle, JobResult(ok=True)))
    release.set()
    assert finished.wait(timeout=1)
    assert coordinator._completion_saturation.wait(timeout=1)

    assert coordinator.run() == 1
    assert not coordinator.shutdown.is_set()
    assert item.result is not None
    assert item.result.reason == "resumable at planning"
    records = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    assert {record["event"] for record in records} >= {"completion_saturation", "resumable"}

    monkeypatch.setattr(
        seeding_mod,
        "seed_issue_from_github",
        lambda issue, _github: IssueFacts(
            number=issue,
            title="recover",
            body="",
            is_epic=False,
            labels={"state:needs-plan"},
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        ),
    )
    monkeypatch.setattr(admission_mod, "_filter_open_issues", lambda _repo, issues: list(issues))
    recovered = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[71],
            parallel_repos=1,
            max_workers=1,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    recovered.stages[StageName.PLANNING] = _PassStage()

    assert recovered.run() == 0
    assert any(
        candidate.issue == 71 and candidate.result and candidate.result.passed
        for candidate in recovered.items
    )
