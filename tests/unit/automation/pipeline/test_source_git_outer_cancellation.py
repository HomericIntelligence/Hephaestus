"""Tests for source Git cancellation in an active operation."""

import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.automation import git_runtime, source_worktree


def test_source_git_obeys_the_outer_cancellation_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source transition checks must stop when the active job is cancelled."""
    shutdown = threading.Event()
    shutdown.set()
    tracked = Mock()
    monkeypatch.setattr(source_worktree, "run_subprocess", tracked)
    with git_runtime.operation_deadline(time.monotonic() + 30, shutdown=shutdown):
        with pytest.raises(InterruptedError):
            source_worktree._git(tmp_path, "status", "--porcelain")
    tracked.assert_not_called()
