"""Integration coverage for real Git porcelain type-change handling."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hephaestus.automation import pr_manager

_GIT_REPO_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_posix,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="Real file-to-symlink Git type changes require POSIX symlink semantics",
    ),
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run Git against the temporary integration repository."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def type_changed_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str]:
    """Create a repository with a tracked regular file replaced by a symlink."""
    for key in _GIT_REPO_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.symlinks", "true")

    relative_path = "src/type-changed.py"
    tracked_path = repo / relative_path
    tracked_path.parent.mkdir()
    tracked_path.write_text("regular file\n", encoding="utf-8")
    _git(repo, "add", "--", relative_path)
    _git(
        repo,
        "-c",
        "user.name=Hephaestus Integration Test",
        "-c",
        "user.email=hephaestus-test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--no-gpg-sign",
        "-qm",
        "test: add regular file baseline",
    )

    tracked_path.unlink()
    tracked_path.symlink_to("replacement.py")
    return repo, relative_path


def test_git_produces_real_worktree_type_change(
    type_changed_worktree: tuple[Path, str],
) -> None:
    """A real file-to-symlink replacement must produce worktree status `` T``."""
    repo, relative_path = type_changed_worktree

    assert (repo / relative_path).is_symlink()
    assert pr_manager._read_porcelain_status(repo, git_timeout=10) == (f" T {relative_path}\0")


def test_real_type_change_is_parsed_selected_and_staged(
    type_changed_worktree: tuple[Path, str],
) -> None:
    """The real porcelain record must pass parsing and reach Git's index."""
    repo, relative_path = type_changed_worktree

    porcelain = pr_manager._read_porcelain_status(repo, git_timeout=10)
    entries = pr_manager._parse_porcelain_status(porcelain)
    assert entries == ((" T", relative_path),)

    paths = pr_manager._select_commit_paths(entries, allowed_paths=(relative_path,))
    assert paths == pr_manager._CommitPaths(
        add_paths=(relative_path,),
        update_paths=(),
    )

    pr_manager._stage_commit_paths(paths, repo, git_timeout=10)

    staged = _git(repo, "diff", "--cached", "--name-status")
    assert staged.stdout == f"T\t{relative_path}\n"
