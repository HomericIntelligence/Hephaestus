"""Repository-scoped locks for pipeline Git operations.

The primary lock protects shared Git metadata. The in-process lock prevents
same-process ``flock`` ambiguity. A short-lived owner record gives another
process enough verified information to explain a timeout without exposing raw
paths or file contents.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
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

DEFAULT_GIT_LOCK_TIMEOUT_S = 7200
_POLL_S = 0.1
_OWNER_RECORD_VERSION = 1
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
class _Holder:
    """Internal holder data used for local and cross-process diagnostics."""

    repository: str
    operation: str
    process_id: int
    acquisition_token: str
    acquired_at: str


class RepositoryLockError(RuntimeError):
    """Base error for a repository-lock acquisition failure."""

    def __init__(self, failure_kind: str, details: dict[str, object]) -> None:
        """Store a stable failure class and bounded diagnostic fields."""
        self.failure_kind = failure_kind
        self.details = dict(details)
        super().__init__(failure_kind)


class LockTimeoutError(RepositoryLockError):
    """Raised when the repository lock wait budget expires."""


class LockMetadataError(RepositoryLockError):
    """Raised when holder metadata cannot be verified safely."""


class LockInterruptedError(RepositoryLockError):
    """Raised when shutdown interrupts repository-lock acquisition."""


def repo_lock_path(repo: str, lock_dir: Path | None = None) -> Path:
    """Return the stable primary lock path for ``repo``.

    Args:
        repo: Repository scheduling identity.
        lock_dir: Optional test or operator-selected lock directory.

    Returns:
        The primary cross-process lock path.

    """
    directory = lock_dir or get_repo_root() / DEFAULT_STATE_DIR / "locks"
    return directory / f"git-{repo.replace('/', '_')}.lock"


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


class RepositoryOperationLock:
    """Coordinate one repository's in-process and cross-process operations.

    The instance is shared by all workers in one process. Git operations use
    the in-process lock, the primary repository lock, and the owner sentinel.
    GitHub operations can use only the in-process lock.
    """

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
        """Initialize a repository lock and its holder state."""
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError("repository must be a non-empty string")
        self.repository = repository
        self.lock = threading.Lock()
        self.users = 0
        self._state_guard = threading.Lock()
        self._holder: _Holder | None = None
        self._lock_path = repo_lock_path(repository, lock_dir)
        self._owner_lock_path = Path(f"{self._lock_path}.owner.lock")
        self._owner_record_path = Path(f"{self._lock_path}.owner.json")
        self._shutdown = shutdown or threading.Event()
        self._on_idle = on_idle
        self._monotonic = monotonic
        self._wall_time = wall_time

    @property
    def holder(self) -> dict[str, object] | None:
        """Return a bounded copy of the current in-process holder data."""
        with self._state_guard:
            holder = self._holder
        if holder is None:
            return None
        return {
            "repository": holder.repository,
            "operation": holder.operation,
            "process_id": holder.process_id,
            "acquired_at": holder.acquired_at,
        }

    @contextmanager
    def acquire_in_process(
        self,
        *,
        operation: str = "",
        timeout_s: float | None = None,
    ) -> Iterator[None]:
        """Acquire only the in-process repository lock."""
        with self._acquire(operation=operation, timeout_s=timeout_s, include_file_lock=False):
            yield

    @contextmanager
    def acquire(
        self,
        *,
        operation: str,
        timeout_s: float,
        include_file_lock: bool = True,
    ) -> Iterator[None]:
        """Acquire the requested lock layers under one monotonic deadline."""
        with self._acquire(
            operation=operation,
            timeout_s=timeout_s,
            include_file_lock=include_file_lock,
        ):
            yield

    @contextmanager
    def _acquire(
        self,
        *,
        operation: str,
        timeout_s: float | None,
        include_file_lock: bool,
    ) -> Iterator[None]:
        started = self._monotonic()
        deadline = None if timeout_s is None else started + max(float(timeout_s), 0.0)
        token = secrets.token_hex(16)
        self._increment_users()
        in_process_acquired = False
        holder: _Holder | None = None
        published = False
        stack = ExitStack()
        try:
            try:
                self._acquire_thread_lock(deadline, started, operation)
                in_process_acquired = True
                if self._shutdown.is_set():
                    raise self._interrupted_failure(operation, started)
                acquired_at = (
                    datetime.fromtimestamp(self._wall_time(), tz=UTC)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                holder = _Holder(
                    repository=self.repository,
                    operation=operation or "repository_operation",
                    process_id=os.getpid(),
                    acquisition_token=token,
                    acquired_at=acquired_at,
                )
                with self._state_guard:
                    self._holder = holder

                if include_file_lock:
                    stack.enter_context(
                        self._poll_file_lock(self._lock_path, deadline, started, operation)
                    )
                    self._prepare_owner_record()
                    try:
                        stack.enter_context(
                            self._poll_file_lock(
                                self._owner_lock_path,
                                deadline,
                                started,
                                operation,
                            )
                        )
                    except LockTimeoutError as exc:
                        details = dict(exc.details)
                        details["failure_kind"] = "lock_metadata_error"
                        raise LockMetadataError("lock_metadata_error", details) from exc
                    self._write_owner_record(holder)
                    published = True

            except (LockTimeoutError, LockMetadataError, LockInterruptedError):
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                raise self._metadata_failure(
                    operation=operation,
                    started=started,
                    holder=None,
                    source="lock_metadata",
                ) from exc
            yield
        finally:
            try:
                if published and holder is not None:
                    self._remove_matching_owner_record(holder.acquisition_token)
            finally:
                try:
                    stack.close()
                finally:
                    if in_process_acquired:
                        with self._state_guard:
                            if holder is not None and self._holder == holder:
                                self._holder = None
                        self.lock.release()
                    self._decrement_users()

    def _increment_users(self) -> None:
        """Count a holder or waiter before lock acquisition begins."""
        with self._state_guard:
            self.users += 1

    def _decrement_users(self) -> None:
        """Drop a holder or waiter and evict an idle pool entry."""
        with self._state_guard:
            self.users -= 1
            idle = self.users == 0
        if idle and self._on_idle is not None:
            self._on_idle(self)

    def _acquire_thread_lock(
        self,
        deadline: float | None,
        started: float,
        operation: str,
    ) -> None:
        """Acquire the process lock with interruptible polling."""
        while not self.lock.acquire(blocking=False):
            if self._shutdown.is_set():
                raise self._interrupted_failure(operation, started)
            now = self._monotonic()
            if deadline is not None and now >= deadline:
                with self._state_guard:
                    holder = self._holder
                if holder is None:
                    raise self._metadata_failure(
                        operation=operation,
                        started=started,
                        holder=None,
                        source="in_process",
                    )
                raise self._timeout_failure(operation, started, holder, "in_process")
            wait_s = _remaining(deadline, now)
            if self._shutdown.wait(timeout=min(_POLL_S, wait_s)):
                raise self._interrupted_failure(operation, started)

    @contextmanager
    def _poll_file_lock(
        self,
        path: Path,
        deadline: float | None,
        started: float,
        operation: str,
    ) -> Iterator[None]:
        """Poll an exclusive file lock until the shared deadline."""
        while True:
            if self._shutdown.is_set():
                raise self._interrupted_failure(operation, started)
            try:
                lock_context = file_lock(path, blocking=False, require_exclusive=True)
                stack = ExitStack()
                stack.enter_context(lock_context)
            except LockUnavailableError as exc:
                now = self._monotonic()
                if deadline is not None and now >= deadline:
                    holder = self._probe_external_holder()
                    if holder is None:
                        raise self._metadata_failure(
                            operation=operation,
                            started=started,
                            holder=None,
                            source="owner_sidecar",
                        ) from exc
                    raise self._timeout_failure(
                        operation, started, holder, "owner_sidecar"
                    ) from exc
                wait_s = _remaining(deadline, now)
                if self._shutdown.wait(timeout=min(_POLL_S, wait_s)):
                    raise self._interrupted_failure(operation, started) from exc
            except (OSError, RuntimeError) as exc:
                raise self._metadata_failure(
                    operation=operation,
                    started=started,
                    holder=None,
                    source="lock_metadata",
                ) from exc
            else:
                try:
                    if self._shutdown.is_set():
                        raise self._interrupted_failure(operation, started)
                    yield
                finally:
                    stack.close()
                return

    def _prepare_owner_record(self) -> None:
        """Reject unsafe sidecar paths and remove a stale regular record."""
        for path in (self._owner_lock_path, self._owner_record_path):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"unsafe repository lock metadata path: {path}")
            if path == self._owner_record_path:
                path.unlink()

    def _write_owner_record(self, holder: _Holder) -> None:
        """Write the current holder sidecar with restrictive permissions."""
        payload = {
            "version": _OWNER_RECORD_VERSION,
            "repository": holder.repository,
            "operation": holder.operation,
            "process_id": holder.process_id,
            "acquisition_token": holder.acquisition_token,
            "acquired_at": holder.acquired_at,
        }
        write_secure(
            self._owner_record_path,
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        )

    def _probe_external_holder(self) -> _Holder | None:
        """Return verified external holder data, or ``None`` on metadata error."""
        try:
            with file_lock(
                self._owner_lock_path,
                blocking=False,
                require_exclusive=True,
            ):
                return None
        except LockUnavailableError:
            return self._read_owner_record()
        except (OSError, RuntimeError):
            return None

    def _read_owner_record(self) -> _Holder | None:
        """Strictly validate and decode the owner sidecar."""
        try:
            if self._owner_record_path.is_symlink() or not self._owner_record_path.is_file():
                return None
            payload = json.loads(self._owner_record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != _OWNER_RECORD_KEYS:
            return None
        version = payload.get("version")
        process_id = payload.get("process_id")
        repository = payload.get("repository")
        operation = payload.get("operation")
        token = payload.get("acquisition_token")
        acquired_at = payload.get("acquired_at")
        if (
            isinstance(version, bool)
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
        return _Holder(
            repository=repository,
            operation=operation,
            process_id=process_id,
            acquisition_token=token,
            acquired_at=acquired_at,
        )

    def _remove_matching_owner_record(self, token: str) -> None:
        """Remove the sidecar only when its token matches this holder."""
        try:
            holder = self._read_owner_record()
            if holder is not None and holder.acquisition_token == token:
                self._owner_record_path.unlink()
        except OSError:
            # The primary lock release remains authoritative. The next holder
            # will validate and replace a stale regular sidecar.
            return

    def _timeout_failure(
        self,
        operation: str,
        started: float,
        holder: _Holder,
        source: str,
    ) -> LockTimeoutError:
        """Build a timeout with verified holder fields."""
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
        *,
        operation: str,
        started: float,
        holder: _Holder | None,
        source: str | None,
    ) -> LockMetadataError:
        """Build a fail-closed metadata error."""
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
        """Build a bounded shutdown interruption result."""
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
    """Return a positive wait interval for the next polling step."""
    if deadline is None:
        return _POLL_S
    return max(deadline - now, 0.0)
