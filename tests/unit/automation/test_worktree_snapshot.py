"""Tests for the shared dirty worktree content identity."""

from pathlib import Path

import pytest

from hephaestus.automation.worktree_snapshot import _path_content_identity


@pytest.mark.parametrize("paths", ["file", "file\0\0", "./file\0", "dir//file\0", "../file\0"])
def test_snapshot_rejects_noncanonical_path_records(tmp_path: Path, paths: str) -> None:
    """Reject ambiguous path records before reading file content."""
    with pytest.raises(RuntimeError, match="unsafe path"):
        _path_content_identity(tmp_path, paths)


def test_snapshot_rejects_fifo(tmp_path: Path) -> None:
    """A special file cannot stand in for captured source content."""
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are unavailable")
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(RuntimeError, match="unsupported path type"):
        _path_content_identity(tmp_path, "pipe\0")
