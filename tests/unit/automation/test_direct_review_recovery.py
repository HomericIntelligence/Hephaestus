"""Tests for durable detached-review recovery receipts."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.automation.direct_review_recovery import (
    list_direct_review_recovery_paths,
    record_direct_review_recovery,
)
from hephaestus.automation.models import DEFAULT_STATE_DIR


def _worktree(repo_root: Path, issue: int) -> Path:
    """Create a direct-review checkout fixture path."""
    path = repo_root / "build" / ".worktrees" / f"review-pr-{issue}"
    path.mkdir(parents=True)
    return path


def test_receipt_backed_recovery_is_discoverable_across_manager_instances(tmp_path: Path) -> None:
    """Only an authoritative remote-drift receipt makes a path recoverable."""
    worktree = _worktree(tmp_path, 2500)

    record_direct_review_recovery(
        repo_root=tmp_path,
        issue=2500,
        pr=2501,
        worktree=worktree,
        branch="2500-auto",
        expected_remote_sha="a" * 40,
        source_sha="b" * 40,
    )

    assert list_direct_review_recovery_paths(repo_root=tmp_path, issue=2500, pr=2501) == [
        worktree.resolve()
    ]


def test_active_unreceipted_checkout_is_never_reported_as_recovery(tmp_path: Path) -> None:
    """Collision avoidance must not turn a live checkout into cleanup guidance."""
    _worktree(tmp_path, 2500)

    assert list_direct_review_recovery_paths(repo_root=tmp_path, issue=2500, pr=2501) == []


def test_receipt_rejects_a_worktree_outside_the_isolated_review_root(tmp_path: Path) -> None:
    """A receipt cannot make an arbitrary path eligible for summary output."""
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="worktree is invalid"):
        record_direct_review_recovery(
            repo_root=tmp_path,
            issue=2500,
            pr=2501,
            worktree=outside,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )


def test_receipt_rejects_a_symlinked_state_directory(tmp_path: Path) -> None:
    """Receipt reads and writes never follow a state-directory symlink."""
    worktree = _worktree(tmp_path, 2500)
    outside = tmp_path / "outside"
    outside.mkdir()
    state_dir = tmp_path / DEFAULT_STATE_DIR
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    state_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="receipt directory"):
        record_direct_review_recovery(
            repo_root=tmp_path,
            issue=2500,
            pr=2501,
            worktree=worktree,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )

    assert list_direct_review_recovery_paths(repo_root=tmp_path, issue=2500, pr=2501) == []


def test_receipt_does_not_authorize_a_replacement_checkout_at_the_same_path(
    tmp_path: Path,
) -> None:
    """A stale receipt cannot reclassify a later checkout as an old recovery."""
    worktree = _worktree(tmp_path, 2500)
    record_direct_review_recovery(
        repo_root=tmp_path,
        issue=2500,
        pr=2501,
        worktree=worktree,
        branch="2500-auto",
        expected_remote_sha="a" * 40,
        source_sha="b" * 40,
    )

    worktree.rmdir()
    worktree.mkdir()

    assert list_direct_review_recovery_paths(repo_root=tmp_path, issue=2500, pr=2501) == []
