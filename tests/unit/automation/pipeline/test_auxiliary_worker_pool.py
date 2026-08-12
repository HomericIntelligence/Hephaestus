"""Behavior tests for the closed auxiliary worker pool."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.jobs import AgentJob


class _Host:
    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        return AthenaSkillResult(
            kind=str(request.kind), receipt={"worker": threading.current_thread().name}
        )


def test_learning_workers_are_distinct_and_reject_generic_agents(tmp_path: Path) -> None:
    """The lane has distinct workers and no generic agent dispatch surface."""
    assert (
        importlib.util.find_spec("hephaestus.automation.pipeline.auxiliary_worker_pool") is not None
    )
    auxiliary = importlib.import_module("hephaestus.automation.pipeline.auxiliary_worker_pool")
    completions: queue.Queue = queue.Queue(maxsize=1)
    pool = auxiliary.AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=_Host(),
    )
    request = AthenaSkillRequest(
        kind="learn",
        repo="Hephaestus",
        issue=2705,
        agent="codex",
        model="",
        cwd=tmp_path,
        timeout_s=10,
    )
    handle = pool.submit(AthenaSkillJob(request=request), "DONE")
    done, result = completions.get(timeout=2)

    assert done is handle
    assert result.ok
    learning_worker = result.value.receipt["worker"]
    assert learning_worker.startswith("hephaestus-learning-worker-")
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="hephaestus-pipeline-worker-"
    ) as main:
        main_worker = main.submit(lambda: threading.current_thread().name).result()
    assert learning_worker != main_worker
    with pytest.raises(TypeError, match="does not accept"):
        pool.submit(AgentJob("r", 1, "codex", "", lambda: "", tmp_path, 1), "DONE")
    pool.shutdown(mark_interrupted=False)


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
