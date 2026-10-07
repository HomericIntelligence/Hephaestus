"""Descriptor-owned file publication for finite source snapshots."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from hephaestus.automation.fleet_snapshot_policy import SnapshotError
from hephaestus.automation.git_runtime import remaining_operation_timeout

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def remaining(deadline: float) -> float:
    """Keep the snapshot and inherited operation budgets for filesystem work."""
    value = deadline - time.monotonic()
    if value <= 0:
        raise SnapshotError("snapshot deadline exceeded")
    return cast(float, remaining_operation_timeout(value))


def node(metadata: os.stat_result) -> tuple[int, int]:
    """Identify an owned filesystem object without mutable directory timestamps."""
    return metadata.st_dev, metadata.st_ino


def private_parent(metadata: os.stat_result) -> None:
    """Require the private parent; its exclusive lease remains with the caller."""
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SnapshotError("snapshot output parent must be current-user private 0700")


def _check_binding(parent: int, name: str, expected: tuple[int, int]) -> None:
    if node(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
        raise SnapshotError("snapshot directory ownership changed")


def _create_directory(parent: int, name: str) -> tuple[int, tuple[int, int]]:
    """Clean ordinary setup failure while the caller holds its exclusive lease."""
    os.mkdir(name, mode=0o700, dir_fd=parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        metadata = os.fstat(descriptor)
        private_parent(metadata)
        return descriptor, node(metadata)
    except BaseException as failure:
        if descriptor is not None:
            os.close(descriptor)
        # The exclusive construction lease owns this name after successful mkdir.
        # This is setup cleanup, not an inode-based defence against another writer.
        try:
            os.rmdir(name, dir_fd=parent)
        except OSError as exc:
            failure.add_note(f"Snapshot directory setup cleanup is incomplete: {exc}")
        raise


@contextmanager
def directory(path: Path, deadline: float) -> Iterator[int]:
    """Hold every absolute ancestor without following links and recheck its name."""
    if not path.is_absolute():
        raise SnapshotError("snapshot directory must be absolute")
    descriptors = [os.open(path.anchor, _DIRECTORY_FLAGS)]
    bindings: list[tuple[int, str, tuple[int, int]]] = []
    try:
        for part in path.parts[1:]:
            remaining(deadline)
            if part in {"", ".", ".."}:
                raise SnapshotError("snapshot directory is not canonical")
            parent = descriptors[-1]
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
            descriptors.append(child)
            bindings.append((parent, part, node(os.fstat(child))))
        yield descriptors[-1]
        for parent, name, expected in bindings:
            remaining(deadline)
            _check_binding(parent, name, expected)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class _Publication:
    """Keep descriptors and an identity ledger for only the objects we create."""

    def __init__(self, parent: int, name: str, deadline: float) -> None:
        """Create a private root and retain an independent parent descriptor."""
        self.deadline = deadline
        self.directories: list[tuple[int, str, int, tuple[int, int]]] = []
        self.files: list[tuple[int, str, tuple[int, int], tuple[int, ...]]] = []
        self.parent = os.dup(parent)
        try:
            root, identity = _create_directory(self.parent, name)
        except BaseException:
            os.close(self.parent)
            raise
        self.directories.append((self.parent, name, root, identity))
        self.paths = {"": root}

    def _parent(self, name: str) -> int:
        relative = ""
        for part in name.split("/")[:-1]:
            remaining(self.deadline)
            parent = self.paths[relative]
            relative = f"{relative}/{part}" if relative else part
            if relative not in self.paths:
                descriptor, identity = _create_directory(parent, part)
                self.directories.append((parent, part, descriptor, identity))
                self.paths[relative] = descriptor
        return self.paths[relative]

    def write(self, name: str, data: bytes, mode: int) -> None:
        """Write and verify through one held regular-file descriptor."""
        parent = self._parent(name)
        leaf = name.split("/")[-1]
        descriptor = os.open(
            leaf,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            expected = node(os.fstat(descriptor))
            self.files.append((parent, leaf, expected, ()))
            offset = 0
            while offset < len(data):
                remaining(self.deadline)
                count = os.write(descriptor, data[offset : offset + 65536])
                if count <= 0:
                    raise SnapshotError("snapshot write made no progress")
                offset += count
            os.lseek(descriptor, 0, os.SEEK_SET)
            offset = 0
            while offset < len(data):
                remaining(self.deadline)
                block = os.read(descriptor, min(65536, len(data) - offset))
                if not block or block != data[offset : offset + len(block)]:
                    raise SnapshotError("published snapshot bytes differ")
                offset += len(block)
            os.fchmod(descriptor, mode)
            metadata = os.fstat(descriptor)
            if (
                metadata.st_size != len(data)
                or metadata.st_nlink != 1
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != mode
            ):
                raise SnapshotError("published snapshot mode or identity differs")
            _check_binding(parent, leaf, expected)
            self.files[-1] = (parent, leaf, expected, _file_identity(metadata))
        finally:
            os.close(descriptor)

    def verify(self) -> None:
        """Check every owned name and the complete directory membership."""
        members: dict[int, set[str]] = {entry[2]: set() for entry in self.directories}
        for parent, name, descriptor, expected in self.directories:
            remaining(self.deadline)
            _check_binding(parent, name, expected)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
                raise SnapshotError("published snapshot directory mode differs")
            if parent in members:
                members[parent].add(name)
        for parent, name, expected, identity in self.files:
            _check_binding(parent, name, expected)
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _file_identity(metadata) != identity:
                raise SnapshotError("published snapshot file changed")
            members[parent].add(name)
        for descriptor, expected_names in members.items():
            remaining(self.deadline)
            if set(os.listdir(descriptor)) != expected_names:
                raise SnapshotError("published snapshot membership changed")

    def cleanup(self, failure: BaseException) -> None:
        """Remove owned names and report incomplete cleanup on the original failure."""
        for parent, name, expected, _ in reversed(self.files):
            try:
                _check_binding(parent, name, expected)
                os.unlink(name, dir_fd=parent)
            except (OSError, SnapshotError) as exc:
                failure.add_note(f"Snapshot file cleanup is incomplete: {exc}")
        for parent, name, _, expected in reversed(self.directories):
            try:
                _check_binding(parent, name, expected)
                os.rmdir(name, dir_fd=parent)
            except (OSError, SnapshotError) as exc:
                failure.add_note(f"Snapshot directory cleanup is incomplete: {exc}")

    def close(self) -> None:
        """Release descriptors after publication or owned cleanup."""
        for _, _, descriptor, _ in reversed(self.directories):
            os.close(descriptor)
        os.close(self.parent)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def publish(path: Path, files: Mapping[str, tuple[bytes, int]], deadline: float) -> None:
    """Publish a new private tree; preserve foreign replacements on any failure."""
    publication: _Publication | None = None
    try:
        with directory(path.parent, deadline) as parent:
            private_parent(os.fstat(parent))
            publication = _Publication(parent, path.name, deadline)
            for name, (data, mode) in files.items():
                remaining(deadline)
                publication.write(name, data, mode)
            publication.verify()
    except BaseException as failure:
        if publication is not None:
            publication.cleanup(failure)
        raise
    finally:
        if publication is not None:
            publication.close()
