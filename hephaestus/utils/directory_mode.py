"""Private directory creation for automation state."""

from __future__ import annotations

import os
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700


def create_private_directory(
    path: Path | str,
    *,
    dir_fd: int | None = None,
    mode: int = PRIVATE_DIRECTORY_MODE,
    parents: bool = False,
) -> bool:
    """Create one private directory and report whether this call created it.

    A directory that this call created ends at ``mode``, even when the
    filesystem ignores the requested mode. An existing directory keeps its
    mode, so the caller can still reject it.

    Args:
        path: The directory to create.
        dir_fd: An open descriptor of the parent directory. The child is
            created under it, so a caller does not follow a replaced path.
        mode: The mode for the new directory.
        parents: Whether to create the missing parent directories.

    Returns:
        Whether this call created the directory.

    """
    try:
        if dir_fd is None:
            Path(path).mkdir(mode=mode, parents=parents)
        else:
            os.mkdir(os.fspath(path), mode, dir_fd=dir_fd)
    except FileExistsError:
        return False
    return True


def normalize_private_directory(descriptor: int, *, mode: int = PRIVATE_DIRECTORY_MODE) -> None:
    """Set one owned directory descriptor to a private mode.

    The caller owns the directory. The mode is set on the live descriptor, so
    a replaced path cannot change what is set.
    """
    os.fchmod(descriptor, mode)
