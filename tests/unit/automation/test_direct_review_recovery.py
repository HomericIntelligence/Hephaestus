"""Tests for durable detached-review recovery receipts."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from hephaestus.automation import direct_review_recovery
from hephaestus.automation.direct_review_recovery import (
    list_direct_review_recovery_paths,
    record_direct_review_recovery,
)
from hephaestus.automation.models import DEFAULT_STATE_DIR


def _worktree(repo_root: Path, issue: int) -> Path:
    """Create a direct-review checkout fixture path."""
    path = repo_root / "build" / ".worktrees" / f"review-pr-{issue}"
    path.mkdir(parents=True)
    (path / ".git").mkdir()
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


def test_receipts_fail_closed_without_no_follow_directory_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Portable fallbacks preserve without creating a race-prone receipt."""
    worktree = _worktree(tmp_path, 2500)
    monkeypatch.setattr(direct_review_recovery, "_DIR_FD_SUPPORTED", False)

    with pytest.raises(ValueError, match="no-follow directory descriptors"):
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


def test_receipt_rejects_worktree_git_metadata_outside_the_repository(tmp_path: Path) -> None:
    """A mutable linked-worktree gitdir cannot redirect recovery marker writes."""
    worktree = _worktree(tmp_path, 2500)
    shutil.rmtree(worktree / ".git")
    outside = tmp_path.parent / "outside-git-metadata"
    outside.mkdir(exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {outside}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Git metadata"):
        record_direct_review_recovery(
            repo_root=tmp_path,
            issue=2500,
            pr=2501,
            worktree=worktree,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )

    assert not (outside / "hephaestus-direct-review-recovery").exists()


def test_receipt_rejects_a_symlinked_repository_git_metadata(tmp_path: Path) -> None:
    """A root Git metadata symlink cannot redirect linked-worktree markers."""
    worktree = _worktree(tmp_path, 2500)
    shutil.rmtree(worktree / ".git")
    outside = tmp_path.parent / "outside-common-git"
    review_git_dir = outside / ".git" / "worktrees" / "review-pr-2500"
    review_git_dir.mkdir(parents=True)
    pointer = tmp_path / "root-git-pointer"
    pointer.write_text(f"gitdir: {outside / '.git' / 'worktrees' / 'root'}\n", encoding="utf-8")
    (tmp_path / ".git").symlink_to(pointer)
    (worktree / ".git").write_text(f"gitdir: {review_git_dir}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="repository Git metadata is symlinked"):
        record_direct_review_recovery(
            repo_root=tmp_path,
            issue=2500,
            pr=2501,
            worktree=worktree,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )

    assert not (review_git_dir / "hephaestus-direct-review-recovery").exists()


def test_receipt_rejects_forged_linked_repository_git_metadata(tmp_path: Path) -> None:
    """A root gitfile must prove its common metadata directory before marker I/O."""
    repo_root = tmp_path / "linked-root"
    worktree = repo_root / "build" / ".worktrees" / "review-pr-2500"
    worktree.mkdir(parents=True)
    outside_git_dir = tmp_path / "outside" / ".git"
    root_git_dir = outside_git_dir / "worktrees" / "linked-root"
    review_git_dir = outside_git_dir / "worktrees" / "review-pr-2500"
    root_git_dir.mkdir(parents=True)
    review_git_dir.mkdir()
    (repo_root / ".git").write_text(f"gitdir: {root_git_dir}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {review_git_dir}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="repository Git metadata is unavailable"):
        record_direct_review_recovery(
            repo_root=repo_root,
            issue=2500,
            pr=2501,
            worktree=worktree,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )

    assert not (review_git_dir / "hephaestus-direct-review-recovery").exists()


def test_receipt_rejects_root_git_metadata_swapped_to_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root gitfile is opened no-follow after the trusted-root descriptor exists."""
    repo_root = tmp_path / "linked-root"
    worktree = repo_root / "build" / ".worktrees" / "review-pr-2500"
    worktree.mkdir(parents=True)
    common_git_dir = tmp_path / "common" / ".git"
    root_git_dir = common_git_dir / "worktrees" / "linked-root"
    review_git_dir = common_git_dir / "worktrees" / "review-pr-2500"
    root_git_dir.mkdir(parents=True)
    review_git_dir.mkdir()
    (root_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    root_dot_git = repo_root / ".git"
    root_dot_git.write_text(f"gitdir: {root_git_dir}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {review_git_dir}\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    forged_pointer = outside / "git-pointer"
    forged_pointer.write_text(
        f"gitdir: {outside / '.git' / 'worktrees' / 'linked-root'}\n", encoding="utf-8"
    )
    original_open = os.open
    swapped = False

    def open_after_swap(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """Replace the gitfile just before its descriptor-relative open."""
        nonlocal swapped
        if not swapped and path == ".git" and dir_fd is not None:
            root_dot_git.unlink()
            root_dot_git.symlink_to(forged_pointer)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_after_swap)

    with pytest.raises(ValueError, match="repository Git metadata is symlinked"):
        record_direct_review_recovery(
            repo_root=repo_root,
            issue=2500,
            pr=2501,
            worktree=worktree,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )

    outside_marker = (
        outside / ".git" / "worktrees" / "review-pr-2500" / "hephaestus-direct-review-recovery"
    )
    assert not outside_marker.exists()


def test_receipt_rejects_worktree_git_metadata_swapped_to_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review gitfile is opened no-follow after the worktree descriptor exists."""
    worktree = _worktree(tmp_path, 2500)
    shutil.rmtree(worktree / ".git")
    git_dir = tmp_path / ".git" / "worktrees" / "review-pr-2500"
    other_git_dir = tmp_path / ".git" / "worktrees" / "other"
    git_dir.mkdir(parents=True)
    other_git_dir.mkdir()
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    forged_pointer = tmp_path / "forged-worktree-git-pointer"
    forged_pointer.write_text(f"gitdir: {other_git_dir}\n", encoding="utf-8")
    original_open = os.open
    swapped = False
    worktree_stat = worktree.stat()

    def open_after_swap(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """Replace the review gitfile just before its descriptor-relative open."""
        nonlocal swapped
        if (
            not swapped
            and path == ".git"
            and dir_fd is not None
            and os.path.samestat(os.fstat(dir_fd), worktree_stat)
        ):
            (worktree / ".git").unlink()
            (worktree / ".git").symlink_to(forged_pointer)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_after_swap)

    with pytest.raises(ValueError, match="Git metadata is symlinked"):
        record_direct_review_recovery(
            repo_root=tmp_path,
            issue=2500,
            pr=2501,
            worktree=worktree,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )

    assert not (other_git_dir / "hephaestus-direct-review-recovery").exists()


def test_receipt_rejects_normal_root_git_directory_swapped_to_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified root Git directory cannot be replaced before marker I/O."""
    worktree = _worktree(tmp_path, 2500)
    shutil.rmtree(worktree / ".git")
    git_dir = tmp_path / ".git" / "worktrees" / "review-pr-2500"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    root_git_dir = tmp_path / ".git"
    displaced_git_dir = tmp_path / "displaced-git"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_common_git_dir = direct_review_recovery._common_git_dir

    def common_git_dir_after_swap(repo_root: Path) -> Path:
        """Swap the root metadata path after it is verified but before reopening it."""
        common_git_dir = original_common_git_dir(repo_root)
        root_git_dir.rename(displaced_git_dir)
        root_git_dir.symlink_to(outside, target_is_directory=True)
        return common_git_dir

    monkeypatch.setattr(direct_review_recovery, "_common_git_dir", common_git_dir_after_swap)

    with pytest.raises(ValueError, match="Git metadata is unavailable"):
        record_direct_review_recovery(
            repo_root=tmp_path,
            issue=2500,
            pr=2501,
            worktree=worktree,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )

    assert not (
        outside / "worktrees" / "review-pr-2500" / "hephaestus-direct-review-recovery"
    ).exists()


def test_receipt_supports_and_binds_a_linked_worktree_gitdir(tmp_path: Path) -> None:
    """The production linked-worktree gitdir layout remains receipt-backed."""
    worktree = _worktree(tmp_path, 2500)
    shutil.rmtree(worktree / ".git")
    git_dir = tmp_path / ".git" / "worktrees" / "review-pr-2500"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (worktree / ".git").write_text(
        "gitdir: ../../../.git/worktrees/review-pr-2500\n", encoding="utf-8"
    )

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


def test_receipt_supports_a_linked_repository_root(tmp_path: Path) -> None:
    """Recovery works when the loop itself runs from a linked checkout."""
    repo_root = tmp_path / "linked-root"
    worktree = repo_root / "build" / ".worktrees" / "review-pr-2500"
    worktree.mkdir(parents=True)
    common_git_dir = tmp_path / "common" / ".git"
    root_git_dir = common_git_dir / "worktrees" / "linked-root"
    review_git_dir = common_git_dir / "worktrees" / "review-pr-2500"
    root_git_dir.mkdir(parents=True)
    review_git_dir.mkdir()
    (root_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (review_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (review_git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (repo_root / ".git").write_text(f"gitdir: {root_git_dir}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {review_git_dir}\n", encoding="utf-8")

    record_direct_review_recovery(
        repo_root=repo_root,
        issue=2500,
        pr=2501,
        worktree=worktree,
        branch="2500-auto",
        expected_remote_sha="a" * 40,
        source_sha="b" * 40,
    )

    assert list_direct_review_recovery_paths(repo_root=repo_root, issue=2500, pr=2501) == [
        worktree.resolve()
    ]


def test_receipt_write_does_not_follow_a_replaced_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent receipt-directory swap cannot redirect the atomic write."""
    if os.name != "posix":
        pytest.skip("descriptor-relative receipt writes require POSIX")
    worktree = _worktree(tmp_path, 2500)
    outside = tmp_path / "outside"
    outside.mkdir()
    receipt_dir = tmp_path / DEFAULT_STATE_DIR / "direct-review-recovery"
    displaced_dir = tmp_path / "displaced-receipts"
    original_replace = os.replace

    def replace_after_swap(*args: object, **kwargs: object) -> None:
        """Swap the pathname after descriptor acquisition and before replacement."""
        if kwargs.get("dst_dir_fd") is not None and receipt_dir.exists():
            receipt_dir.rename(displaced_dir)
            receipt_dir.symlink_to(outside, target_is_directory=True)
        cast(Any, original_replace)(*args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_after_swap)

    with pytest.raises(ValueError, match="receipt directory changed"):
        record_direct_review_recovery(
            repo_root=tmp_path,
            issue=2500,
            pr=2501,
            worktree=worktree,
            branch="2500-auto",
            expected_remote_sha="a" * 40,
            source_sha="b" * 40,
        )

    assert list(outside.iterdir()) == []


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

    shutil.rmtree(worktree)
    worktree.mkdir()
    (worktree / ".git").mkdir()

    assert list_direct_review_recovery_paths(repo_root=tmp_path, issue=2500, pr=2501) == []
