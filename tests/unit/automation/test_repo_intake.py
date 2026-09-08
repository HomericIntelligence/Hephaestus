"""Behavior tests for the isolated repository-intake control plane."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hephaestus.automation.repo_intake import RepoIntakeError, RepoIntakeManager


def _run_git(cwd: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a local Git command for the fixture repository."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _make_repository(tmp_path: Path) -> tuple[Path, Path]:
    """Create a master checkout and a local bare remote with a GitHub URL."""
    remote = tmp_path / "remote.git"
    _run_git(tmp_path, "init", "--bare", str(remote))
    caller = tmp_path / "caller"
    caller.mkdir()
    _run_git(caller, "init", "--initial-branch=master")
    _run_git(caller, "config", "user.name", "Test User")
    _run_git(caller, "config", "user.email", "test@example.invalid")
    (caller / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_git(caller, "add", "tracked.txt")
    _run_git(caller, "commit", "-m", "base")
    _run_git(caller, "remote", "add", "origin", str(remote))
    _run_git(caller, "push", "-u", "origin", "master")
    _run_git(caller, "remote", "set-url", "origin", "https://github.com/acme/repo.git")
    return caller, remote


def _manager(caller: Path, remote: Path) -> RepoIntakeManager:
    """Build a manager with a local transport test double."""

    def runner(
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        log_errors: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del log_errors
        if command[0] == "gh":
            return subprocess.CompletedProcess(command, 0, "master\n", "")
        adjusted = list(command)
        if "fetch" in adjusted:
            adjusted[adjusted.index("origin")] = str(remote)
        return subprocess.run(
            adjusted,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
            env=env,
        )

    return RepoIntakeManager(
        caller,
        repository="acme/repo",
        gh_command="gh",
        timeout_s=30,
        git_runner=runner,
        git_env={},
        remote_config=(),
    )


def _advance_remote(tmp_path: Path, remote: Path) -> None:
    """Push one fast-forward commit without changing the caller checkout."""
    updater = tmp_path / "updater"
    _run_git(tmp_path, "clone", str(remote), str(updater))
    _run_git(updater, "config", "user.name", "Test User")
    _run_git(updater, "config", "user.email", "test@example.invalid")
    (updater / "remote.txt").write_text("remote\n", encoding="utf-8")
    _run_git(updater, "add", "remote.txt")
    _run_git(updater, "commit", "-m", "remote update")
    _run_git(updater, "push", "origin", "master")


def test_isolated_intake_preserves_primary_head_index_and_status(tmp_path: Path) -> None:
    """A tracked and untracked caller stays unchanged while intake syncs."""
    caller, remote = _make_repository(tmp_path)
    before_head = _run_git(caller, "rev-parse", "HEAD").stdout
    (caller / "tracked.txt").write_text("local work\n", encoding="utf-8")
    (caller / "untracked.txt").write_text("keep\n", encoding="utf-8")
    before_index = _run_git(caller, "diff", "--cached").stdout
    before_status = _run_git(caller, "status", "--porcelain", "--untracked-files=all").stdout

    receipt = _manager(caller, remote).prepare()

    assert receipt.path != caller
    assert receipt.revision == _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip()
    assert _run_git(caller, "rev-parse", "HEAD").stdout == before_head
    assert _run_git(caller, "diff", "--cached").stdout == before_index
    assert (
        _run_git(caller, "status", "--porcelain", "--untracked-files=all").stdout == before_status
    )
    assert (caller / "tracked.txt").read_text(encoding="utf-8") == "local work\n"
    assert (caller / "untracked.txt").read_text(encoding="utf-8") == "keep\n"


def test_isolated_intake_is_bound_to_fetched_default_head(tmp_path: Path) -> None:
    """The receipt and intake HEAD match the fetched remote default branch."""
    caller, remote = _make_repository(tmp_path)
    _advance_remote(tmp_path, remote)

    receipt = _manager(caller, remote).prepare()
    fetched_head = _run_git(remote, "rev-parse", "refs/heads/master").stdout.strip()

    assert receipt.revision == fetched_head
    assert _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip() == fetched_head


def test_stale_clean_owned_intake_is_rebound_under_common_dir_lock(tmp_path: Path) -> None:
    """A clean owned intake path is reused and rebound under its lock."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    first = manager.prepare()
    _advance_remote(tmp_path, remote)

    second = _manager(caller, remote).prepare()

    assert second.path == first.path
    assert second.revision != first.revision
    assert second.generation == first.generation + 1
    assert second.revision == _run_git(second.path, "rev-parse", "HEAD").stdout.strip()


def test_direct_scope_from_detached_linked_worktree_prepares_isolated_intake(
    tmp_path: Path,
) -> None:
    """A detached linked caller does not need to move its own worktree."""
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(caller, "worktree", "add", "--detach", str(linked), "HEAD")
    before_head = _run_git(linked, "rev-parse", "HEAD").stdout

    receipt = _manager(linked, remote).prepare()

    assert receipt.path != linked
    assert _run_git(linked, "rev-parse", "HEAD").stdout == before_head
    assert (
        _run_git(linked, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).returncode == 1
    )


def test_default_branch_already_checked_out_does_not_create_branch_conflict(
    tmp_path: Path,
) -> None:
    """A primary master worktree and detached intake can coexist."""
    caller, remote = _make_repository(tmp_path)

    receipt = _manager(caller, remote).prepare()

    assert _run_git(caller, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() == "master"
    assert (
        _run_git(receipt.path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).returncode
        == 1
    )


def test_dirty_owned_intake_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A dirty owned intake is never removed or silently rebound."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    receipt = manager.prepare()
    dirty_file = receipt.path / "operator-notes.txt"
    dirty_file.write_text("keep for recovery\n", encoding="utf-8")
    _advance_remote(tmp_path, remote)

    with pytest.raises(RepoIntakeError, match="dirty and preserved"):
        _manager(caller, remote).prepare()

    assert dirty_file.read_text(encoding="utf-8") == "keep for recovery\n"
    assert receipt.path.is_dir()


def test_foreign_intake_path_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """An unreceipted path cannot be adopted as automation-owned."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.worktree_path.mkdir(parents=True)
    marker = manager.worktree_path / "manual-recovery.txt"
    marker.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(RepoIntakeError, match=r"unsafe|unowned"):
        manager.prepare()

    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_symlinked_intake_state_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A symlink at the owned state path cannot redirect intake ownership."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    foreign = tmp_path / "foreign-intake"
    foreign.mkdir()
    marker = foreign / "manual-recovery.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    manager.state_parent.mkdir(mode=0o700, parents=True)
    manager.state_dir.symlink_to(foreign, target_is_directory=True)

    with pytest.raises(RepoIntakeError, match="state path is unsafe"):
        manager.prepare()

    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_concurrent_intake_preparation_reuses_one_owned_path(tmp_path: Path) -> None:
    """Concurrent preparations produce one path and one initial generation."""
    caller, remote = _make_repository(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda _index: _manager(caller, remote).prepare(),
                range(2),
            )
        )

    assert receipts[0].path == receipts[1].path
    assert receipts[0].revision == receipts[1].revision
    assert receipts[0].generation == receipts[1].generation == 1
