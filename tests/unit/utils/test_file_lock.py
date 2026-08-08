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

from hephaestus.utils.file_lock import LockUnavailableError, file_lock


class TestFileLock:
    """Behaviour of the ``file_lock`` context manager."""

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
