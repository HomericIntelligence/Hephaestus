"""Keep an explicit Fleet build selection out of local execution.

The real public pool owns dispatch, worker identity, evidence and completion.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from queue import Empty

import pytest

from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.config.child_environments import build_python_phase_env


@pytest.mark.parametrize("immutable", [False, True], ids=["ordinary", "immutable"])
def test_selected_build_without_capability_never_runs_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, immutable: bool
) -> None:
    """Refuse selected work before either legacy execution path can prepare it."""
    source = tmp_path / "source"
    source.mkdir()
    marker = source / "local-execution"
    prepared: list[Path] = []
    real_environment = build_python_phase_env

    def observe_environment(cwd: Path) -> dict[str, str]:
        prepared.append(cwd)
        return real_environment(cwd)

    monkeypatch.setattr(worker_pool, "build_python_phase_env", observe_environment)
    completions = CompletionQueue(maxsize=2)
    receipts = tmp_path / "receipts"
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        lock_dir=tmp_path / "locks",
        evidence_receipt_dir=receipts,
    )
    job = BuildTestJob(
        repo="example/project",
        cwd=source,
        argv=(
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
            str(marker),
        ),
        timeout_s=5,
        immutable_source=immutable,
        fleet_context_id="admitted-context",
    )
    try:
        handle = pool.submit(job, StageName.IMPLEMENTATION, claim_key="example/project#7")
        completed, result = completions.get(timeout=8)
        assert completed is handle
        assert result.ok is False, "the selected remote build ran through the local path"
        assert result.error == "fleet_build_capability_unavailable"
        assert not result.interrupted
        assert prepared == [], "the selected remote build prepared a local environment"
        assert not marker.exists(), "the selected remote build started a local command"
        assert result.worker_id.startswith("hephaestus-pipeline-worker")
        rows = [json.loads(path.read_text()) for path in receipts.glob("*.json")]
        assert len(rows) == 1
        assert rows[0]["claim_key"] == "example/project#7"
        assert rows[0]["succeeded"] is False
        assert "fleet_collection" not in rows[0]
        with pytest.raises(Empty):
            completions.get_nowait()
    finally:
        pool.shutdown()


@pytest.mark.parametrize("immutable", [False, True], ids=["ordinary", "immutable"])
def test_build_without_fleet_selection_keeps_legacy_execution(
    tmp_path: Path, immutable: bool
) -> None:
    """Keep local execution and the immutable-head guard when Fleet is absent."""
    marker = tmp_path / "local-execution"
    completions = CompletionQueue(maxsize=2)
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        lock_dir=tmp_path / "locks",
    )
    job = BuildTestJob(
        repo="example/project",
        cwd=tmp_path,
        argv=(
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
            str(marker),
        ),
        timeout_s=5,
        immutable_source=immutable,
    )
    try:
        handle = pool.submit(job, StageName.IMPLEMENTATION)
        completed, result = completions.get(timeout=8)
        assert completed is handle
        if immutable:
            assert result.ok is False
            assert result.error == "immutable_source_requires_full_head_sha"
            assert not marker.exists()
        else:
            assert result.ok is True
            assert marker.is_file()
        assert result.worker_id.startswith("hephaestus-pipeline-worker")
        with pytest.raises(Empty):
            completions.get_nowait()
    finally:
        pool.shutdown()
