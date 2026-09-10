"""Behavior tests for the closed auxiliary worker pool."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.test_source_worktree import _repository


class _Host:
    def __init__(self) -> None:
        """Record host execution and cancellation."""
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.calls: list[AthenaSkillRequest] = []

    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        """Return the identity of the actual host worker."""
        self.started.set()
        self.calls.append(request)
        return AthenaSkillResult(
            kind=request.kind, receipt={"worker": threading.current_thread().name}
        )

    def cancel(self) -> None:
        """Record the pool's cancellation request."""
        self.cancelled.set()


@pytest.fixture
def learning_request(tmp_path: Path) -> AthenaSkillRequest:
    """Bind host work to a real prepared source workspace."""
    root, revision, _second = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="HomericIntelligence/Hephaestus")
    binding = manager.prepare_bounded(1, SourceLane.IMPLEMENTATION, revision)
    return AthenaSkillRequest(
        kind="learn",
        repo="HomericIntelligence/Hephaestus",
        issue=1,
        agent="codex",
        model="",
        cwd=binding.cwd,
        timeout_s=10,
        workspace=binding,
    )


def test_learning_workers_are_distinct_and_reject_generic_agents(
    tmp_path: Path, learning_request: AthenaSkillRequest
) -> None:
    """The lane has distinct workers and no generic agent dispatch surface."""
    assert (
        importlib.util.find_spec("hephaestus.automation.pipeline.auxiliary_worker_pool") is not None
    )
    auxiliary = importlib.import_module("hephaestus.automation.pipeline.auxiliary_worker_pool")
    completions = CompletionQueue(maxsize=1)
    host = _Host()
    pool = auxiliary.AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=host,
    )
    try:
        handle = pool.submit(AthenaSkillJob(request=learning_request), "DONE")
        done, result = completions.get(timeout=2)

        assert done is handle
        assert result.ok
        assert host.started.is_set()
        assert host.calls[0].workspace == learning_request.workspace
        learning_worker = result.value.receipt["worker"]
        assert learning_worker.startswith("hephaestus-learning-worker-")
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hephaestus-pipeline-worker-"
        ) as main:
            main_worker = main.submit(lambda: threading.current_thread().name).result(timeout=1)
        assert learning_worker != main_worker
        with pytest.raises(TypeError, match="does not accept"):
            pool.submit(AgentJob("r", 1, "codex", "", lambda: "", tmp_path, 1), "DONE")
    finally:
        pool.shutdown(mark_interrupted=False)
    assert host.cancelled.is_set()


def test_learning_dependency_graph_excludes_agent_runtime_and_pi() -> None:
    """The closed auxiliary module has no provider or generic-worker import."""
    spec = importlib.util.find_spec("hephaestus.automation.pipeline.auxiliary_worker_pool")
    assert spec is not None and spec.origin is not None
    module_path = Path(spec.origin)
    imported = {
        alias.name
        for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
    )

    assert not any(
        "worker_pool" in name and "auxiliary_worker_pool" not in name for name in imported
    )
    assert not any("agents.runtime" in name or "pi_" in name for name in imported)


def test_graceful_coordinator_signal_does_not_rewrite_completed_result(
    learning_request: AthenaSkillRequest,
) -> None:
    """A graceful stop lets active host work publish its real result."""
    auxiliary = importlib.import_module("hephaestus.automation.pipeline.auxiliary_worker_pool")
    graceful = threading.Event()
    forced = threading.Event()
    completions = CompletionQueue(maxsize=1)

    class BlockingHost(_Host):
        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            self.started.set()
            assert graceful.wait(timeout=2)
            return super().execute(request)

    host = BlockingHost()
    pool = auxiliary.AuxiliaryWorkerPool(
        size=1,
        shutdown=forced,
        completion_q=completions,
        athena_skill_executor=host,
    )
    try:
        pool.submit(AthenaSkillJob(request=learning_request), "DONE")
        assert host.started.wait(timeout=1)
        graceful.set()
        _handle, result = completions.get(timeout=2)

        assert result.ok
        assert not result.interrupted
        assert len(host.calls) == 1
        assert host.calls[0].workspace == learning_request.workspace
    finally:
        graceful.set()
        pool.shutdown(mark_interrupted=False)


def test_forced_shutdown_publishes_cancelled_queued_job(
    learning_request: AthenaSkillRequest,
) -> None:
    """A queued host job becomes an explicit resumable completion."""
    auxiliary = importlib.import_module("hephaestus.automation.pipeline.auxiliary_worker_pool")
    forced = threading.Event()
    completions = CompletionQueue(maxsize=2)

    class BlockingHost(_Host):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()

        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            self.started.set()
            assert self.release.wait(timeout=2)
            return super().execute(request)

    host = BlockingHost()
    pool = auxiliary.AuxiliaryWorkerPool(
        size=1, shutdown=forced, completion_q=completions, athena_skill_executor=host
    )
    shutdown_done = threading.Event()

    def stop_pool() -> None:
        pool.shutdown()
        shutdown_done.set()

    try:
        running = pool.submit(AthenaSkillJob(request=learning_request), "DONE")
        assert host.started.wait(timeout=1)
        queued = pool.submit(AthenaSkillJob(request=learning_request), "DONE")
        shutdown_thread = threading.Thread(target=stop_pool)
        shutdown_thread.start()
        assert not shutdown_done.wait(timeout=0.05)
        host.release.set()
        assert shutdown_done.wait(timeout=2)
        shutdown_thread.join(timeout=1)
        results = dict(completions.get(timeout=2) for _ in range(2))

        assert set(results) == {running, queued}
        assert all(result.interrupted and not result.ok for result in results.values())
        assert results[queued].error == "interrupted_before_start"
        assert len(host.calls) == 1
        assert host.calls[0].workspace == learning_request.workspace
        assert host.cancelled.is_set()
        assert completions.empty()
    finally:
        host.release.set()
        pool.shutdown()
