"""Behavior tests for private directory creation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hephaestus.utils.directory_mode import (
    create_private_directory,
    normalize_private_directory,
)


def test_a_created_directory_is_private(tmp_path: Path) -> None:
    """A directory that this call created ends at the private mode."""
    target = tmp_path / "state"

    created = create_private_directory(target)

    assert created is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_a_created_directory_is_normalized_when_the_filesystem_ignores_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A created directory ends at the private mode even if the mode is ignored."""
    target = tmp_path / "state"
    real_mkdir = Path.mkdir

    def fake_mkdir(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        real_mkdir(self, 0o757, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    created = create_private_directory(target)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(target, flags)
    try:
        normalize_private_directory(descriptor)
    finally:
        os.close(descriptor)

    assert created is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_an_existing_directory_keeps_its_mode(tmp_path: Path) -> None:
    """An existing directory keeps its mode, so the caller can still reject it."""
    target = tmp_path / "state"
    target.mkdir(mode=0o755)
    target.chmod(0o755)

    created = create_private_directory(target)

    assert created is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_a_directory_under_an_open_parent_uses_that_parent(tmp_path: Path) -> None:
    """The child is created under the open parent descriptor."""
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(parent, flags)
    try:
        created = create_private_directory("state", dir_fd=descriptor)
    finally:
        os.close(descriptor)

    assert created is True
    assert (parent / "state").is_dir()
