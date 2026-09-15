#!/usr/bin/env python3
"""Unit tests for ``hephaestus.utils.file_lock``.

Covers the cross-process advisory lock context manager: acquire/release
round-trip, sequential re-acquisition, non-blocking contention, symlink refusal,
and graceful no-op when ``fcntl`` is unavailable (Windows).
"""

from __future__ import annotations

import builtins
import os
from pathlib import Path

import pytest

from hephaestus.utils.file_lock import (
    ExclusiveLockUnavailableError,
    LockUnavailableError,
    file_lock,
    file_lock_at,
)


class TestFileLock:
    """Behaviour of the ``file_lock`` context manager."""

    @pytest.mark.skipif(os.name == "nt", reason="native descriptor locks require POSIX")
    @pytest.mark.parametrize("name", ["metadata.lock", "metadata.lock.owner.lock"])
    @pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo", "mode", "owner"])
    def test_file_lock_at_rejects_unsafe_existing_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        unsafe_kind: str,
    ) -> None:
        """Descriptor-relative exclusive locking verifies an existing entry."""
        target = tmp_path / "unrelated"
        target.write_bytes(b"unchanged")
        target.chmod(0o600)
        entry = tmp_path / name
        if unsafe_kind == "symlink":
            entry.symlink_to(target)
        elif unsafe_kind == "hardlink":
            os.link(target, entry)
        elif unsafe_kind == "fifo":
            os.mkfifo(entry, 0o600)
        else:
            entry.write_bytes(b"entry")
            entry.chmod(0o644 if unsafe_kind == "mode" else 0o600)
            if unsafe_kind == "owner":
                monkeypatch.setattr(os, "geteuid", lambda: entry.stat().st_uid + 1)
        parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            with pytest.raises(ExclusiveLockUnavailableError):
                with file_lock_at(parent_fd, name, require_exclusive=True):
                    pytest.fail("an unsafe existing entry was locked")
        finally:
            os.close(parent_fd)
        assert target.read_bytes() == b"unchanged"
        assert target.stat().st_nlink == (2 if unsafe_kind == "hardlink" else 1)
        if unsafe_kind == "mode":
            assert entry.stat().st_mode & 0o777 == 0o644

    @pytest.mark.skipif(os.name == "nt", reason="native descriptor locks require POSIX")
    @pytest.mark.parametrize("name", ["metadata.lock", "metadata.lock.owner.lock"])
    def test_file_lock_at_creates_one_safe_entry(self, tmp_path: Path, name: str) -> None:
        """A missing descriptor-relative entry is created with exact safe metadata."""
        parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            with file_lock_at(parent_fd, name, require_exclusive=True):
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                assert metadata.st_mode & 0o777 == 0o600
                assert metadata.st_nlink == 1
                assert metadata.st_uid == os.geteuid()
        finally:
            os.close(parent_fd)

    @pytest.mark.parametrize(
        "capability",
        ["nofollow", "dir_fd", "stat_dir_fd", "stat_follow_symlinks", "fcntl"],
    )
    def test_file_lock_at_requires_safe_open_capabilities(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: str
    ) -> None:
        """Exclusive descriptor locking fails closed without safe open support."""
        if capability == "nofollow":
            monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        elif capability == "dir_fd":
            monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {os.open})
        elif capability == "stat_dir_fd":
            monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {os.stat})
        elif capability == "stat_follow_symlinks":
            monkeypatch.setattr(
                os,
                "supports_follow_symlinks",
                os.supports_follow_symlinks - {os.stat},
            )
        else:
            real_import = builtins.__import__

            def fake_import(name: str, *args: object, **kwargs: object) -> object:
                if name == "fcntl":
                    raise ImportError("simulated: no fcntl on this platform")
                return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ExclusiveLockUnavailableError):
            with file_lock_at(-1, "metadata.lock", require_exclusive=True):
                pytest.fail("an unsupported exclusive lock was admitted")
        assert not (tmp_path / "metadata.lock").exists()

    def test_acquire_release_round_trip_creates_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "x.lock"
        with file_lock(lock_path):
            assert lock_path.exists()
        # File persists after release (we never unlink while/after holding).
        assert lock_path.exists()

    def test_sequential_reacquire_succeeds(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "x.lock"
        for _ in range(3):
            with file_lock(lock_path):
                pass  # released at block exit, so the next acquire succeeds

    def test_non_blocking_raises_when_already_held(self, tmp_path: Path) -> None:
        """``blocking=False`` raises LockUnavailableError on contention.

        ``fcntl.flock`` is advisory per open file description. Hold the lock on
        one fd, then a non-blocking acquire on a *separate* fd must fail.
        """
        fcntl = pytest.importorskip("fcntl")
        lock_path = tmp_path / "x.lock"
        held_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(held_fd, fcntl.LOCK_EX)
            with pytest.raises(LockUnavailableError):
                with file_lock(lock_path, blocking=False):
                    pass
        finally:
            fcntl.flock(held_fd, fcntl.LOCK_UN)
            os.close(held_fd)

    def test_refuses_symlinked_path(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        target = tmp_path / "real"
        target.write_text("", encoding="utf-8")
        link = tmp_path / "link.lock"
        link.symlink_to(target)
        with pytest.raises(RuntimeError):
            with file_lock(link):
                pass

    def test_no_fcntl_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``fcntl`` import fails (Windows), the lock degrades to a no-op."""
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "fcntl":
                raise ImportError("simulated: no fcntl on this platform")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Must not raise, and must not require/lock anything.
        with file_lock(tmp_path / "x.lock"):
            pass

    def test_no_fcntl_required_lock_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-idempotent caller can reject a no-op lock on Windows."""
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "fcntl":
                raise ImportError("simulated: no fcntl on this platform")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(LockUnavailableError, match="Exclusive file locking is unavailable"):
            with file_lock(tmp_path / "x.lock", require_exclusive=True):
                pass
