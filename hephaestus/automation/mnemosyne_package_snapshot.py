"""Own the descriptors and entries of one private Node package snapshot."""

from __future__ import annotations

import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_UNAVAILABLE = "Node package dependency tree is unavailable"
_CLEANUP_FAILED = "Node package snapshot cleanup failed"


class DirectoryBinding(Protocol):
    """Keep an absolute directory path bound to open descriptors."""

    @property
    def descriptor(self) -> int:
        """Return the final directory descriptor."""

    @property
    def descriptors(self) -> list[int]:
        """Return the descriptors that this binding owns."""

    def verify(self) -> None:
        """Reject a replaced path component."""

    def close(self, *, preserve_error: bool) -> None:
        """Close the binding and preserve an active error when requested."""


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _revision(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise LearnDeliveryError("Node package dependency tree timed out")


def verify_snapshot_base(binding: DirectoryBinding) -> None:
    """Reject a temporary path that another user can replace."""
    binding.verify()
    for descriptor in binding.descriptors:
        metadata = os.fstat(descriptor)
        writable = bool(metadata.st_mode & 0o022)
        root_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or (writable and not root_sticky)
        ):
            raise LearnDeliveryError(_UNAVAILABLE)


@dataclass
class _CleanupFrame:
    descriptor: int
    relative: Path
    children: list[str]
    index: int = 0


@dataclass
class PackageSnapshot:
    """Keep one private snapshot bound from creation through cleanup.

    The private parent has mode 0700. The lint child has no write grant for
    this parent. Identity checks detect replacements at operation boundaries.
    Portable unlink does not provide an atomic identity condition.
    """

    base: Path
    binding: DirectoryBinding
    max_entries: int
    max_depth: int
    timeout_s: float
    max_descriptors: int
    outside_descriptors: int
    _name: str = ""
    _parent_fd: int = -1
    _root_fd: int = -1
    _parent_identity: os.stat_result | None = None
    _entries: dict[Path, os.stat_result] = field(default_factory=dict)
    _transient_descriptors: int = 0
    _closed: bool = False

    @classmethod
    def create(
        cls,
        base: Path,
        binding: DirectoryBinding,
        *,
        deadline: float,
        max_entries: int,
        max_depth: int,
        timeout_s: float,
        max_descriptors: int,
        outside_descriptors: int,
    ) -> PackageSnapshot:
        """Take ownership of the base binding and create a private snapshot."""
        snapshot = cls(
            base,
            binding,
            max_entries,
            max_depth,
            timeout_s,
            max_descriptors,
            outside_descriptors,
        )
        try:
            _check_deadline(deadline)
            verify_snapshot_base(binding)
            snapshot.require_descriptors(2)
            for _attempt in range(8):
                name = "hephaestus-node-package-" + secrets.token_hex(12)
                try:
                    os.mkdir(name, 0o700, dir_fd=binding.descriptor)
                except FileExistsError:
                    continue
                snapshot._name = name
                break
            else:
                raise LearnDeliveryError(_UNAVAILABLE)
            snapshot._parent_identity = os.stat(
                name, dir_fd=binding.descriptor, follow_symlinks=False
            )
            snapshot._parent_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=binding.descriptor)
            parent = os.fstat(snapshot._parent_fd)
            named = os.stat(name, dir_fd=binding.descriptor, follow_symlinks=False)
            if (
                _revision(parent) != _revision(snapshot._parent_identity)
                or _revision(parent) != _revision(named)
                or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
            ):
                raise LearnDeliveryError(_UNAVAILABLE)
            snapshot._parent_identity = parent
            os.mkdir("node_modules", 0o700, dir_fd=snapshot._parent_fd)
            snapshot._entries[Path()] = os.stat(
                "node_modules", dir_fd=snapshot._parent_fd, follow_symlinks=False
            )
            snapshot._root_fd = os.open(
                "node_modules", _DIRECTORY_FLAGS, dir_fd=snapshot._parent_fd
            )
            snapshot.verify()
            _check_deadline(deadline)
            return snapshot
        except BaseException as error:
            with suppress(BaseException):
                snapshot.close(preserve_error=True)
            if isinstance(error, OSError):
                raise LearnDeliveryError(_UNAVAILABLE) from None
            raise

    @property
    def parent(self) -> Path:
        """Return the private parent path for the sandbox profile."""
        return self.base / self._name

    @property
    def root(self) -> Path:
        """Return the package root path for the sandbox profile."""
        return self.parent / "node_modules"

    @property
    def descriptor(self) -> int:
        """Return the retained package root descriptor."""
        return self._root_fd

    @property
    def descriptors(self) -> list[int]:
        """Return the live descriptors for resource accounting."""
        return [
            *self.binding.descriptors,
            *(descriptor for descriptor in (self._parent_fd, self._root_fd) if descriptor >= 0),
        ]

    def require_descriptors(self, additional: int) -> None:
        """Reserve room before an operation opens more descriptors."""
        active = len(self.descriptors) + self.outside_descriptors + self._transient_descriptors
        if active + additional > self.max_descriptors:
            raise LearnDeliveryError("Node package dependency tree is too large")

    def _open_transient(self, name: str, flags: int, parent_fd: int, mode: int = 0o600) -> int:
        self.require_descriptors(1)
        descriptor = os.open(name, flags, mode, dir_fd=parent_fd)
        self._transient_descriptors += 1
        return descriptor

    def _close_transient(self, descriptor: int) -> None:
        os.close(descriptor)
        self._transient_descriptors -= 1

    def verify(self) -> None:
        """Reject a replaced ancestor, private parent, or package root."""
        if self._closed or self._parent_identity is None:
            raise LearnDeliveryError(_UNAVAILABLE)
        try:
            verify_snapshot_base(self.binding)
            for parent_fd, name, descriptor, expected in (
                (self.binding.descriptor, self._name, self._parent_fd, self._parent_identity),
                (self._parent_fd, "node_modules", self._root_fd, self._entries[Path()]),
            ):
                opened = os.fstat(descriptor)
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    _identity(expected) != _identity(opened)
                    or _identity(expected) != _identity(named)
                    or stat.S_IMODE(expected.st_mode) != stat.S_IMODE(opened.st_mode)
                ):
                    raise LearnDeliveryError(_UNAVAILABLE)
        except (OSError, KeyError):
            raise LearnDeliveryError(_UNAVAILABLE) from None

    @contextmanager
    def _directory(self, relative: Path) -> Iterator[int]:
        """Open a recorded directory through the retained package root."""
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) > self.max_depth:
            raise LearnDeliveryError(_UNAVAILABLE)
        self.verify()
        self.require_descriptors(1)
        descriptor = os.dup(self._root_fd)
        self._transient_descriptors += 1
        primary_error = False
        try:
            current = Path()
            for name in relative.parts:
                current /= name
                expected = self._entries[current]
                child = self._open_transient(name, _DIRECTORY_FLAGS, descriptor)
                previous, descriptor = descriptor, child
                self._close_transient(previous)
                if _identity(os.fstat(descriptor)) != _identity(expected):
                    raise LearnDeliveryError(_UNAVAILABLE)
            yield descriptor
            self.verify()
        except (OSError, KeyError):
            primary_error = True
            raise LearnDeliveryError(_UNAVAILABLE) from None
        except BaseException:
            primary_error = True
            raise
        finally:
            closing, descriptor = descriptor, -1
            try:
                self._close_transient(closing)
            except OSError:
                if not primary_error:
                    raise LearnDeliveryError(_UNAVAILABLE) from None

    def mkdir(self, relative: Path, deadline: float) -> None:
        """Create and record one package directory."""
        _check_deadline(deadline)
        with self._directory(relative.parent) as parent_fd:
            os.mkdir(relative.name, 0o700, dir_fd=parent_fd)
            self._entries[relative] = os.stat(
                relative.name, dir_fd=parent_fd, follow_symlinks=False
            )

    def write_file(self, relative: Path, payload: bytes, mode: int, deadline: float) -> None:
        """Write one exclusive file and remove its write permission."""
        _check_deadline(deadline)
        with self._directory(relative.parent) as parent_fd:
            descriptor = self._open_transient(relative.name, _FILE_FLAGS, parent_fd)
            primary_error = False
            try:
                self._entries[relative] = os.fstat(descriptor)
                offset = 0
                while offset < len(payload):
                    _check_deadline(deadline)
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise LearnDeliveryError(_UNAVAILABLE)
                    offset += written
                os.fchmod(descriptor, 0o500 if mode & 0o111 else 0o400)
                _check_deadline(deadline)
            except BaseException:
                primary_error = True
                raise
            finally:
                try:
                    self._entries[relative] = os.fstat(descriptor)
                except OSError:
                    if not primary_error:
                        primary_error = True
                        raise LearnDeliveryError(_UNAVAILABLE) from None
                finally:
                    closing, descriptor = descriptor, -1
                    try:
                        self._close_transient(closing)
                    except OSError:
                        if not primary_error:
                            raise LearnDeliveryError(_UNAVAILABLE) from None

    def symlink(self, relative: Path, target: Path, deadline: float) -> None:
        """Create a relative link to one admitted internal target."""
        _check_deadline(deadline)
        text = os.path.relpath(target, start=relative.parent)
        with self._directory(relative.parent) as parent_fd:
            os.symlink(text, relative.name, dir_fd=parent_fd)
            self._entries[relative] = os.stat(
                relative.name, dir_fd=parent_fd, follow_symlinks=False
            )

    def seal(self, deadline: float) -> None:
        """Remove write permission from all recorded directories."""
        for relative, metadata in reversed(tuple(self._entries.items())):
            _check_deadline(deadline)
            if stat.S_ISDIR(metadata.st_mode):
                with self._directory(relative) as descriptor:
                    os.fchmod(descriptor, 0o500)
                    self._entries[relative] = os.fstat(descriptor)
        self.verify()

    def _cleanup(self, deadline: float) -> None:  # noqa: C901
        """Remove only recorded entries within one traversal budget."""
        remaining = self.max_entries
        frames: list[_CleanupFrame] = []

        def scan(descriptor: int, relative: Path) -> list[str]:
            nonlocal remaining
            _check_deadline(deadline)
            if len(relative.parts) > self.max_depth:
                raise LearnDeliveryError(_CLEANUP_FAILED)
            children: list[str] = []
            self.require_descriptors(1)
            with os.scandir(descriptor) as entries:
                for child in entries:
                    _check_deadline(deadline)
                    if remaining <= 0:
                        raise LearnDeliveryError(_CLEANUP_FAILED)
                    remaining -= 1
                    children.append(child.name)
            return children

        def current(parent_fd: int, name: str, expected: os.stat_result) -> os.stat_result:
            _check_deadline(deadline)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(expected) != _identity(named):
                raise LearnDeliveryError(_CLEANUP_FAILED)
            if not stat.S_ISDIR(expected.st_mode) and _revision(expected) != _revision(named):
                raise LearnDeliveryError(_CLEANUP_FAILED)
            return named

        try:
            verify_snapshot_base(self.binding)
            if self._parent_identity is None:
                raise LearnDeliveryError(_CLEANUP_FAILED)
            current(self.binding.descriptor, self._name, self._parent_identity)
            if _identity(os.fstat(self._parent_fd)) != _identity(self._parent_identity):
                raise LearnDeliveryError(_CLEANUP_FAILED)
            root_identity = self._entries.get(Path())
            if root_identity is not None:
                if remaining <= 0:
                    raise LearnDeliveryError(_CLEANUP_FAILED)
                remaining -= 1
                current(self._parent_fd, "node_modules", root_identity)
                if self._root_fd < 0:
                    # An open failure leaves an empty root with a recorded identity.
                    os.rmdir("node_modules", dir_fd=self._parent_fd)
                else:
                    if _identity(os.fstat(self._root_fd)) != _identity(root_identity):
                        raise LearnDeliveryError(_CLEANUP_FAILED)
                    os.fchmod(self._root_fd, 0o700)
                    frames.append(_CleanupFrame(self._root_fd, Path(), []))
                    frames[-1].children = scan(self._root_fd, Path())
            elif self._root_fd >= 0:
                raise LearnDeliveryError(_CLEANUP_FAILED)
            while frames:
                frame = frames[-1]
                _check_deadline(deadline)
                if frame.index < len(frame.children):
                    name = frame.children[frame.index]
                    frame.index += 1
                    relative = frame.relative / name
                    expected = self._entries.get(relative)
                    if expected is None:
                        raise LearnDeliveryError(_CLEANUP_FAILED)
                    observed = current(frame.descriptor, name, expected)
                    if stat.S_ISDIR(observed.st_mode):
                        descriptor = self._open_transient(name, _DIRECTORY_FLAGS, frame.descriptor)
                        try:
                            frames.append(_CleanupFrame(descriptor, relative, []))
                        except BaseException:
                            with suppress(BaseException):
                                self._close_transient(descriptor)
                            raise
                        if _identity(os.fstat(descriptor)) != _identity(observed):
                            raise LearnDeliveryError(_CLEANUP_FAILED)
                        os.fchmod(descriptor, 0o700)
                        frames[-1].children = scan(descriptor, relative)
                    else:
                        current(frame.descriptor, name, observed)
                        os.unlink(name, dir_fd=frame.descriptor)
                    continue
                parent_fd = frames[-2].descriptor if len(frames) > 1 else self._parent_fd
                name = frame.relative.name if frame.relative.parts else "node_modules"
                current(parent_fd, name, os.fstat(frame.descriptor))
                os.rmdir(name, dir_fd=parent_fd)
                completed = frames.pop()
                if completed.descriptor != self._root_fd:
                    self._close_transient(completed.descriptor)
            current(self.binding.descriptor, self._name, self._parent_identity)
            os.rmdir(self._name, dir_fd=self.binding.descriptor)
        finally:
            while frames:
                frame = frames.pop()
                if frame.descriptor != self._root_fd:
                    with suppress(BaseException):
                        self._close_transient(frame.descriptor)

    def close(self, *, preserve_error: bool = False) -> None:
        """Remove known entries and close each owned descriptor once."""
        if self._closed:
            return
        self._closed = True
        failed = False
        try:
            if self._parent_identity is not None:
                self._cleanup(time.monotonic() + self.timeout_s)
        except BaseException:
            failed = True
        finally:
            descriptors = (self._root_fd, self._parent_fd)
            self._root_fd = self._parent_fd = -1
            for descriptor in descriptors:
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except BaseException:
                        failed = True
            try:
                self.binding.close(preserve_error=False)
            except BaseException:
                failed = True
        if failed and not preserve_error:
            raise LearnDeliveryError(_CLEANUP_FAILED)
