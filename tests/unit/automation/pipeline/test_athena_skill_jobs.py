"""Tests for typed Athena skill jobs."""
# ruff: noqa: D103

from __future__ import annotations

import ast
import json
import queue
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_worker_persists_typed_athena_result_when_evidence_is_enabled(tmp_path: Path) -> None:
    """An evidence run receives the exact host-owned result from the queue worker."""

    class Executor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            return AthenaSkillResult(
                kind=request.kind,
                context="selected skills",
                receipt={"binding": "live"},
            )

    receipt_dir = tmp_path / "receipts"
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
        athena_skill_executor=Executor(),
        evidence_receipt_dir=receipt_dir,
    )
    try:
        result = pool._run(
            AthenaSkillJob(request=_request(), descr="advise"),
            claim_key="Hephaestus#42",
            claim_stage="planning",
        )
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is True
    receipts = list(receipt_dir.glob("*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text())
    assert payload["job_type"] == "athena"
    assert payload["claim_stage"] == "planning"
    assert payload["result"]["kind"] == "advise"
    assert payload["result"]["receipt"] == {"binding": "live"}


@pytest.mark.parametrize("kind", ["advise", "learn"])
def test_athena_skill_job_never_invokes_an_agent_harness(
    tmp_path: Path,
    kind: str,
) -> None:
    """The host contract is authoritative even when the selected agent is Pi."""
    calls: list[AthenaSkillRequest] = []

    class Executor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            calls.append(request)
            return AthenaSkillResult(
                kind=request.kind,
                context="selected skills",
                receipt={"ok": True},
            )

    request = _request(kind=kind, agent="pi")
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
                "hephaestus.automation.pipeline.worker_pool.resolve_agent",
                side_effect=AssertionError("Athena host work must not resolve a harness"),
            ) as resolve,
            patch(
                "hephaestus.automation.pipeline.worker_pool.run_agent_session",
                side_effect=AssertionError("Athena host work must not start a harness"),
            ) as start,
            patch(
                "hephaestus.automation.pipeline.worker_pool.resume_agent_session",
                side_effect=AssertionError("Athena host work must not resume a harness"),
            ) as resume,
        ):
            result = pool._run(AthenaSkillJob(request=request, descr=kind))
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is True
    assert calls == [request]
    resolve.assert_not_called()
    start.assert_not_called()
    resume.assert_not_called()


def test_worker_pool_has_no_athena_agent_dispatch_dependency() -> None:
    source = Path("hephaestus/automation/pipeline/worker_pool.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not any("athena_skill" in name and name.startswith("run_") for name in imported_names)


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
