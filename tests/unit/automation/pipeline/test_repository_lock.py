"""Tests for repository-scoped pipeline lock coordination."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
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
from hephaestus.utils.file_lock import ExclusiveLockUnavailableError, LockUnavailableError

_LOCK_PROCESS = r"""
import sys
import threading
import time
from pathlib import Path

from hephaestus.automation.pipeline.repository_lock import RepositoryOperationLock

lock_dir, marker, release = map(Path, sys.argv[1:])
lock = RepositoryOperationLock("owner/repo", lock_dir=lock_dir, shutdown=threading.Event())
with lock.acquire(operation=marker.name, timeout_s=10):
    marker.write_text("entered", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
raise SystemExit(0 if release.exists() else 2)
"""


def _wait_for(predicate: object, *, timeout_s: float = 5.0) -> None:
    """Wait until one test predicate returns true."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.01)
    pytest.fail("the repository-lock process did not reach its expected state")


def _owner_paths(lock_dir: Path) -> tuple[Path, Path, Path]:
    """Return the primary, owner-lock, and owner-record paths."""
    primary = repo_lock_path("owner/repo", lock_dir)
    return primary, Path(f"{primary}.owner.lock"), Path(f"{primary}.owner.json")


def _sidecar(repository: str = "owner/repo") -> dict[str, object]:
    """Return a valid owner record for external-holder tests."""
    return {
        "version": 1,
        "repository": repository,
        "operation": "commit_push",
        "process_id": os.getpid(),
        "acquisition_token": "a" * 32,
        "acquired_at": "2026-09-03T12:00:00Z",
    }


class TestRepositoryOperationLock:
    """Verify the three-layer repository lock contract."""

    @pytest.mark.skipif(os.name == "nt", reason="native descriptor locks require POSIX")
    @pytest.mark.parametrize("change", ["remove", "replace"])
    def test_existing_parent_lock_does_not_create_through_a_changed_path(
        self, tmp_path: Path, change: str
    ) -> None:
        """A common lock stays bound to its validated directory identity."""
        common = tmp_path / "common"
        common.mkdir()
        metadata = common.stat()
        lock_path = common / ".hephaestus-git-metadata.lock"
        lock = RepositoryOperationLock(
            "owner/repo",
            lock_path=lock_path,
            existing_parent_identity=(metadata.st_dev, metadata.st_ino),
        )
        retained = tmp_path / "retained"
        common.rename(retained)
        if change == "replace":
            common.mkdir()

        with pytest.raises(LockMetadataError):
            with lock.acquire(operation="fetch_main", timeout_s=1):
                pytest.fail("a changed common directory admitted the lock")

        if change == "remove":
            assert not common.exists()
        else:
            assert tuple(common.iterdir()) == ()
        assert tuple(retained.iterdir()) == ()

    @pytest.mark.parametrize(
        ("timeout_s", "passive_expires_first"),
        [(4.0, True), (9.0, True), (14.0, False)],
        ids=["passive-first", "equal", "operation-first"],
    )
    @pytest.mark.parametrize("absolute_wait", [False, True])
    def test_contention_uses_the_first_deadline(
        self,
        tmp_path: Path,
        timeout_s: float,
        passive_expires_first: bool,
        absolute_wait: bool,
    ) -> None:
        """A clock jump must not change which admission budget expired first."""
        now = [1.0]
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path, monotonic=lambda: now[0])
        with lock.acquire_in_process(operation="holder", timeout_s=30):
            thread_lock = lock.lock

            def delayed_contention(*, blocking: bool) -> bool:
                acquired = thread_lock.acquire(blocking=blocking)
                assert not acquired
                now[0] = 20.0
                return acquired

            with patch.object(lock, "lock", wraps=thread_lock) as proxy:
                proxy.acquire.side_effect = delayed_contention
                expected = LockTimeoutError if passive_expires_first else subprocess.TimeoutExpired
                with pytest.raises(expected) as raised:
                    with lock.acquire_in_process(
                        operation="waiter",
                        timeout_s=None if absolute_wait else timeout_s,
                        wait_deadline_s=1.0 + timeout_s if absolute_wait else None,
                        deadline_s=10.0,
                    ):
                        pytest.fail("an expired admission dispatched")

            if passive_expires_first:
                assert isinstance(raised.value, LockTimeoutError)
                assert raised.value.details["holder_operation"] == "holder"
                assert raised.value.details["holder_source"] == "in_process"
            assert lock.users == 1

        assert lock.users == 0

    def test_absolute_wait_deadline_does_not_restart_after_reservation(
        self, tmp_path: Path
    ) -> None:
        """Reservation time must not extend an existing passive wait deadline."""
        now = [1.0]
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path, monotonic=lambda: now[0])
        with lock.acquire_in_process(operation="holder", timeout_s=30):
            reserve = lock.reserve

            def late_reservation() -> None:
                reserve()
                now[0] = 6.0

            with (
                patch.object(lock, "reserve", side_effect=late_reservation),
                pytest.raises(LockTimeoutError) as raised,
            ):
                with lock.acquire_in_process(operation="waiter", wait_deadline_s=5.0):
                    pytest.fail("an expired passive admission dispatched")

            assert raised.value.details["holder_operation"] == "holder"
            assert raised.value.details["holder_source"] == "in_process"
            assert lock.users == 1

        assert lock.users == 0

    def test_publishes_and_removes_holder_record(self, tmp_path: Path) -> None:
        """A Git critical section publishes and removes its owner record."""
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

    def test_in_process_holder_is_verified(self, tmp_path: Path) -> None:
        """A waiter reports the active same-process holder."""
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
        entered = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with lock.acquire_in_process(operation="github_read", timeout_s=1):
                entered.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold)
        thread.start()
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(LockTimeoutError) as raised:
                with lock.acquire(operation="create_worktree", timeout_s=0.01):
                    pytest.fail("the waiter acquired the held lock")
        finally:
            release.set()
            thread.join(timeout=5)

        assert not thread.is_alive()
        assert raised.value.details["failure_kind"] == "lock_timeout"
        assert raised.value.details["holder_operation"] == "github_read"
        assert raised.value.details["holder_process_id"] == os.getpid()
        assert raised.value.details["holder_source"] == "in_process"

    def test_primary_timeout_requires_verified_owner_metadata(self, tmp_path: Path) -> None:
        """An old primary-lock holder without an owner record fails closed."""
        fcntl = pytest.importorskip("fcntl")
        primary, _owner_lock, _owner_record = _owner_paths(tmp_path)
        primary.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(primary, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
            with pytest.raises(LockMetadataError) as raised:
                with lock.acquire(operation="create_worktree", timeout_s=0.01):
                    pytest.fail("the primary lock was held")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        assert raised.value.details["holder_source"] == "owner_sidecar"

    def test_primary_timeout_reports_verified_external_holder(self, tmp_path: Path) -> None:
        """A primary timeout reports an active and valid owner record."""
        fcntl = pytest.importorskip("fcntl")
        primary, owner_lock, owner_record = _owner_paths(tmp_path)
        primary.parent.mkdir(parents=True, exist_ok=True)
        owner_record.write_text(json.dumps(_sidecar()) + "\n", encoding="utf-8")
        owner_record.chmod(0o600)
        primary_descriptor = os.open(primary, os.O_RDWR | os.O_CREAT, 0o600)
        owner_descriptor = os.open(owner_lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(primary_descriptor, fcntl.LOCK_EX)
            fcntl.flock(owner_descriptor, fcntl.LOCK_EX)
            lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
            with pytest.raises(LockTimeoutError) as raised:
                with lock.acquire(operation="create_worktree", timeout_s=0.01):
                    pytest.fail("the primary lock was held")
        finally:
            fcntl.flock(owner_descriptor, fcntl.LOCK_UN)
            fcntl.flock(primary_descriptor, fcntl.LOCK_UN)
            os.close(owner_descriptor)
            os.close(primary_descriptor)

        assert raised.value.details["holder_operation"] == "commit_push"
        assert raised.value.details["holder_process_id"] == os.getpid()
        assert raised.value.details["holder_source"] == "owner_sidecar"

    def test_primary_timeout_rejects_non_integer_record_version(self, tmp_path: Path) -> None:
        """A JSON number equal to one is not an integer schema version."""
        fcntl = pytest.importorskip("fcntl")
        primary, owner_lock, owner_record = _owner_paths(tmp_path)
        primary.parent.mkdir(parents=True, exist_ok=True)
        sidecar = _sidecar()
        sidecar["version"] = 1.0
        owner_record.write_text(json.dumps(sidecar) + "\n", encoding="utf-8")
        owner_record.chmod(0o600)
        primary_descriptor = os.open(primary, os.O_RDWR | os.O_CREAT, 0o600)
        owner_descriptor = os.open(owner_lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(primary_descriptor, fcntl.LOCK_EX)
            fcntl.flock(owner_descriptor, fcntl.LOCK_EX)
            lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
            with pytest.raises(LockMetadataError) as raised:
                with lock.acquire(operation="create_worktree", timeout_s=0.01):
                    pytest.fail("a non-integer record version was verified")
        finally:
            fcntl.flock(owner_descriptor, fcntl.LOCK_UN)
            fcntl.flock(primary_descriptor, fcntl.LOCK_UN)
            os.close(owner_descriptor)
            os.close(primary_descriptor)

        assert raised.value.details["holder_operation"] is None
        assert raised.value.details["holder_source"] == "owner_sidecar"

    def test_primary_timeout_rejects_non_exact_record_mode(self, tmp_path: Path) -> None:
        """A holder record with mode 0700 is not an owner-only data record."""
        fcntl = pytest.importorskip("fcntl")
        primary, owner_lock, owner_record = _owner_paths(tmp_path)
        primary.parent.mkdir(parents=True, exist_ok=True)
        owner_record.write_text(json.dumps(_sidecar()) + "\n", encoding="utf-8")
        owner_record.chmod(0o700)
        primary_descriptor = os.open(primary, os.O_RDWR | os.O_CREAT, 0o600)
        owner_descriptor = os.open(owner_lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(primary_descriptor, fcntl.LOCK_EX)
            fcntl.flock(owner_descriptor, fcntl.LOCK_EX)
            lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
            with pytest.raises(LockMetadataError) as raised:
                with lock.acquire(operation="create_worktree", timeout_s=0.01):
                    pytest.fail("a non-0600 record mode was verified")
        finally:
            fcntl.flock(owner_descriptor, fcntl.LOCK_UN)
            fcntl.flock(primary_descriptor, fcntl.LOCK_UN)
            os.close(owner_descriptor)
            os.close(primary_descriptor)

        assert raised.value.details["holder_operation"] is None
        assert raised.value.details["holder_source"] == "owner_sidecar"

    def test_unavailable_exclusive_lock_does_not_verify_a_stale_record(
        self,
        tmp_path: Path,
    ) -> None:
        """A missing lock capability fails closed without asserting contention."""
        _primary, _owner_lock, owner_record = _owner_paths(tmp_path)
        owner_record.parent.mkdir(parents=True, exist_ok=True)
        owner_record.write_text(json.dumps(_sidecar()) + "\n", encoding="utf-8")
        owner_record.chmod(0o600)
        now = [0.0]
        lock = RepositoryOperationLock(
            "owner/repo",
            lock_dir=tmp_path,
            monotonic=lambda: now[0],
        )

        def unavailable_lock(*_args: object, **_kwargs: object) -> None:
            now[0] = 1.0
            raise ExclusiveLockUnavailableError("exclusive locks unavailable")

        with (
            patch(
                "hephaestus.automation.pipeline.repository_lock.file_lock",
                side_effect=unavailable_lock,
            ),
            pytest.raises(LockMetadataError) as raised,
        ):
            with lock.acquire(operation="create_worktree", timeout_s=0.5):
                pytest.fail("an unavailable lock capability dispatched")

        assert raised.value.details["holder_operation"] is None
        assert raised.value.details["holder_source"] == "lock_metadata"

    def test_outer_primary_and_owner_wait_share_one_deadline(self, tmp_path: Path) -> None:
        """All three acquisitions use one monotonic deadline."""
        now = [0.0]
        calls: list[Path] = []
        lock = RepositoryOperationLock(
            "owner/repo",
            lock_dir=tmp_path,
            monotonic=lambda: now[0],
        )

        @contextmanager
        def controlled_file_lock(
            path: Path,
            *,
            blocking: bool,
            require_exclusive: bool,
        ) -> Iterator[None]:
            assert blocking is False
            assert require_exclusive is True
            calls.append(path)
            if path.name.endswith(".owner.lock"):
                now[0] = 1.0
                raise LockUnavailableError("held")
            now[0] = 0.75
            yield

        with patch(
            "hephaestus.automation.pipeline.repository_lock.file_lock",
            side_effect=controlled_file_lock,
        ):
            with pytest.raises(LockMetadataError):
                with lock.acquire(operation="create_worktree", timeout_s=1):
                    pytest.fail("the unavailable owner sentinel was acquired")

        assert calls[0].name == "git-owner_repo.lock"
        assert calls[1].name == "git-owner_repo.lock.owner.lock"

    def test_late_in_process_acquisition_prevents_dispatch(self, tmp_path: Path) -> None:
        """A process-lock success after the deadline does not dispatch."""
        now = [0.0]
        lock = RepositoryOperationLock(
            "owner/repo",
            lock_dir=tmp_path,
            monotonic=lambda: now[0],
        )

        def late_acquire(*, blocking: bool) -> bool:
            assert blocking is False
            now[0] = 1.0
            return True

        with patch.object(lock, "lock") as process_lock:
            process_lock.acquire.side_effect = late_acquire
            with pytest.raises(LockMetadataError):
                with lock.acquire_in_process(operation="read_current_plan", timeout_s=0.5):
                    pytest.fail("a late in-process lock dispatched")

        process_lock.release.assert_called_once_with()

    def test_late_primary_acquisition_prevents_owner_acquisition(self, tmp_path: Path) -> None:
        """A primary-lock success after the deadline does not acquire the owner lock."""
        now = [0.0]
        calls: list[Path] = []
        lock = RepositoryOperationLock(
            "owner/repo",
            lock_dir=tmp_path,
            monotonic=lambda: now[0],
        )

        @contextmanager
        def late_file_lock(
            path: Path,
            *,
            blocking: bool,
            require_exclusive: bool,
        ) -> Iterator[None]:
            assert blocking is False
            assert require_exclusive is True
            calls.append(path)
            now[0] = 1.0
            yield

        with (
            patch(
                "hephaestus.automation.pipeline.repository_lock.file_lock",
                side_effect=late_file_lock,
            ),
            pytest.raises(LockMetadataError),
        ):
            with lock.acquire(operation="commit_push", timeout_s=0.5):
                pytest.fail("a late primary lock dispatched")

        assert calls == [repo_lock_path("owner/repo", tmp_path)]

    def test_late_owner_record_publication_prevents_dispatch(self, tmp_path: Path) -> None:
        """A record publication after the deadline does not dispatch."""
        now = [0.0]
        lock = RepositoryOperationLock(
            "owner/repo",
            lock_dir=tmp_path,
            monotonic=lambda: now[0],
        )

        def late_record(_holder: object, _parent_fd: int) -> None:
            now[0] = 1.0

        with (
            patch.object(lock, "_prepare_owner_paths"),
            patch.object(lock, "_write_owner_record", side_effect=late_record),
            patch.object(lock, "_remove_matching_owner_record"),
            pytest.raises(LockMetadataError),
        ):
            with lock.acquire(operation="commit_push", timeout_s=0.5):
                pytest.fail("a late owner record dispatched")

        assert now[0] == 1.0

    def test_shutdown_prevents_dispatch(self, tmp_path: Path) -> None:
        """Shutdown stops acquisition before the critical section."""
        shutdown = threading.Event()
        shutdown.set()
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path, shutdown=shutdown)

        with pytest.raises(LockInterruptedError):
            with lock.acquire(operation="create_worktree", timeout_s=1):
                pytest.fail("shutdown did not stop acquisition")

    def test_stale_record_handoff_replaces_and_removes_record(self, tmp_path: Path) -> None:
        """A new holder replaces a regular stale record safely."""
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

    def test_owner_record_symlink_fails_closed(self, tmp_path: Path) -> None:
        """The lock does not follow or replace an owner-record link."""
        _primary, _owner_lock, owner_record = _owner_paths(tmp_path)
        owner_record.parent.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "target.json"
        target.write_text(json.dumps(_sidecar()) + "\n", encoding="utf-8")
        owner_record.symlink_to(target)
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)

        with pytest.raises(LockMetadataError):
            with lock.acquire(operation="create_worktree", timeout_s=1):
                pytest.fail("a linked owner record was accepted")

        assert owner_record.is_symlink()

    def test_owner_record_hardlink_fails_without_unlink(self, tmp_path: Path) -> None:
        """The lock does not unlink a multiply linked stale owner record."""
        _primary, _owner_lock, owner_record = _owner_paths(tmp_path)
        owner_record.parent.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "target.json"
        target.write_text(json.dumps(_sidecar()) + "\n", encoding="utf-8")
        target.chmod(0o600)
        os.link(target, owner_record)
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)

        with pytest.raises(LockMetadataError):
            with lock.acquire(operation="create_worktree", timeout_s=1):
                pytest.fail("a hard-linked owner record was accepted")

        assert owner_record.exists()
        assert target.stat().st_nlink == 2
        assert target.read_text(encoding="utf-8") == json.dumps(_sidecar()) + "\n"

    def test_cleanup_failure_does_not_replace_completed_operation(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A record cleanup error does not replace the operation result."""
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)

        with (
            patch.object(lock, "_remove_matching_owner_record", side_effect=OSError("cleanup")),
            caplog.at_level("WARNING"),
        ):
            with lock.acquire(operation="commit_push", timeout_s=1):
                completed = True

        assert completed is True
        assert "owner record cleanup failed" in caplog.text

    def test_body_error_is_not_reclassified_as_lock_metadata(self, tmp_path: Path) -> None:
        """The lock does not replace an operation failure with a lock failure."""
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)

        with pytest.raises(ValueError, match="operation failed"):
            with lock.acquire(operation="commit_push", timeout_s=1):
                raise ValueError("operation failed")

    def test_release_order_removes_record_before_lock_release(self, tmp_path: Path) -> None:
        """Release removes the record before the owner and primary locks."""
        lock = RepositoryOperationLock("owner/repo", lock_dir=tmp_path)
        events: list[str] = []

        @contextmanager
        def observed_file_lock(
            path: Path,
            *,
            blocking: bool,
            require_exclusive: bool,
        ) -> Iterator[None]:
            del blocking, require_exclusive
            layer = "owner" if path.name.endswith(".owner.lock") else "primary"
            events.append(f"{layer}_enter")
            try:
                yield
            finally:
                events.append(f"{layer}_exit")

        def remove(_token: str, _parent_fd: int | None = None) -> None:
            events.append("record_remove")

        with (
            patch(
                "hephaestus.automation.pipeline.repository_lock.file_lock",
                side_effect=observed_file_lock,
            ),
            patch.object(lock, "_prepare_owner_paths"),
            patch.object(lock, "_write_owner_record"),
            patch.object(lock, "_remove_matching_owner_record", side_effect=remove),
        ):
            with lock.acquire(operation="commit_push", timeout_s=1):
                events.append("dispatch")

        assert events == [
            "primary_enter",
            "owner_enter",
            "dispatch",
            "record_remove",
            "owner_exit",
            "primary_exit",
        ]

    def test_controlled_three_process_race_serializes_every_holder(self, tmp_path: Path) -> None:
        """Three processes enter the repository critical section one at a time."""
        lock_dir = tmp_path / "locks"
        markers = [tmp_path / f"holder-{index}" for index in range(3)]
        releases = [tmp_path / f"release-{index}" for index in range(3)]
        command = [sys.executable, "-c", _LOCK_PROCESS]
        processes: list[subprocess.Popen[str]] = []
        try:
            first = subprocess.Popen(
                [*command, str(lock_dir), str(markers[0]), str(releases[0])],
                cwd=Path(__file__).resolve().parents[4],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            processes.append(first)
            _wait_for(markers[0].exists)
            for index in (1, 2):
                processes.append(
                    subprocess.Popen(
                        [*command, str(lock_dir), str(markers[index]), str(releases[index])],
                        cwd=Path(__file__).resolve().parents[4],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            assert not markers[1].exists()
            assert not markers[2].exists()
            releases[0].touch()
            _wait_for(lambda: markers[1].exists() ^ markers[2].exists())
            second_index = 1 if markers[1].exists() else 2
            third_index = 2 if second_index == 1 else 1
            assert not markers[third_index].exists()
            releases[second_index].touch()
            _wait_for(markers[third_index].exists)
            releases[third_index].touch()
            for process in processes:
                _stdout, stderr = process.communicate(timeout=5)
                assert process.returncode == 0, stderr
        finally:
            for release in releases:
                release.touch(exist_ok=True)
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=2)
