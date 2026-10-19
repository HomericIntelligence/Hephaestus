"""Behavior tests for managed worktree identity helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.utils.worktree_identity import is_managed_source_lane_path


@pytest.mark.parametrize("name", ["auto-7-impl", "auto-7-review", "auto-2910-impl"])
def test_a_managed_source_lane_is_recognized(tmp_path: Path, name: str) -> None:
    """A lane worktree under a worker base is the automation's own worktree."""
    lane = tmp_path / "build" / ".worktrees" / name
    lane.parent.mkdir(parents=True)
    lane.mkdir()

    assert is_managed_source_lane_path(lane) is True


@pytest.mark.parametrize(
    "relative",
    [
        "linked-caller",
        "build/.worktrees/auto-7-guard",
        "build/.worktrees/auto-impl",
        "build/.worktrees/auto-x-impl",
        "build/worktrees/auto-7-impl",
    ],
)
def test_a_foreign_worktree_is_not_a_managed_source_lane(tmp_path: Path, relative: str) -> None:
    """A path outside the managed lane naming is not the automation's own worktree."""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()

    assert is_managed_source_lane_path(path) is False
