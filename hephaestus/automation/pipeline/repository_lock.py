"""Coordinate repository-scoped locks for pipeline operations.

The primary file lock protects shared Git metadata. The in-process lock avoids
same-process file-lock ambiguity. The owner sentinel and record provide bounded
holder data when a waiting operation reaches its separate lock deadline.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.utils.file_lock import (
    ExclusiveLockUnavailableError,
    LockUnavailableError,
    file_lock,
)
from hephaestus.utils.helpers import get_repo_root

__all__ = [
    "DEFAULT_GIT_LOCK_TIMEOUT_S",
    "LockInterruptedError",
    "LockMetadataError",
    "LockTimeoutError",
    "RepositoryLockError",
    "RepositoryOperationLock",
    "repo_lock_path",
]

logger = logging.getLogger(__name__)

DEFAULT_GIT_LOCK_TIMEOUT_S = 7200
_POLL_S = 0.1
_OWNER_RECORD_VERSION = 1
_OWNER_RECORD_MAX_BYTES = 4096
_OWNER_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
_OWNER_RECORD_KEYS = frozenset(
    {
        "version",
        "repository",
        "operation",
        "process_id",
        "acquisition_token",
        "acquired_at",
    }
)


@dataclass(frozen=True)
class _FileIdentity:
    """Identify one file without retaining its content."""

    device: int
    inode: int
    file_type: int


@dataclass(frozen=True)
class _Holder:
    """Store verified holder data inside the lock implementation."""

    repository: str
    operation: str
    process_id: int
    acquisition_token: str
    acquired_at: str


class RepositoryLockError(RuntimeError):
    """Base error for a repository-lock acquisition failure."""

    def __init__(self, failure_kind: str, details: dict[str, object]) -> None:
        """Store a stable class and a closed diagnostic record."""
        self.failure_kind = failure_kind
        self.details = dict(details)
        super().__init__(failure_kind)


class LockTimeoutError(RepositoryLockError):
    """Raised when verified lock contention exceeds its wait budget."""


class LockMetadataError(RepositoryLockError):
    """Raised when active holder data cannot be verified safely."""


class LockInterruptedError(RepositoryLockError):
    """Raised when shutdown interrupts repository-lock acquisition."""


def repo_lock_path(repo: str, lock_dir: Path | None = None) -> Path:
    """Return the stable primary repository-lock path."""
    directory = lock_dir or get_repo_root() / DEFAULT_STATE_DIR / "locks"
    return directory / f"git-{repo.replace('/', '_')}.lock"


def _validate_admission(
    operation: str,
    timeout_s: float | None,
    deadline_s: float | None,
    wait_deadline_s: float | None,
) -> None:
    """Validate admission inputs before the lock reserves any resources."""
    if not isinstance(operation, str) or not operation or len(operation) > 200:
        raise ValueError("operation must contain between 1 and 200 characters")
    if timeout_s is not None and (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or timeout_s < 0
    ):
        raise ValueError("timeout_s must be a finite non-negative number")
    for name, value in (("deadline_s", deadline_s), ("wait_deadline_s", wait_deadline_s)):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a finite positive monotonic time")


def _admission_deadlines(
    started: float,
    timeout_s: float | None,
    wait_deadline_s: float | None,
    deadline_s: float | None,
) -> tuple[float | None, float | None]:
    """Select the first contention deadline. Passive expiry wins a tie."""
    passive = None if timeout_s is None else started + timeout_s
    if wait_deadline_s is not None:
        passive = wait_deadline_s if passive is None else min(passive, wait_deadline_s)
    if deadline_s is not None and (passive is None or deadline_s < passive):
        return deadline_s, deadline_s
    return passive, None


def _identity(metadata: os.stat_result) -> _FileIdentity:
    """Return stable identity fields for one file-system object."""
    return _FileIdentity(metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _failure_fields(
    *,
    failure_kind: str,
    repository: str,
    operation: str,
    wait_duration_s: float,
    holder: _Holder | None,
    holder_source: str | None,
) -> dict[str, object]:
    """Build the closed, output-free lock diagnostic schema."""
    return {
        "failure_kind": failure_kind,
        "repository": repository,
        "waiting_operation": operation,
        "waiting_process_id": os.getpid(),
        "holder_operation": holder.operation if holder is not None else None,
        "holder_process_id": holder.process_id if holder is not None else None,
        "holder_acquired_at": holder.acquired_at if holder is not None else None,
        "holder_source": holder_source,
        "wait_duration_s": round(max(wait_duration_s, 0.0), 3),
    }


def _open_parent(path: Path) -> int:
    """Open and bind one directory without following its final component."""
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("repository-lock directory is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(metadata) or not stat.S_ISDIR(opened.st_mode):
            raise RuntimeError("repository-lock directory changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_regular_at(parent_fd: int, name: str) -> tuple[bytes, _FileIdentity]:
    """Read one bounded owner-only regular file through a bound directory."""
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > _OWNER_RECORD_MAX_BYTES
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("repository-lock owner record is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            _identity(opened) != _identity(metadata)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise RuntimeError("repository-lock owner record changed")
        payload = os.read(descriptor, _OWNER_RECORD_MAX_BYTES + 1)
        if len(payload) > _OWNER_RECORD_MAX_BYTES or os.read(descriptor, 1):
            raise RuntimeError("repository-lock owner record is too large")
    finally:
        os.close(descriptor)
    return payload, _identity(opened)


class RepositoryOperationLock:
    """Coordinate one repository's in-process and cross-process operations."""

    def __init__(
        self,
        repository: str,
        *,
        lock_dir: Path | None = None,
        shutdown: threading.Event | None = None,
        on_idle: Callable[[RepositoryOperationLock], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        """Initialize the lock and its bounded holder state."""
        if not isinstance(repository, str) or not repository or len(repository) > 256:
            raise ValueError("repository must contain between 1 and 256 characters")
        self.repository = repository
        self.lock = threading.Lock()
        self._state_guard = threading.Lock()
        self._users = 0
        self._holder: _Holder | None = None
        self._lock_path = repo_lock_path(repository, lock_dir)
        self._owner_lock_path = Path(f"{self._lock_path}.owner.lock")
        self._owner_record_path = Path(f"{self._lock_path}.owner.json")
        self._shutdown = shutdown or threading.Event()
        self._on_idle = on_idle
        self._monotonic = monotonic
        self._wall_time = wall_time

    @property
    def users(self) -> int:
        """Return the current holder and waiter count."""
        with self._state_guard:
            return self._users

    def reserve(self) -> None:
        """Reserve this lock before a pool releases its cache guard."""
        with self._state_guard:
            self._users += 1

    def release_reservation(self) -> None:
        """Release one cache-safe lock reservation."""
        with self._state_guard:
            self._users -= 1
            if self._users < 0:
                raise RuntimeError("repository-lock user count became negative")
            idle = self._users == 0
        if idle and self._on_idle is not None:
            self._on_idle(self)

    @contextmanager
    def acquire_in_process(
        self,
        *,
        operation: str,
        timeout_s: float | None = None,
        deadline_s: float | None = None,
        wait_deadline_s: float | None = None,
        reserved: bool = False,
    ) -> Iterator[None]:
        """Acquire the in-process lock and publish its local holder.

        Absolute wait and operation deadlines remain unchanged. For verified
        contention, the first deadline determines the failure type and passive
        expiry wins a tie. An expired operation prevents free admission.
        """
        with self._acquire(
            operation=operation,
            timeout_s=timeout_s,
            deadline_s=deadline_s,
            wait_deadline_s=wait_deadline_s,
            include_file_lock=False,
            reserved=reserved,
        ):
            yield

    @contextmanager
    def acquire(
        self,
        *,
        operation: str,
        timeout_s: float,
        deadline_s: float | None = None,
        wait_deadline_s: float | None = None,
        include_file_lock: bool = True,
        reserved: bool = False,
    ) -> Iterator[None]:
        """Acquire all requested layers under one monotonic deadline.

        The passive timeout starts here. Absolute wait and operation deadlines
        remain unchanged. For verified contention, the first deadline determines
        the failure type and passive expiry wins a tie. An expired operation
        prevents free admission.
        """
        with self._acquire(
            operation=operation,
            timeout_s=timeout_s,
            deadline_s=deadline_s,
            wait_deadline_s=wait_deadline_s,
            include_file_lock=include_file_lock,
            reserved=reserved,
        ):
            yield

    @contextmanager
    def _acquire(
        self,
        *,
        operation: str,
        timeout_s: float | None,
        deadline_s: float | None,
        wait_deadline_s: float | None,
        include_file_lock: bool,
        reserved: bool,
    ) -> Iterator[None]:
        _validate_admission(operation, timeout_s, deadline_s, wait_deadline_s)
        if not reserved:
            self.reserve()
        started = self._monotonic()
        deadline, contention_deadline_s = _admission_deadlines(
            started, timeout_s, wait_deadline_s, deadline_s
        )
        holder: _Holder | None = None
        in_process_acquired = False
        published = False
        locks = ExitStack()
        try:
            try:
                self._acquire_thread_lock(
                    deadline, started, operation, deadline_s, contention_deadline_s
                )
                in_process_acquired = True
                self._raise_if_deadline_elapsed(
                    deadline,
                    operation=operation,
                    started=started,
                    source="in_process",
                    operation_deadline_s=deadline_s,
                )
                self._raise_if_shutdown(operation, started)
                holder = self._new_holder(operation)
                with self._state_guard:
                    self._holder = holder
                if include_file_lock:
                    locks.enter_context(
                        self._poll_file_lock(
                            self._lock_path,
                            deadline=deadline,
                            started=started,
                            operation=operation,
                            layer="primary",
                            operation_deadline_s=deadline_s,
                            contention_deadline_s=contention_deadline_s,
                        )
                    )
                    self._prepare_owner_paths()
                    locks.enter_context(
                        self._poll_file_lock(
                            self._owner_lock_path,
                            deadline=deadline,
                            started=started,
                            operation=operation,
                            layer="owner",
                            operation_deadline_s=deadline_s,
                            contention_deadline_s=contention_deadline_s,
                        )
                    )
                    self._raise_if_deadline_elapsed(
                        deadline,
                        operation=operation,
                        started=started,
                        source="lock_metadata",
                        operation_deadline_s=deadline_s,
                    )
                    self._write_owner_record(holder)
                    published = True
                    self._raise_if_deadline_elapsed(
                        deadline,
                        operation=operation,
                        started=started,
                        source="lock_metadata",
                        operation_deadline_s=deadline_s,
                    )
            except (LockTimeoutError, LockMetadataError, LockInterruptedError):
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise self._metadata_failure(operation, started, None, "lock_metadata") from exc
            self._raise_if_deadline_elapsed(
                deadline,
                operation=operation,
                started=started,
                source="lock_metadata" if include_file_lock else "in_process",
                operation_deadline_s=deadline_s,
            )
            yield
        finally:
            if published and holder is not None:
                try:
                    self._remove_matching_owner_record(holder.acquisition_token)
                except (OSError, RuntimeError):
                    logger.warning("repository-lock owner record cleanup failed")
            locks.close()
            if in_process_acquired:
                with self._state_guard:
                    if self._holder == holder:
                        self._holder = None
                self.lock.release()
            if not reserved:
                self.release_reservation()

    def _new_holder(self, operation: str) -> _Holder:
        """Create one bounded holder record."""
        wall_time = self._wall_time()
        if not math.isfinite(wall_time):
            raise ValueError("wall time is not finite")
        acquired_at = datetime.fromtimestamp(wall_time, tz=UTC).isoformat().replace("+00:00", "Z")
        return _Holder(
            repository=self.repository,
            operation=operation,
            process_id=os.getpid(),
            acquisition_token=secrets.token_hex(16),
            acquired_at=acquired_at,
        )

    def _acquire_thread_lock(
        self,
        deadline: float | None,
        started: float,
        operation: str,
        operation_deadline_s: float | None,
        contention_deadline_s: float | None,
    ) -> None:
        """Acquire the process lock with interruptible polling."""
        while True:
            self._raise_if_shutdown(operation, started)
            self._raise_if_operation_expired(contention_deadline_s)
            if self.lock.acquire(blocking=False):
                return
            now = self._operation_time(contention_deadline_s)
            if deadline is not None and now >= deadline:
                with self._state_guard:
                    holder = self._holder
                if holder is None:
                    self._raise_if_operation_expired(operation_deadline_s)
                    raise self._metadata_failure(operation, started, None, "in_process")
                raise self._timeout_failure(operation, started, holder, "in_process")
            if self._shutdown.wait(timeout=min(_POLL_S, _remaining(deadline, now))):
                raise self._interrupted_failure(operation, started)

    @contextmanager
    def _poll_file_lock(
        self,
        path: Path,
        *,
        deadline: float | None,
        started: float,
        operation: str,
        layer: str,
        operation_deadline_s: float | None,
        contention_deadline_s: float | None,
    ) -> Iterator[None]:
        """Poll one exclusive file lock under the shared deadline."""
        while True:
            self._raise_if_shutdown(operation, started)
            self._raise_if_operation_expired(contention_deadline_s)
            stack = ExitStack()
            try:
                stack.enter_context(file_lock(path, blocking=False, require_exclusive=True))
            except ExclusiveLockUnavailableError as exc:
                stack.close()
                raise self._metadata_failure(
                    operation,
                    started,
                    None,
                    "lock_metadata",
                ) from exc
            except LockUnavailableError as exc:
                stack.close()
                now = self._operation_time(contention_deadline_s)
                if deadline is not None and now >= deadline:
                    if layer == "owner":
                        self._raise_if_operation_expired(operation_deadline_s)
                        raise self._metadata_failure(
                            operation, started, None, "owner_sentinel"
                        ) from exc
                    holder = self._probe_external_holder()
                    if holder is None:
                        self._raise_if_operation_expired(operation_deadline_s)
                        raise self._metadata_failure(
                            operation, started, None, "owner_sidecar"
                        ) from exc
                    raise self._timeout_failure(
                        operation, started, holder, "owner_sidecar"
                    ) from exc
                if self._shutdown.wait(timeout=min(_POLL_S, _remaining(deadline, now))):
                    raise self._interrupted_failure(operation, started) from exc
            except (OSError, RuntimeError) as exc:
                stack.close()
                raise self._metadata_failure(operation, started, None, "lock_metadata") from exc
            else:
                try:
                    self._raise_if_deadline_elapsed(
                        deadline,
                        operation=operation,
                        started=started,
                        source="owner_sentinel" if layer == "owner" else "lock_metadata",
                        operation_deadline_s=operation_deadline_s,
                    )
                    self._raise_if_shutdown(operation, started)
                    yield
                finally:
                    stack.close()
                return

    def _prepare_owner_paths(self) -> None:
        """Reject unsafe owner paths and remove one stale regular record."""
        self._owner_record_path.parent.mkdir(parents=True, exist_ok=True)
        parent_fd = _open_parent(self._owner_record_path.parent)
        try:
            for name, remove in (
                (self._owner_lock_path.name, False),
                (self._owner_record_path.name, True),
            ):
                try:
                    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("repository-lock owner path is unsafe")
                if remove:
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def _write_owner_record(self, holder: _Holder) -> None:
        """Publish one complete mode-0600 record without replacing a path."""
        payload = json.dumps(
            {
                "version": _OWNER_RECORD_VERSION,
                "repository": holder.repository,
                "operation": holder.operation,
                "process_id": holder.process_id,
                "acquisition_token": holder.acquisition_token,
                "acquired_at": holder.acquired_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _OWNER_RECORD_MAX_BYTES:
            raise ValueError("repository-lock owner record is too large")
        parent_fd = _open_parent(self._owner_record_path.parent)
        temporary = f".{self._owner_record_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if os.name == "posix":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        temporary_exists = False
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
            temporary_exists = True
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("repository-lock owner write made no progress")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.link(
                temporary,
                self._owner_record_path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=parent_fd)
            temporary_exists = False
            os.fsync(parent_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_exists:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=parent_fd)
            os.close(parent_fd)

    def _probe_external_holder(self) -> _Holder | None:
        """Read holder data only while the owner sentinel is active."""
        try:
            with file_lock(
                self._owner_lock_path,
                blocking=False,
                require_exclusive=True,
            ):
                return None
        except ExclusiveLockUnavailableError:
            return None
        except LockUnavailableError:
            return self._read_owner_record()
        except (OSError, RuntimeError):
            return None

    def _read_owner_record(self) -> _Holder | None:
        """Read and strictly validate one bounded owner record."""
        try:
            parent_fd = _open_parent(self._owner_record_path.parent)
        except (FileNotFoundError, OSError, RuntimeError):
            return None
        try:
            payload_bytes, _record_identity = _read_regular_at(
                parent_fd, self._owner_record_path.name
            )
        except (FileNotFoundError, OSError, RuntimeError):
            return None
        finally:
            os.close(parent_fd)
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return self._decode_holder(payload)

    def _decode_holder(self, payload: object) -> _Holder | None:
        """Validate the closed owner-record schema."""
        if not isinstance(payload, dict) or set(payload) != _OWNER_RECORD_KEYS:
            return None
        version = payload.get("version")
        process_id = payload.get("process_id")
        repository = payload.get("repository")
        operation = payload.get("operation")
        token = payload.get("acquisition_token")
        acquired_at = payload.get("acquired_at")
        if (
            type(version) is not int
            or version != _OWNER_RECORD_VERSION
            or isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id <= 0
            or repository != self.repository
            or not isinstance(operation, str)
            or not operation
            or len(operation) > 200
            or not isinstance(token, str)
            or _OWNER_TOKEN_RE.fullmatch(token) is None
            or not isinstance(acquired_at, str)
            or not acquired_at.endswith("Z")
        ):
            return None
        try:
            parsed = datetime.fromisoformat(acquired_at[:-1] + "+00:00")
        except ValueError:
            return None
        if parsed.tzinfo != UTC:
            return None
        return _Holder(repository, operation, process_id, token, acquired_at)

    def _remove_matching_owner_record(self, token: str) -> None:
        """Remove the record only if it still belongs to this holder."""
        parent_fd = _open_parent(self._owner_record_path.parent)
        try:
            payload_bytes, record_identity = _read_regular_at(
                parent_fd, self._owner_record_path.name
            )
            try:
                holder = self._decode_holder(json.loads(payload_bytes.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                holder = None
            if holder is None or holder.acquisition_token != token:
                return
            current = os.stat(
                self._owner_record_path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if _identity(current) != record_identity:
                raise RuntimeError("repository-lock owner record changed")
            os.unlink(self._owner_record_path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def _raise_if_shutdown(self, operation: str, started: float) -> None:
        """Raise the typed interruption when shutdown is set."""
        if self._shutdown.is_set():
            raise self._interrupted_failure(operation, started)

    def _raise_if_deadline_elapsed(
        self,
        deadline: float | None,
        *,
        operation: str,
        started: float,
        source: str,
        operation_deadline_s: float | None,
    ) -> None:
        """Stop acquisition when its shared deadline has elapsed."""
        now = self._operation_time(operation_deadline_s)
        if deadline is not None and now >= deadline:
            raise self._metadata_failure(operation, started, None, source)

    def _raise_if_operation_expired(self, deadline_s: float | None) -> None:
        """Keep an operation deadline distinct from passive lock contention."""
        if deadline_s is not None:
            self._operation_time(deadline_s)

    def _operation_time(self, deadline_s: float | None) -> float:
        """Read the admission clock and reject an expired operation."""
        now = self._monotonic()
        if deadline_s is not None and now >= deadline_s:
            raise subprocess.TimeoutExpired("repository lock operation deadline", 0)
        return now

    def _timeout_failure(
        self, operation: str, started: float, holder: _Holder, source: str
    ) -> LockTimeoutError:
        """Build a verified lock-timeout result."""
        return LockTimeoutError(
            "lock_timeout",
            _failure_fields(
                failure_kind="lock_timeout",
                repository=self.repository,
                operation=operation,
                wait_duration_s=self._monotonic() - started,
                holder=holder,
                holder_source=source,
            ),
        )

    def _metadata_failure(
        self,
        operation: str,
        started: float,
        holder: _Holder | None,
        source: str | None,
    ) -> LockMetadataError:
        """Build a fail-closed metadata result."""
        return LockMetadataError(
            "lock_metadata_error",
            _failure_fields(
                failure_kind="lock_metadata_error",
                repository=self.repository,
                operation=operation,
                wait_duration_s=self._monotonic() - started,
                holder=holder,
                holder_source=source,
            ),
        )

    def _interrupted_failure(self, operation: str, started: float) -> LockInterruptedError:
        """Build a bounded shutdown result."""
        return LockInterruptedError(
            "interrupted_waiting_for_git_lock",
            _failure_fields(
                failure_kind="interrupted",
                repository=self.repository,
                operation=operation,
                wait_duration_s=self._monotonic() - started,
                holder=None,
                holder_source=None,
            ),
        )


def _remaining(deadline: float | None, now: float) -> float:
    """Return a non-negative polling interval."""
    if deadline is None:
        return _POLL_S
    return max(deadline - now, 0.0)
