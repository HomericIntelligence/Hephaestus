"""Synchronize private build storage before an external effect."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from hephaestus.automation.fleet_snapshot_files import (
    directory,
    node,
    private_parent,
    remaining,
)


@contextmanager
def private_directory(path: Path, deadline: float) -> Iterator[int]:
    """Create private descendants, then synchronize their names and final contents.

    The caller holds the exclusive construction lease below an existing private
    ancestor. Keep resources opened in the body inside the caller's cleanup
    scope: synchronization or descriptor-context exit can fail after the body.
    """
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("build storage must be canonical")
    ancestor = path
    missing: list[str] = []
    while True:
        remaining(deadline)
        try:
            ancestor.lstat()
        except FileNotFoundError:
            missing.append(ancestor.name)
            ancestor = ancestor.parent
        else:
            break
    with (
        directory(ancestor.parent, deadline) as parent,
        directory(ancestor, deadline) as initial,
        ExitStack() as descriptors,
    ):
        private_parent(os.fstat(initial))
        # A previous failed construction can leave this existing name unsynced.
        os.fsync(parent)
        os.fsync(initial)
        current = initial
        created: list[tuple[int, str, int, tuple[int, int]]] = []
        for name in reversed(missing):
            remaining(deadline)
            os.mkdir(name, 0o700, dir_fd=current)
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            descriptors.callback(os.close, child)
            metadata = os.fstat(child)
            private_parent(metadata)
            created.append((current, name, child, node(metadata)))
            os.fsync(current)
            current = child
        yield current
        remaining(deadline)
        for owner, name, child, identity in created:
            private_parent(os.fstat(child))
            if node(os.stat(name, dir_fd=owner, follow_symlinks=False)) != identity:
                raise RuntimeError("build storage directory changed during construction")
        private_parent(os.fstat(current))
        os.fsync(current)


@contextmanager
def journal_directory(path: Path, deadline: float) -> Iterator[int]:
    """Synchronize retained build journal bytes and names before replay effects.

    Open the journal in the body under its writer lock. Keep the caller's cleanup
    scope outside this context so a failed synchronization also closes the lock.
    """
    with private_directory(path, deadline) as owner:
        yield owner
        remaining(deadline)
        descriptor = os.open(
            "receipts.jsonl",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=owner,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise RuntimeError("invalid private build journal")
            os.fsync(descriptor)
            binding = os.stat("receipts.jsonl", dir_fd=owner, follow_symlinks=False)
            if node(binding) != node(metadata):
                raise RuntimeError("build journal changed during synchronization")
        finally:
            os.close(descriptor)
