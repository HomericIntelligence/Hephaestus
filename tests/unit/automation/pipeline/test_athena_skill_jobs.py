"""Tests for typed Athena skill jobs."""
# ruff: noqa: D103

from __future__ import annotations

import ast
import json
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool
from hephaestus.automation.pipeline.jobs import JobHandle
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.test_source_worktree import _repository


@pytest.fixture
def source_binding(tmp_path: Path) -> WorkspaceBinding:
    """Prepare the real source lease used by host requests."""
    repo, revision, _second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="HomericIntelligence/Hephaestus")
    return manager.prepare_bounded(42, SourceLane.IMPLEMENTATION, revision)


def _request(
    workspace: WorkspaceBinding, kind: str = "advise", *, agent: str = "claude"
) -> AthenaSkillRequest:
    return AthenaSkillRequest(
        kind=kind,
        repo="HomericIntelligence/Hephaestus",
        issue=42,
        agent=agent,
        model="default",
        cwd=workspace.cwd,
        timeout_s=60,
        workspace=workspace,
        payload={"issue_title": "title"},
    )


def test_job_is_immutable_and_exposes_worker_fields(source_binding: WorkspaceBinding) -> None:
    job = AthenaSkillJob(request=_request(source_binding), descr="advise")

    assert job.repo == "HomericIntelligence/Hephaestus"
    assert job.issue == 42
    assert job.timeout_s == 60
    with pytest.raises(FrozenInstanceError):
        job.descr = "mutated"  # type: ignore[misc]


def test_worker_dispatches_athena_skill_job_to_injected_executor(
    tmp_path: Path, source_binding: WorkspaceBinding
) -> None:
    completion_q = CompletionQueue(maxsize=1)
    calls: list[AthenaSkillRequest] = []
    cancelled = threading.Event()

    class Executor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            calls.append(request)
            return AthenaSkillResult(
                kind=request.kind,
                context="selected skills",
                receipt={"ok": True},
            )

        def cancel(self) -> None:
            cancelled.set()

    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path,
        athena_skill_executor=Executor(),
    )
    request = _request(source_binding)
    try:
        handle = pool.submit(AthenaSkillJob(request=request, descr="advise"), "DONE")
        completed, result = completion_q.get(timeout=10)
        assert completed is handle
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is True
    assert result.value == AthenaSkillResult(
        kind="advise",
        context="selected skills",
        receipt={"ok": True},
    )
    assert len(calls) == 1
    assert 0 < calls[0].timeout_s <= request.timeout_s
    assert calls == [replace(request, timeout_s=calls[0].timeout_s)]
    assert cancelled.is_set()


def test_worker_persists_typed_athena_result_when_evidence_is_enabled(
    tmp_path: Path, source_binding: WorkspaceBinding
) -> None:
    """An evidence run receives the exact host-owned result from the queue worker."""
    cancelled = threading.Event()

    class Executor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            return AthenaSkillResult(
                kind=request.kind,
                context="selected skills",
                receipt={"binding": "live"},
            )

        def cancel(self) -> None:
            cancelled.set()

    receipt_dir = tmp_path / "receipts"
    completion_q = CompletionQueue(maxsize=1)
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path,
        athena_skill_executor=Executor(),
        evidence_receipt_dir=receipt_dir,
    )
    try:
        handle = pool.submit(
            AthenaSkillJob(request=_request(source_binding), descr="advise"),
            "DONE",
            claim_key="Hephaestus#42",
            claim_stage="planning",
        )
        completed, result = completion_q.get(timeout=10)
        assert completed is handle
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
    assert cancelled.is_set()


@pytest.mark.parametrize("kind", ["advise", "learn"])
def test_athena_skill_job_never_invokes_an_agent_harness(
    tmp_path: Path,
    kind: str,
    source_binding: WorkspaceBinding,
) -> None:
    """The host contract is authoritative even when the selected agent is Pi."""
    calls: list[AthenaSkillRequest] = []
    cancelled = threading.Event()

    class Executor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            calls.append(request)
            return AthenaSkillResult(
                kind=request.kind,
                context="selected skills",
                receipt={"ok": True},
            )

        def cancel(self) -> None:
            cancelled.set()

    request = _request(source_binding, kind=kind, agent="pi")
    completion_q = CompletionQueue(maxsize=1)
    pool: WorkerPool | AuxiliaryWorkerPool
    if kind == "learn":
        pool = AuxiliaryWorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=completion_q,
            athena_skill_executor=Executor(),
        )
    else:
        pool = WorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=completion_q,
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
            handle = pool.submit(AthenaSkillJob(request=request, descr=kind), "DONE")
            completed, result = completion_q.get(timeout=10)
            assert completed is handle
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is True
    assert len(calls) == 1
    assert 0 < calls[0].timeout_s <= request.timeout_s
    assert calls == [replace(request, timeout_s=calls[0].timeout_s)]
    assert cancelled.is_set()
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


def test_worker_converts_athena_executor_failure_to_bounded_job_error(
    source_binding: WorkspaceBinding,
) -> None:
    cancelled = threading.Event()

    class BrokenExecutor:
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            del request
            raise RuntimeError("backend unavailable")

        def cancel(self) -> None:
            cancelled.set()

    completion_q = CompletionQueue(maxsize=1)
    pool = AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        athena_skill_executor=BrokenExecutor(),
    )
    try:
        handle = pool.submit(
            AthenaSkillJob(request=_request(source_binding, kind="learn"), descr="learn"),
            "DONE",
        )
        completed, result = completion_q.get(timeout=10)
        assert completed is handle
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is False
    assert result.error is not None
    assert "backend unavailable" in result.error
    assert cancelled.is_set()


def test_job_handle_accepts_athena_skill_job(source_binding: WorkspaceBinding) -> None:
    handle = JobHandle(
        job=AthenaSkillJob(request=_request(source_binding), descr="advise"),
        on_done_state=StageName.PLANNING,
    )

    assert isinstance(handle.job, AthenaSkillJob)
