"""Check worktree creation through a parent-directory alias."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.pipeline.worker_pool import WorkerPool

_WORKER = "hephaestus.automation.pipeline.worker_pool"


def _git(repo: Path, *args: str) -> str:
    """Run Git in a test repository and return its output."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(parent: Path) -> Path:
    """Create a repository with a local main reference."""
    repo = parent / "repository"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", "https://github.com/owner/repo.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo.resolve()


@pytest.mark.skipif(os.name == "nt", reason="parent aliases require directory symlinks")
def test_create_worktree_keeps_admitted_repository_through_parent_alias(
    tmp_path: Path,
) -> None:
    """Creation stays in the admitted repository when its parent alias changes."""
    original = _repository(tmp_path / "original")
    replacement = _repository(tmp_path / "replacement")
    alias = tmp_path / "alias"
    alias.symlink_to(original.parent, target_is_directory=True)
    pool = WorkerPool(1, threading.Event(), queue.Queue(), lock_dir=tmp_path / "locks")
    authenticate = pool._authenticated_remote_git_configuration
    validation_count = 0

    def authenticate_and_change_alias(
        *,
        cwd: Path | None = None,
        expected_repo: str | None = None,
        timeout: int = 60,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Change the alias after the admitted checkout passes revalidation."""
        nonlocal validation_count
        result = authenticate(cwd=cwd, expected_repo=expected_repo, timeout=timeout)
        validation_count += 1
        if validation_count == 2:
            alias.unlink()
            alias.symlink_to(replacement.parent, target_is_directory=True)
        return result

    job = GitJob(
        repo="repo",
        expected_repository="owner/repo",
        op="create_worktree",
        timeout_s=5,
        deadline_s=time.monotonic() + 5,
        kwargs={
            "issue_number": 7,
            "branch_name": "7-auto",
            "repo_root": str(alias / "repository"),
        },
    )
    try:
        with (
            patch(f"{_WORKER}._trusted_gh_executable", return_value="gh"),
            patch(f"{_WORKER}._trusted_remote_git_config", return_value=()),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                side_effect=authenticate_and_change_alias,
            ),
        ):
            result = pool._run_git(job)
    finally:
        pool.shutdown()

    assert alias.resolve() == replacement.parent
    assert result.ok is True, result.error
    expected = original / "build" / ".worktrees" / "issue-7"
    assert expected.is_dir()
    assert Path(result.value["path"]).resolve() == expected
    assert _git(expected, "branch", "--show-current") == "7-auto"
    assert not (replacement / "build").exists()
