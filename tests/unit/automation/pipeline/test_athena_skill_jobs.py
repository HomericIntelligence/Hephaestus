"""Tests for typed Athena skill jobs."""
# ruff: noqa: D103

from __future__ import annotations

import queue
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.agents.runtime import AgentRunResult
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.jobs import JobHandle, JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool


def _request(kind: str = "advise", *, agent: str = "claude") -> AthenaSkillRequest:
    return AthenaSkillRequest(
        kind=kind,
        repo="HomericIntelligence/Hephaestus",
        issue=42,
        agent=agent,
        model="default",
        cwd=Path("/tmp/worktree"),
        timeout_s=60,
        payload={"issue_title": "title"},
    )


def test_job_is_immutable_and_exposes_worker_fields() -> None:
    job = AthenaSkillJob(request=_request(), descr="advise")

    assert job.repo == "HomericIntelligence/Hephaestus"
    assert job.issue == 42
    assert job.timeout_s == 60
    with pytest.raises(FrozenInstanceError):
        job.descr = "mutated"  # type: ignore[misc]


def test_worker_dispatches_athena_skill_job_to_injected_executor(tmp_path: Path) -> None:
    completion_q: queue.Queue[tuple[JobHandle, JobResult]] = queue.Queue()
    calls: list[AthenaSkillRequest] = []

    class Executor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            calls.append(request)
            return AthenaSkillResult(
                kind=request.kind,
                context="selected skills",
                receipt={"ok": True},
            )

    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path,
        athena_skill_executor=Executor(),
    )
    try:
        result = pool._run(AthenaSkillJob(request=_request(), descr="advise"))
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is True
    assert result.value == AthenaSkillResult(
        kind="advise",
        context="selected skills",
        receipt={"ok": True},
    )
    assert calls == [_request()]


def test_pi_athena_skill_runs_receipt_proven_command_before_host_enforcement(
    tmp_path: Path,
) -> None:
    """Pi jobs invoke their proven skill command while the host owns enforcement."""
    calls: list[AthenaSkillRequest] = []

    class Executor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            calls.append(request)
            return AthenaSkillResult(
                kind=request.kind,
                context="selected skills",
                receipt={"ok": True},
            )

    request = _request(agent="pi")
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
        athena_skill_executor=Executor(),
    )
    try:
        with (
            patch(
                "hephaestus.automation.pipeline.worker_pool.run_agent_athena_skill",
                return_value=AgentRunResult(stdout="Pi skill completed", stderr=""),
            ) as run_skill,
        ):
            result = pool._run(AthenaSkillJob(request=request, descr="advise"))
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is True
    assert calls == [request]
    run_skill.assert_called_once()
    assert run_skill.call_args.args[0] == "advise"
    assert run_skill.call_args.kwargs["agent"] == "pi"
    assert run_skill.call_args.kwargs["cwd"] == request.cwd
    assert run_skill.call_args.kwargs["timeout"] == request.timeout_s
    assert run_skill.call_args.kwargs["model"] == request.model


def test_worker_converts_athena_executor_failure_to_bounded_job_error(tmp_path: Path) -> None:
    class BrokenExecutor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            del request
            raise RuntimeError("backend unavailable")

    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
        athena_skill_executor=BrokenExecutor(),
    )
    try:
        result = pool._run(AthenaSkillJob(request=_request(kind="learn"), descr="learn"))
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is False
    assert result.error is not None
    assert "backend unavailable" in result.error


def test_job_handle_accepts_athena_skill_job() -> None:
    handle = JobHandle(
        job=AthenaSkillJob(request=_request(), descr="advise"),
        on_done_state=StageName.PLANNING,
    )

    assert isinstance(handle.job, AthenaSkillJob)
