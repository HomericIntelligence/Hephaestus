"""Tests for final workspace ownership during auxiliary learning execution."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.automation.source_worktree import (
    SourceWorkspaceManager,
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
    _PreparationDeadline,
)
from tests.unit.automation.test_source_worktree import _repository


class RecordingHost:
    """Record host execution without an external provider."""

    def __init__(self) -> None:
        self.requests: list[AthenaSkillRequest] = []
        self.cancel_calls = 0

    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        """Record the final request passed by the worker."""
        self.requests.append(request)
        return AthenaSkillResult(kind="learn")

    def cancel(self) -> None:
        """Record worker shutdown."""
        self.cancel_calls += 1


def _learning_job(cwd: Path, binding: WorkspaceBinding | None) -> AthenaSkillJob:
    """Build one learning request with an explicit workspace field."""
    return AthenaSkillJob(
        request=AthenaSkillRequest(
            kind="learn",
            repo="example/project",
            issue=7,
            agent="codex",
            model="test",
            cwd=cwd,
            timeout_s=10,
            workspace=binding,
        )
    )


def _run_auxiliary(job: AthenaSkillJob, host: RecordingHost) -> JobResult:
    """Return the result from the bounded auxiliary completion channel."""
    completions: queue.Queue[tuple[JobHandle, JobResult]] = queue.Queue(maxsize=1)
    pool = AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=host,
    )
    try:
        submitted = pool.submit(job, "DONE")
        handle, result = completions.get(timeout=10)
        assert handle is submitted
        return result
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("invalid_binding", ["missing", "stale"])
def test_auxiliary_learning_rejects_missing_or_stale_source_ownership(
    tmp_path: Path, invalid_binding: str
) -> None:
    """Invalid source authority stops the request before host execution."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare_bounded(7, SourceLane.IMPLEMENTATION, first)
    selected = binding if invalid_binding == "stale" else None
    if invalid_binding == "stale":
        manager.prepare_bounded(7, SourceLane.IMPLEMENTATION, second)
    host = RecordingHost()

    result = _run_auxiliary(_learning_job(binding.cwd, selected), host)

    assert not result.ok
    assert host.requests == []
    assert result.error is not None and "workspace" in result.error.lower()


def test_auxiliary_learning_keeps_source_lease_until_host_returns(tmp_path: Path) -> None:
    """A second preparation cannot change the source during host execution."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare_bounded(7, SourceLane.IMPLEMENTATION, first)

    class PreparingHost(RecordingHost):
        blocked_cause: SourceWorkspacePreparationCause | None = None

        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            try:
                manager.prepare_bounded(7, SourceLane.IMPLEMENTATION, second)
            except SourceWorkspacePreparationError as error:
                self.blocked_cause = error.cause
            return super().execute(request)

    host = PreparingHost()

    result = _run_auxiliary(_learning_job(binding.cwd, binding), host)

    assert result.ok
    assert host.blocked_cause is SourceWorkspacePreparationCause.LANE_LOCK_UNAVAILABLE
    with manager.acquire(binding):
        assert manager._require_receipt(7, SourceLane.IMPLEMENTATION).revision == first
    assert len(host.requests) == 1


def test_auxiliary_host_receives_only_time_left_after_source_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source admission and host execution share one time limit."""
    repo, first, _second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare_bounded(7, SourceLane.IMPLEMENTATION, first)
    clock = [100.0]

    @contextmanager
    def delayed_acquire(
        _manager: SourceWorkspaceManager,
        selected: WorkspaceBinding,
        *,
        allowed_tools: str = "",
        deadline: _PreparationDeadline | None = None,
    ) -> Iterator[Path]:
        assert allowed_tools == "Read,Glob,Grep"
        assert deadline is not None
        clock[0] += 4.0
        yield selected.cwd

    monkeypatch.setattr(SourceWorkspaceManager, "acquire", delayed_acquire)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    host = RecordingHost()

    result = _run_auxiliary(_learning_job(binding.cwd, binding), host)

    assert result.ok
    assert len(host.requests) == 1
    assert host.requests[0].timeout_s == 6
