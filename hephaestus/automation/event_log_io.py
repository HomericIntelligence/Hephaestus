"""Descriptor-bound file operations for optional pipeline event logs."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

from hephaestus.utils.file_lock import LockUnavailableError

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


def event_log_io_supported() -> bool:
    """Return whether the host supports the required descriptor operations."""
    required_dir_fd = (os.mkdir, os.open, os.stat, os.unlink)
    return (
        os.name == "posix"
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and all(operation in os.supports_dir_fd for operation in required_dir_fd)
        and os.stat in os.supports_follow_symlinks
        and os.listdir in os.supports_fd
        and callable(getattr(os, "geteuid", None))
        and fcntl is not None
    )


def _effective_uid() -> int:
    """Return the effective user identifier after capability admission."""
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        raise OSError(errno.ENOTSUP, "effective user identifiers are unavailable")
    return int(get_effective_uid())


def _directory_flags() -> int:
    """Return the admitted flags for a no-follow directory open."""
    if not event_log_io_supported():
        raise OSError(errno.ENOTSUP, "descriptor-bound event logs are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _validate_private_directory(descriptor: int) -> None:
    """Require one private directory owned by the effective user."""
    status = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE
        or status.st_uid != _effective_uid()
    ):
        raise OSError(errno.EPERM, "event-log directory is not private")


@dataclass(frozen=True)
class EventLogCandidate:
    """Describe one event-log path and its first private directory."""

    path: Path
    private_root: Path


class EventLogHandle:
    """Operate on one event log through a held parent-directory descriptor."""

    def __init__(self, path: Path, directory_descriptor: int) -> None:
        """Bind the display path and an externally owned directory descriptor."""
        self.path = path
        self._directory_descriptor = directory_descriptor

    def _open_file(
        self,
        name: str,
        *,
        append: bool = False,
        exclusive: bool = False,
    ) -> int:
        """Open one private regular file relative to the bound directory."""
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        if exclusive:
            flags |= os.O_EXCL
        if append:
            flags |= os.O_APPEND
        descriptor = os.open(
            name,
            flags,
            _FILE_MODE,
            dir_fd=self._directory_descriptor,
        )
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_uid != _effective_uid():
                raise OSError(errno.EPERM, "event-log file is not private")
            os.fchmod(descriptor, _FILE_MODE)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def append_line(self, line: str) -> None:
        """Append one complete UTF-8 line to the bound event log."""
        descriptor = self._open_file(self.path.name, append=True)
        try:
            data = line.encode("utf-8")
            while data:
                written = os.write(descriptor, data)
                if written == 0:
                    raise OSError(errno.EIO, "event-log append did not make progress")
                data = data[written:]
        finally:
            os.close(descriptor)

    def probe_write(self) -> None:
        """Create, write, and remove one private probe in the bound directory."""
        name = f".event-log-probe-{secrets.token_hex(16)}"
        descriptor = self._open_file(name, exclusive=True)
        try:
            os.write(descriptor, b"{}\n")
        finally:
            os.close(descriptor)
            with suppress(OSError):
                os.unlink(name, dir_fd=self._directory_descriptor)

    def names(self) -> list[str]:
        """Return names from the bound directory."""
        return os.listdir(self._directory_descriptor)

    def stat_name(self, name: str) -> os.stat_result:
        """Return no-follow metadata for one bound child name."""
        return os.stat(
            name,
            dir_fd=self._directory_descriptor,
            follow_symlinks=False,
        )

    def unlink_name(self, name: str) -> None:
        """Remove one child from the bound directory."""
        os.unlink(name, dir_fd=self._directory_descriptor)

    @contextmanager
    def lock(self, name: str, *, blocking: bool) -> Iterator[None]:
        """Hold one exclusive advisory lock in the bound directory."""
        descriptor = self._open_file(name)
        try:
            if fcntl is None:  # pragma: no cover - guarded by capability admission
                raise OSError(errno.ENOTSUP, "event-log locking is unavailable")
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(descriptor, operation)
            except OSError as exc:
                if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise LockUnavailableError(f"Lock already held: {self.path / name}") from exc
                raise
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _absolute_path(path: Path) -> Path:
    """Return one lexical absolute path without following a symlink."""
    return Path(os.path.abspath(os.fspath(path)))


@contextmanager
def open_event_log_handle(candidate: EventLogCandidate) -> Iterator[EventLogHandle]:
    """Create and bind one event-log directory without following components."""
    if not event_log_io_supported():
        raise OSError(errno.ENOTSUP, "descriptor-bound event logs are unavailable")
    path = _absolute_path(candidate.path)
    parent = path.parent
    private_root = _absolute_path(candidate.private_root)
    try:
        parent.relative_to(private_root)
    except ValueError as exc:
        raise OSError(errno.EPERM, "event-log path is outside its private root") from exc

    descriptor = os.open(parent.anchor, _directory_flags())
    current_path = Path(parent.anchor)
    try:
        for component in parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError(errno.EPERM, "event-log path has an unsafe component")
            child_path = current_path / component
            created = False
            try:
                child_descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(component, _DIRECTORY_MODE, dir_fd=descriptor)
                    created = True
                child_descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            if created or child_path == private_root or private_root in child_path.parents:
                try:
                    _validate_private_directory(child_descriptor)
                except BaseException:
                    os.close(child_descriptor)
                    raise
            os.close(descriptor)
            descriptor = child_descriptor
            current_path = child_path
        yield EventLogHandle(path, descriptor)
    finally:
        os.close(descriptor)
