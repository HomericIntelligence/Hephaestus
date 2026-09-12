"""Check operation limits at the shared Git metadata lock."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_runtime
from hephaestus.automation.source_worktree import (
    SourceWorkspaceManager,
    SourceWorkspacePreparationError,
    _PreparationDeadline,
)
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.utils.file_lock import LockUnavailableError, file_lock


def _git(repo: Path, *args: str) -> str:
    """Run Git in a disposable repository."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("operation", ["handoff", "create"])
@pytest.mark.parametrize("stop", ["deadline", "cancellation"])
@pytest.mark.parametrize("phase", ["waiting", "acquired"])
def test_writer_metadata_lock_observes_operation_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    stop: str,
    phase: str,
) -> None:
    """Stop before writer state can change, then release both lock references."""
    fcntl = pytest.importorskip("fcntl")
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    head = _git(repo, "rev-parse", "HEAD")
    source = SourceWorkspaceManager(repo, repository="example/project")
    writer = source.prepare(42, SourceLane.IMPLEMENTATION, head, branch="writer")
    receipt = source._receipt_path(42, SourceLane.IMPLEMENTATION)
    receipt_before = receipt.read_bytes()
    worktrees_before = _git(repo, "worktree", "list", "--porcelain")
    manager = WorktreeManager(repo_root=repo, base_dir=source.base_dir, base_branch=head)
    metadata_path = WorktreeManager.git_metadata_lock_path(repo)
    metadata_inode = metadata_path.stat().st_ino
    ready = threading.Event()
    proceed = threading.Event()
    observed = threading.Event()
    complete = threading.Event()
    shutdown = threading.Event()
    clock = [time.monotonic()]
    expires_at = clock[0] + 60.0
    monkeypatch.setattr(
        git_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep)
    )
    deadline = _PreparationDeadline(
        expires_at, lambda: clock[0], shutdown if stop == "cancellation" else None
    )
    failures: list[BaseException] = []
    effects: list[str] = []
    real_flock = fcntl.flock

    def request_stop() -> None:
        if stop == "deadline":
            clock[0] = expires_at + 1.0
        else:
            shutdown.set()

    def observe_flock(fd: int, flags: int) -> None:
        target = (
            threading.current_thread() is worker
            and proceed.is_set()
            and flags & fcntl.LOCK_EX
            and os.fstat(fd).st_ino == metadata_inode
        )
        if target and phase == "waiting":
            observed.set()
        real_flock(fd, flags)
        if target and phase == "acquired":
            request_stop()
            observed.set()

    def run_operation() -> None:
        try:
            if operation == "create":
                with source.implementation_writer_handoff(42, deadline=deadline) as handoff:
                    ready.set()
                    assert proceed.wait(5.0)
                    manager.create_worktree(
                        42,
                        "writer",
                        source_lane="impl",
                        implementation_writer_handoff=handoff,
                    )
                    effects.append("created")
            else:
                ready.set()
                assert proceed.wait(5.0)
                with source.implementation_writer_handoff(42, deadline=deadline):
                    effects.append("entered")
        except (Exception, KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            failures.append(exc)
        finally:
            complete.set()

    worker = threading.Thread(target=run_operation, daemon=True)
    monkeypatch.setattr(fcntl, "flock", observe_flock)
    worker.start()
    try:
        assert ready.wait(5.0)
        lock = (
            file_lock(metadata_path, require_exclusive=True)
            if phase == "waiting"
            else nullcontext()
        )
        with lock:
            proceed.set()
            assert observed.wait(5.0)
            if phase == "waiting":
                request_stop()
            stopped_before_release = complete.wait(1.0)
    finally:
        proceed.set()
        worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert stopped_before_release, "The operation waited beyond its stop request."
    assert effects == []
    assert len(failures) == 1
    cause = failures[0]
    if stop == "deadline":
        assert isinstance(cause, (SourceWorkspacePreparationError, subprocess.TimeoutExpired))
    else:
        assert isinstance(cause, InterruptedError)
    assert receipt.read_bytes() == receipt_before
    assert not source._transition_path(42).exists()
    assert (writer.cwd / "tracked.txt").read_text(encoding="utf-8") == "original\n"
    assert _git(repo, "worktree", "list", "--porcelain") == worktrees_before
    with (
        file_lock(metadata_path, blocking=False, require_exclusive=True),
        file_lock(
            source._lane_lock_path(42, SourceLane.IMPLEMENTATION),
            blocking=False,
            require_exclusive=True,
        ),
    ):
        pass


@pytest.mark.parametrize("stop", ["deadline", "cancellation"])
def test_operation_lock_rejects_stop_before_open(tmp_path: Path, stop: str) -> None:
    """An operation that has stopped must not open the metadata sentinel."""
    path = tmp_path / "metadata.lock"
    shutdown = threading.Event()
    shutdown.set()
    deadline = time.monotonic() - 1.0 if stop == "deadline" else None
    expected = subprocess.TimeoutExpired if stop == "deadline" else InterruptedError
    with (
        git_runtime.operation_deadline(
            deadline, shutdown=shutdown if stop == "cancellation" else None
        ),
        pytest.raises(expected),
        git_runtime.operation_file_lock(path),
    ):
        pytest.fail("An operation that has stopped entered the metadata lock.")
    assert not path.exists()


@pytest.mark.parametrize("bounded", [False, True])
def test_operation_lock_does_not_repeat_body_failure(tmp_path: Path, bounded: bool) -> None:
    """A lock error from the protected operation must leave the body once."""
    pytest.importorskip("fcntl")
    path = tmp_path / "metadata.lock"
    failure = LockUnavailableError("A different resource is unavailable.")
    calls: list[str] = []
    deadline = time.monotonic() + 5.0 if bounded else None
    with pytest.raises(LockUnavailableError) as raised:
        with git_runtime.operation_deadline(deadline), git_runtime.operation_file_lock(path):
            calls.append("body")
            raise failure
    assert raised.value is failure
    assert calls == ["body"]
    with file_lock(path, blocking=False, require_exclusive=True):
        pass


def test_operation_lock_keeps_manual_blocking_default(tmp_path: Path) -> None:
    """A caller without an operation limit can wait until the lock is free."""
    pytest.importorskip("fcntl")
    path = tmp_path / "metadata.lock"
    started = threading.Event()
    completed = threading.Event()
    effects: list[str] = []

    def acquire() -> None:
        started.set()
        with git_runtime.operation_file_lock(path):
            effects.append("entered")
        completed.set()

    worker = threading.Thread(target=acquire, daemon=True)
    with file_lock(path, require_exclusive=True):
        worker.start()
        assert started.wait(1.0)
        remained_blocked = not completed.wait(0.1)
    worker.join(timeout=1.0)
    assert remained_blocked
    assert not worker.is_alive()
    assert completed.is_set()
    assert effects == ["entered"]
