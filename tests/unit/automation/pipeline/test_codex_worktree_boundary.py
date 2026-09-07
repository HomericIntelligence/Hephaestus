"""Behavior tests for the held Codex implementation Git boundary."""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


def _git(path: Path, *args: str) -> str:
    """Run one Git command in the test repository."""
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _linked_worktree(tmp_path: Path) -> Path:
    """Create one linked worktree with separate repository configuration."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test User")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "test: create repository")
    _git(repository, "config", "extensions.worktreeConfig", "true")
    worktree = tmp_path / "worktree"
    _git(repository, "worktree", "add", "-b", "test-worktree", str(worktree))
    _git(worktree, "config", "--worktree", "test.boundary", "enabled")
    return worktree


def _boundary_module():
    """Load the boundary module after its missing-module RED check."""
    module_name = "hephaestus.automation.pipeline.codex_worktree_boundary"
    assert importlib.util.find_spec(module_name) is not None, (
        "the Codex worktree boundary is not implemented"
    )
    return importlib.import_module(module_name)


def test_receipt_binds_git_pointer_directories_index_and_configs(tmp_path: Path) -> None:
    """The receipt binds each Git control path and its fixed child values."""
    worktree = _linked_worktree(tmp_path)
    module = _boundary_module()

    with module.capture_codex_worktree_boundary(worktree) as boundary:
        receipt = boundary.receipt
        fixed = dict(receipt.fixed_environment)

        assert receipt.canonical_worktree == str(worktree.resolve())
        assert Path(receipt.protected_paths[0]).is_file()
        assert Path(receipt.git_dir).is_dir()
        assert Path(receipt.common_dir).is_dir()
        assert Path(receipt.index).is_file()
        assert Path(receipt.repository_config).is_file()
        assert Path(receipt.worktree_config).is_file()
        assert fixed == {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_DIR": receipt.git_dir,
            "GIT_INDEX_FILE": receipt.index,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_WORK_TREE": receipt.canonical_worktree,
        }
        boundary.verify_before_launch()
        boundary.verify_after_return()


@pytest.mark.parametrize(
    ("scope", "key"),
    [
        pytest.param("--local", "core.hooksPath", id="repository-hooks"),
        pytest.param("--local", "core.fsmonitor", id="repository-fsmonitor"),
        pytest.param("--worktree", "core.hooksPath", id="worktree-hooks"),
        pytest.param("--worktree", "core.fsmonitor", id="worktree-fsmonitor"),
    ],
)
def test_receipt_rejects_worktree_hooks_and_fsmonitor(tmp_path: Path, scope: str, key: str) -> None:
    """The receipt rejects hooks and file-system monitors in both configs."""
    worktree = _linked_worktree(tmp_path)
    _git(worktree, "config", scope, key, "unsafe")
    module = _boundary_module()

    with pytest.raises(module.CodexWorktreeBoundaryError, match="unsafe Git configuration"):
        module.capture_codex_worktree_boundary(worktree)


@pytest.mark.parametrize("scope", ["--local", "--worktree"])
def test_receipt_rejects_repository_and_worktree_includes(tmp_path: Path, scope: str) -> None:
    """The receipt rejects configuration includes from each bound source."""
    worktree = _linked_worktree(tmp_path)
    _git(worktree, "config", scope, "include.path", str(tmp_path / "other-config"))
    module = _boundary_module()

    with pytest.raises(module.CodexWorktreeBoundaryError, match="unsafe Git configuration"):
        module.capture_codex_worktree_boundary(worktree)


def test_policy_makes_git_entry_and_all_metadata_read_only(tmp_path: Path) -> None:
    """The adapter policy separates the worktree from protected Git metadata."""
    worktree = _linked_worktree(tmp_path)
    module = _boundary_module()

    with module.capture_codex_worktree_boundary(worktree) as boundary:
        receipt = boundary.receipt

        assert receipt.read_write_paths == (receipt.canonical_worktree,)
        assert receipt.protected_paths == (str(worktree.resolve() / ".git"),)
        assert set(receipt.read_only_paths) == {
            receipt.git_dir,
            receipt.common_dir,
            receipt.index,
            receipt.repository_config,
            receipt.worktree_config,
        }


@pytest.mark.parametrize("phase", ["before", "after"])
def test_receipt_detects_replacement_before_launch_and_after_return(
    tmp_path: Path, phase: str
) -> None:
    """A path replacement fails the applicable descriptor identity check."""
    worktree = _linked_worktree(tmp_path)
    module = _boundary_module()

    with module.capture_codex_worktree_boundary(worktree) as boundary:
        index = Path(boundary.receipt.index)
        original = index.read_bytes()
        index.unlink()
        index.write_bytes(original)

        verify = (
            boundary.verify_before_launch if phase == "before" else boundary.verify_after_return
        )
        with pytest.raises(module.CodexWorktreeBoundaryError, match="Git control identity changed"):
            verify()


def test_receipt_detects_content_change_after_return(tmp_path: Path) -> None:
    """A bound file content change fails the post-invocation digest check."""
    worktree = _linked_worktree(tmp_path)
    module = _boundary_module()

    with module.capture_codex_worktree_boundary(worktree) as boundary:
        config = Path(boundary.receipt.worktree_config)
        config.write_text(config.read_text(encoding="utf-8") + "[test]\nvalue = changed\n")

        with pytest.raises(module.CodexWorktreeBoundaryError, match="Git control content changed"):
            boundary.verify_after_return()


def test_receipt_rejects_unrelated_common_directory(tmp_path: Path) -> None:
    """The receipt rejects a common directory outside the linked-worktree layout."""
    worktree = _linked_worktree(tmp_path)
    module = _boundary_module()
    git_dir = Path(_git(worktree, "rev-parse", "--git-dir"))
    unrelated = tmp_path / "unrelated-common"
    unrelated.mkdir()
    (unrelated / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (git_dir / "commondir").write_text(str(unrelated), encoding="utf-8")

    with pytest.raises(module.CodexWorktreeBoundaryError, match="escapes"):
        module.capture_codex_worktree_boundary(worktree)


def test_receipt_rejects_parent_link_swap_during_git_directory_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent link swap cannot redirect the held Git directory descriptor."""
    worktree = _linked_worktree(tmp_path)
    module = _boundary_module()
    git_dir = Path(_git(worktree, "rev-parse", "--git-dir"))
    repository = git_dir.parents[2]
    moved_repository = tmp_path / "moved-repository"
    real_open = module.os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        value = os.fsdecode(path)
        opens_complete_git_path = dir_fd is None and value == str(git_dir)
        opens_git_parent_component = dir_fd is not None and value == repository.name
        if not swapped and (opens_complete_git_path or opens_git_parent_component):
            repository.rename(moved_repository)
            repository.symlink_to(moved_repository, target_is_directory=True)
            swapped = True
            try:
                return real_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                repository.unlink()
                moved_repository.rename(repository)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "open", racing_open)

    with pytest.raises(module.CodexWorktreeBoundaryError, match="Git control path"):
        module.capture_codex_worktree_boundary(worktree)

    assert swapped is True
