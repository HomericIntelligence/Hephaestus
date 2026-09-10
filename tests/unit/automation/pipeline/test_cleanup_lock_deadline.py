"""Cleanup lock waits must retain the auxiliary job's time limit."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import source_worktree
from hephaestus.automation.pipeline import git_cleanup
from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.test_source_worktree import _repository


@pytest.mark.parametrize("lock_kind", ["source", "metadata"])
@pytest.mark.parametrize("stop_reason", ["shutdown", "timeout"])
def test_auxiliary_cleanup_stops_while_its_lock_remains_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_kind: str,
    stop_reason: str,
) -> None:
    """A stopped cleanup keeps the worktree and its source receipt unchanged."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="example/project")
    binding = manager.prepare(7, SourceLane.REVIEW, revision)
    receipt_path = manager._receipt_path(7, SourceLane.REVIEW)
    original_receipt = receipt_path.read_bytes()
    shutdown = threading.Event()
    completion_q = CompletionQueue(maxsize=1)
    main_pool = WorkerPool(
        size=1,
        shutdown=shutdown,
        completion_q=CompletionQueue(maxsize=1),
        lock_dir=tmp_path / "locks",
    )
    auxiliary = AuxiliaryWorkerPool(
        size=1,
        shutdown=shutdown,
        completion_q=completion_q,
        athena_skill_executor=None,
        cleanup_runner=main_pool.run_cleanup_git,
    )
    lock_path = (
        manager._lane_lock_path(7, SourceLane.REVIEW)
        if lock_kind == "source"
        else WorktreeManager.git_metadata_lock_path(root)
    )
    attempted = threading.Event()

    @contextmanager
    def observed_lock(path: Path, **kwargs: Any) -> Iterator[None]:
        if path == lock_path:
            attempted.set()
        with file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(
        source_worktree if lock_kind == "source" else git_cleanup,
        "file_lock",
        observed_lock,
    )
    kwargs: dict[str, object] = {
        "worktree_path": str(binding.cwd),
        "repo_root": str(root),
        "issue_number": 7,
        "expected_head": revision,
        "expected_detached": True,
    }
    if lock_kind == "source":
        kwargs["source_lane"] = SourceLane.REVIEW.value
    job = GitJob(
        "example/project",
        "remove_worktree",
        10,
        deadline_s=time.monotonic() + (10 if stop_reason == "shutdown" else 0.5),
        kwargs=kwargs,
    )
    with file_lock(lock_path, require_exclusive=True):
        try:
            handle = auxiliary.submit(job, "CLEANUP_DONE")
            assert attempted.wait(timeout=1), "Cleanup did not reach the held lock"
            if stop_reason == "shutdown":
                auxiliary.shutdown()
            completed_handle, result = completion_q.get(timeout=1.5)
            assert completed_handle is handle
            assert not result.ok
            if stop_reason == "shutdown":
                assert result.interrupted
            else:
                assert result.error in {"timeout", "lock_timeout"}
            assert receipt_path.read_bytes() == original_receipt
            assert binding.cwd.is_dir()
        finally:
            auxiliary.shutdown()
            main_pool.shutdown()
