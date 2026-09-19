"""Test current validation policy without executing a container or a full suite."""

from __future__ import annotations

import queue
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import FIRST_PUBLICATION_CHECK_ARGV, GitJob
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import _PreparationDeadline
from tests.unit.automation.test_first_publication_store import _record


@pytest.mark.parametrize("configured", [None, ("wrong", "override")])
def test_hephaestus_recovery_requires_verified_check_only_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: tuple[str, ...] | None
) -> None:
    """A generic override cannot replace the fixed source-preserving checks."""
    repository = "HomericIntelligence/Hephaestus"
    original = _record(tmp_path)
    record = replace(
        original,
        repository=repository,
        scheduler_repository=repository,
        destination=f"https://github.com/{repository}.git",
        workspace=replace(original.workspace, repository=repository),
    )
    record.workspace.cwd.mkdir()
    calls: list[tuple[str, ...]] = []

    def execute(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        assert Path(kwargs["cwd"]) == record.workspace.cwd
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="runner unavailable")

    monkeypatch.setattr(worker_pool, "run_subprocess", execute)
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    job = GitJob(
        repo=repository,
        op="commit_push",
        timeout_s=60,
        workspace=record.workspace,
        kwargs={"publication_recovery_request_id": "e" * 32, "publication_test_argv": configured},
    )
    try:
        result = pool._validate_first_publication_recovery(
            job, record, _PreparationDeadline(60, lambda: 0, threading.Event())
        )
    finally:
        pool.shutdown()
    assert not result.ok
    assert result.stderr_tail == "runner unavailable"
    assert len(calls) == 1
    assert calls[0][-4:] == FIRST_PUBLICATION_CHECK_ARGV
    assert "hephaestus-required-check" in calls[0]
    assert record.workspace.revision in calls[0]
    assert "--rebuild" not in calls[0]
    assert isinstance(result.value, dict)
    assert result.value["argv"] == FIRST_PUBLICATION_CHECK_ARGV
    assert result.value["head_sha"] == record.workspace.revision
    assert result.value["tree_sha"] == record.tree_sha
    assert result.value["publication_recovery_request_id"] == "e" * 32
