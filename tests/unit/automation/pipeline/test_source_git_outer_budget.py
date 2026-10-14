"""Tests for source Git checks inside the active worker budget."""

import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.automation import git_runtime, source_worktree


def test_source_git_does_not_start_after_outer_budget_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source Git call must retain the outer deadline without an override."""
    child = Mock()
    tracked = Mock()
    monkeypatch.setattr(subprocess, "run", child)
    monkeypatch.setattr(source_worktree, "run_subprocess", tracked)
    with git_runtime.operation_deadline(time.monotonic() - 1):
        with pytest.raises(subprocess.TimeoutExpired):
            source_worktree._git(tmp_path, "status", "--porcelain")
    child.assert_not_called()
    tracked.assert_not_called()


def test_source_git_uses_tracked_child_with_outer_remaining_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outer worker budget must bound and track source Git children."""
    child = Mock()
    tracked = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(subprocess, "run", child)
    monkeypatch.setattr(source_worktree, "run_subprocess", tracked)
    with git_runtime.operation_deadline(time.monotonic() + 1):
        source_worktree._git(tmp_path, "status", "--porcelain")
    child.assert_not_called()
    tracked.assert_called_once()
    assert 0 < tracked.call_args.kwargs["timeout"] <= 1
    assert tracked.call_args.kwargs["track_process_group"] is True
