"""Tests for one source lease through initial rebase publication."""

import json
import queue
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceError, SourceWorkspaceManager
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.test_source_worktree import _git, _repository


@pytest.mark.parametrize("publication_failed", [False, True])
def test_initial_rebase_reuses_the_source_lease_and_keeps_its_recorded_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication_failed: bool
) -> None:
    """A later publication failure must retain the single lease's local successor."""
    root, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, head, branch="writer")
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    job = GitJob(
        "repo",
        "rebase",
        30,
        workspace=binding,
        kwargs={
            "repo_root": str(root),
            "cwd": str(binding.cwd),
            "issue_number": 42,
            "branch": "writer",
            "rebase_reason": "implementation_start",
            "expected_head_sha": head,
            "direct_scope_reservation": {"branch": "writer", "base_sha": head},
        },
    )
    path, identity = pool._initial_start_identity(job)
    path.with_suffix(".pending.json").write_text(json.dumps({**identity, "head_sha": head}))
    original_context = SourceWorkspaceManager.implementation_local_commit
    acquisitions: list[int] = []

    @contextmanager
    def source_context(
        current: SourceWorkspaceManager, *args: Any, **kwargs: Any
    ) -> Iterator[Callable[[str], WorkspaceBinding]]:
        acquisitions.append(1)
        if len(acquisitions) != 1:
            raise SourceWorkspaceError("initial rebase acquired its source lease twice")
        with original_context(current, *args, **kwargs) as record:
            yield record

    monkeypatch.setattr(SourceWorkspaceManager, "implementation_local_commit", source_context)
    local_heads: list[str] = []

    def rebase(_job: GitJob, *, record_source: Callable[[str], WorkspaceBinding]) -> JobResult:
        (binding.cwd / "tracked.txt").write_text("controlled rebase result\n")
        _git(binding.cwd, "commit", "-am", "controlled source change")
        revised = _git(binding.cwd, "rev-parse", "HEAD")
        local_heads.append(revised)
        record_source(revised)
        return JobResult(ok=True, value={"head_sha": revised, "rebased": True})

    observed: list[str] = []

    def publish(
        _job: GitJob, result: JobResult, _path: Path, _identity: dict[str, object]
    ) -> JobResult:
        observed.append(manager._require_receipt(42, SourceLane.IMPLEMENTATION).revision)
        if publication_failed:
            return JobResult(
                ok=False,
                error="publication unknown",
                value={"head_sha": local_heads[-1], "initial_reservation_pending": True},
            )
        return result

    monkeypatch.setattr(pool, "_git_rebase_once", rebase)
    monkeypatch.setattr(pool, "_publish_initial_reservation", publish)
    try:
        result = pool._run_git(job)
        assert acquisitions == [1]
        assert local_heads and observed == local_heads
        assert result.ok is not publication_failed
        if publication_failed:
            assert result.error == "publication unknown"
        receipt = manager._require_receipt(42, SourceLane.IMPLEMENTATION)
        assert receipt.revision == local_heads[-1]
        assert result.value["source_receipt"] == receipt.to_dict()
        assert result.value["source_workspace"] == receipt.to_binding(root).to_dict()
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "shutdown"])
def test_runtime_cache_lock_keeps_the_build_job_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    """A busy runtime cache must stop before the worker starts a copy."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n")
    cache_root = tmp_path / worker_pool._HOST_RUNTIME_CACHE_DIRNAME
    cache_root.mkdir()
    lock_path = cache_root / "fixture-runtime.lock"
    shutdown = threading.Event()
    pool = WorkerPool(
        size=1, shutdown=shutdown, completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    job = BuildTestJob(
        "repo",
        checkout,
        ("uv", "run", "pytest"),
        20 if cancel else 1,
        expected_head_sha="a" * 40,
        immutable_source=True,
    )
    attempted = threading.Event()
    copies: list[bool] = []

    @contextmanager
    def observed_lock(path: Path, **kwargs: Any) -> Iterator[None]:
        if path == lock_path:
            attempted.set()
        with file_lock(path, **kwargs):
            yield

    def copy(*_args: Any, **_kwargs: Any) -> None:
        copies.append(True)
        raise OSError("fixture stopped the unexpected runtime copy")

    monkeypatch.setattr(worker_pool, "file_lock", observed_lock)
    monkeypatch.setattr(sys, "prefix", str(runtime))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        worker_pool, "_host_runtime_fingerprint", lambda _runtime: "fixture-runtime"
    )
    monkeypatch.setattr(worker_pool, "_checkout_matches_immutable_head", lambda *_args: None)
    monkeypatch.setattr(worker_pool, "_trusted_uv_executable", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        worker_pool,
        "_validated_git_exec_path",
        lambda: (
            "/usr/bin/git",
            Path("/usr/libexec/git-core"),
            Path("/usr/share/git-core/gitconfig"),
        ),
    )
    monkeypatch.setattr(worker_pool, "_trusted_git_executable", lambda: "/usr/bin/git")
    monkeypatch.setattr(shutil, "copytree", copy)
    results: list[JobResult] = []
    thread = threading.Thread(target=lambda: results.append(pool._run(job)), daemon=True)
    try:
        with file_lock(lock_path, require_exclusive=True):
            thread.start()
            assert attempted.wait(timeout=1)
            if cancel:
                shutdown.set()
            thread.join(timeout=2)
            stopped_while_locked = not thread.is_alive()
        thread.join(timeout=2)
        assert stopped_while_locked
        assert not copies
        assert len(results) == 1 and not results[0].ok
        if cancel:
            assert results[0].interrupted
    finally:
        thread.join(timeout=2)
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "shutdown"])
def test_initial_journal_lock_keeps_the_existing_job_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    """A held initial-start journal must not stall the queue worker."""
    root, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, head, branch="writer")
    shutdown = threading.Event()
    pool = WorkerPool(
        size=1, shutdown=shutdown, completion_q=queue.Queue(), lock_dir=tmp_path / "locks"
    )
    job = GitJob(
        "repo",
        "rebase",
        30,
        workspace=binding,
        deadline_s=time.monotonic() + (20 if cancel else 1),
        kwargs={
            "repo_root": str(root),
            "cwd": str(binding.cwd),
            "issue_number": 42,
            "branch": "writer",
            "rebase_reason": "manual",
            "expected_head_sha": head,
        },
    )
    path, _identity = pool._initial_start_identity(job)
    lock_path = path.with_suffix(".lock")
    attempted = threading.Event()

    @contextmanager
    def observed_lock(path: Path, **kwargs: Any) -> Iterator[None]:
        if path == lock_path:
            attempted.set()
        with file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(worker_pool, "file_lock", observed_lock)
    results: list[JobResult] = []
    thread = threading.Thread(target=lambda: results.append(pool._run_git(job)), daemon=True)
    try:
        with file_lock(lock_path, require_exclusive=True):
            thread.start()
            assert attempted.wait(timeout=1)
            if cancel:
                shutdown.set()
            thread.join(timeout=2)
            stopped_while_locked = not thread.is_alive()
        thread.join(timeout=2)
        assert stopped_while_locked
        assert len(results) == 1 and not results[0].ok
        if cancel:
            assert results[0].interrupted
    finally:
        thread.join(timeout=2)
        pool.shutdown(mark_interrupted=False)
