"""Tests for the shared dirty worktree content identity."""

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hephaestus.automation import git_utils, worktree_snapshot
from hephaestus.automation.worktree_snapshot import _path_content_identity, _run_bounded_git_output


@pytest.mark.parametrize(
    "candidate",
    (
        Path("/Library/Developer/CommandLineTools/usr/bin/git"),
        Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git"),
    ),
)
def test_snapshot_trusts_exact_apple_developer_git(candidate: Path) -> None:
    """A nested host check keeps the outer verifier's approved Apple Git."""
    mode = stat.S_IFREG | 0o755
    with (
        patch.object(worktree_snapshot.shutil, "which", return_value=str(candidate)),
        patch.object(worktree_snapshot, "_TRUSTED_GIT_CANDIDATES", ()),
        patch.object(Path, "is_symlink", return_value=False),
        patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=mode)),
        patch.object(Path, "is_file", return_value=True),
        patch.object(worktree_snapshot.os, "access", return_value=True),
    ):
        assert worktree_snapshot._trusted_git_executable() == str(candidate)


def test_snapshot_rejects_other_apple_developer_git_path() -> None:
    """A similar Apple path outside the two fixed directories stays untrusted."""
    candidate = Path("/Library/Developer/Other/usr/bin/git")
    mode = stat.S_IFREG | 0o755
    with (
        patch.object(worktree_snapshot.shutil, "which", return_value=str(candidate)),
        patch.object(worktree_snapshot, "_TRUSTED_GIT_CANDIDATES", ()),
        patch.object(Path, "is_symlink", return_value=False),
        patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=mode)),
        patch.object(Path, "is_file", return_value=True),
        patch.object(worktree_snapshot.os, "access", return_value=True),
    ):
        assert worktree_snapshot._trusted_git_executable() is None


def test_content_hash_uses_the_remaining_operation_deadline(tmp_path: Path) -> None:
    """File hashing must not start a new timeout after the Git deadline."""
    (tmp_path / "file").write_text("content")
    with (
        git_utils.operation_deadline(10.0),
        patch("hephaestus.automation.worktree_snapshot.time.monotonic", return_value=11.0),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        _path_content_identity(tmp_path, "file\0", timeout=30)


@pytest.mark.parametrize("selector_supported", [True, False])
def test_snapshot_child_stops_during_cancellation(tmp_path: Path, selector_supported: bool) -> None:
    """Both pipe readers must stop a cancelled capture child."""
    shutdown = threading.Event()
    timer = threading.Timer(0.1, shutdown.set)
    started = time.monotonic()
    timer.start()
    try:
        with (
            patch(
                "hephaestus.automation.worktree_snapshot._subprocess_pipe_selector_supported",
                return_value=selector_supported,
            ),
            pytest.raises(InterruptedError),
        ):
            _run_bounded_git_output(
                (sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"),
                cwd=tmp_path,
                timeout=30,
                max_bytes=1024,
                retain_text=True,
                shutdown=shutdown,
            )
    finally:
        timer.cancel()
        timer.join()
    assert time.monotonic() - started < 5


def test_content_hash_stops_during_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation must stop a file hash between bounded reads."""
    (tmp_path / "file").write_bytes(b"x" * (128 * 1024))
    shutdown = threading.Event()
    read = os.read

    def cancel_after_read(descriptor: int, size: int) -> bytes:
        data = read(descriptor, size)
        shutdown.set()
        return data

    monkeypatch.setattr(os, "read", cancel_after_read)
    with pytest.raises(InterruptedError):
        _path_content_identity(tmp_path, "file\0", timeout=30, shutdown=shutdown)


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
