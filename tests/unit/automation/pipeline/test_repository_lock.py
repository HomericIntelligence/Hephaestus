"""Tests for repository-scoped pipeline lock coordination."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation.pipeline.repository_lock import (
    LockInterruptedError,
    LockMetadataError,
    LockTimeoutError,
    RepositoryOperationLock,
    repo_lock_path,
)
from hephaestus.utils.file_lock import LockUnavailableError


def _owner_paths(lock_dir: Path) -> tuple[Path, Path, Path]:
    """Return primary, owner-lock, and owner-record paths."""
    primary = repo_lock_path("owner/repo", lock_dir)
    return primary, Path(f"{primary}.owner.lock"), Path(f"{primary}.owner.json")


def _sidecar(repository: str = "owner/repo") -> dict[str, object]:
    """Return a valid owner sidecar payload for external-holder tests."""
    return {
        "version": 1,
        "repository": repository,
        "operation": "commit_push",
        "process_id": os.getpid(),
        "acquisition_token": "a" * 32,
        "acquired_at": "2026-09-03T12:00:00Z",
    }


def test_repository_lock_publishes_and_removes_holder_record(tmp_path: Path) -> None:
    """A Git critical section publishes bounded metadata and cleans it up."""
    lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
    primary, owner_lock, owner_record = _owner_paths(tmp_path)

    with lock.acquire(operation="commit_push", timeout_s=1):
        assert primary.is_file()
        assert owner_lock.is_file()
        payload = json.loads(owner_record.read_text(encoding="utf-8"))
        assert set(payload) == {
            "version",
            "repository",
            "operation",
            "process_id",
            "acquisition_token",
            "acquired_at",
        }
        assert payload["operation"] == "commit_push"
        assert payload["process_id"] == os.getpid()
        assert payload["repository"] == "owner/repo"
        assert payload["version"] == 1
        assert owner_record.stat().st_mode & 0o777 == 0o600

    assert not owner_record.exists()
    assert primary.is_file()
    assert owner_lock.is_file()


def test_in_process_timeout_reports_verified_holder(tmp_path: Path) -> None:
    """An in-process waiter receives the active operation and process data."""
    lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with lock.acquire_in_process(operation="github_read"):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=5)

    with pytest.raises(LockTimeoutError) as raised:
        with lock.acquire(operation="create_worktree", timeout_s=0):
            pytest.fail("the in-process waiter acquired the held lock")

    assert raised.value.details == {
        "failure_kind": "lock_timeout",
        "repository": "owner/repo",
        "waiting_operation": "create_worktree",
        "waiting_process_id": os.getpid(),
        "holder_operation": "github_read",
        "holder_process_id": os.getpid(),
        "holder_acquired_at": raised.value.details["holder_acquired_at"],
        "holder_source": "in_process",
        "wait_duration_s": raised.value.details["wait_duration_s"],
    }
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_primary_timeout_requires_verified_owner_metadata(tmp_path: Path) -> None:
    """A primary-lock holder without a sidecar fails closed."""
    fcntl = pytest.importorskip("fcntl")
    primary, _owner_lock, _owner_record = _owner_paths(tmp_path)
    primary.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(primary, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
        with pytest.raises(LockMetadataError) as raised:
            with lock.acquire(operation="create_worktree", timeout_s=0):
                pytest.fail("the primary lock was held by another owner")
        assert raised.value.failure_kind == "lock_metadata_error"
        assert raised.value.details["holder_source"] == "owner_sidecar"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_primary_timeout_reports_valid_external_holder(tmp_path: Path) -> None:
    """A primary-lock timeout includes a strictly validated owner sidecar."""
    fcntl = pytest.importorskip("fcntl")
    primary, owner_lock, owner_record = _owner_paths(tmp_path)
    primary.parent.mkdir(parents=True, exist_ok=True)
    owner_record.write_text(json.dumps(_sidecar()) + "\n", encoding="utf-8")
    owner_record.chmod(0o600)
    primary_fd = os.open(primary, os.O_RDWR | os.O_CREAT, 0o600)
    owner_fd = os.open(owner_lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(primary_fd, fcntl.LOCK_EX)
        fcntl.flock(owner_fd, fcntl.LOCK_EX)
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
        with pytest.raises(LockTimeoutError) as raised:
            with lock.acquire(operation="create_worktree", timeout_s=0):
                pytest.fail("the primary lock was held by another owner")
        assert raised.value.details["holder_operation"] == "commit_push"
        assert raised.value.details["holder_process_id"] == os.getpid()
        assert raised.value.details["holder_source"] == "owner_sidecar"
    finally:
        fcntl.flock(owner_fd, fcntl.LOCK_UN)
        fcntl.flock(primary_fd, fcntl.LOCK_UN)
        os.close(owner_fd)
        os.close(primary_fd)


def test_stale_sidecar_is_replaced_after_primary_acquisition(tmp_path: Path) -> None:
    """A new holder invalidates an old regular sidecar before publishing."""
    _primary, _owner_lock, owner_record = _owner_paths(tmp_path)
    owner_record.parent.mkdir(parents=True, exist_ok=True)
    stale = _sidecar()
    stale["acquisition_token"] = "b" * 32
    owner_record.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    owner_record.chmod(0o600)
    lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)

    with lock.acquire(operation="create_worktree", timeout_s=1):
        current = json.loads(owner_record.read_text(encoding="utf-8"))
        assert current["acquisition_token"] != "b" * 32
        assert current["operation"] == "create_worktree"

    assert not owner_record.exists()


def test_owner_metadata_symlink_fails_closed(tmp_path: Path) -> None:
    """A symlink at the owner-record path is not followed or replaced."""
    _primary, _owner_lock, owner_record = _owner_paths(tmp_path)
    owner_record.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_sidecar()) + "\n", encoding="utf-8")
    owner_record.symlink_to(target)
    lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)

    with pytest.raises(LockMetadataError):
        with lock.acquire(operation="create_worktree", timeout_s=1):
            pytest.fail("a symlinked owner record was accepted")
    assert owner_record.is_symlink()


def test_shutdown_prevents_repository_dispatch(tmp_path: Path) -> None:
    """Shutdown interrupts lock acquisition before the critical section."""
    shutdown = threading.Event()
    shutdown.set()
    lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path, shutdown=shutdown)

    with pytest.raises(LockInterruptedError):
        with lock.acquire(operation="create_worktree", timeout_s=1):
            pytest.fail("shutdown did not stop repository acquisition")


def test_primary_and_owner_wait_use_one_deadline(tmp_path: Path) -> None:
    """The primary and owner polls use one shared monotonic deadline."""
    times = [0.0, 0.0, 0.6, 0.6, 1.1]
    lock = RepositoryOperationLock(
        "owner/repo",
        lock_dir=tmp_path,
        monotonic=lambda: times.pop(0) if times else 1.1,
    )
    calls: list[tuple[Path, bool, bool]] = []

    @contextmanager
    def unavailable_file_lock(
        path: Path,
        *,
        blocking: bool,
        require_exclusive: bool,
    ) -> Iterator[None]:
        calls.append((path, blocking, require_exclusive))
        raise LockUnavailableError("held")
        yield

    with patch(
        "hephaestus.automation.pipeline.repository_lock.file_lock",
        side_effect=unavailable_file_lock,
    ):
        with pytest.raises(LockMetadataError):
            with lock.acquire(operation="create_worktree", timeout_s=1):
                pytest.fail("the unavailable lock was acquired")

    assert calls
    assert all(not blocking and exclusive for _, blocking, exclusive in calls)
