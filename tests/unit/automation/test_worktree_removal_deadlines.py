"""Preserve worktrees when removal reaches an operation limit."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_runtime, worktree_manager
from hephaestus.automation.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspaceManager,
    SourceWorkspaceTerminalError,
    _PreparationDeadline,
)
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.config.child_environments import build_git_child_env


def _git(repo: Path, *args: str) -> str:
    """Run Git in a disposable repository."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a repository with two committed source revisions."""
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "commit", "-am", "second")
    return repo, first, _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("stop", ["deadline", "cancellation"])
@pytest.mark.parametrize("phase", ["git_removal", "direct_fallback"])
def test_adoption_stop_preserves_predecessor_and_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
    phase: str,
) -> None:
    """A stopped replacement must preserve the current writer and journal."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", f"{second}:refs/heads/adopted-writer")
    source = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source.prepare(42, SourceLane.IMPLEMENTATION, first)
    manager = WorktreeManager(
        repo_root=repo,
        base_dir=source.base_dir,
        remote_git_env=build_git_child_env(),
        remote_git_config=("-c", "protocol.file.allow=always"),
    )
    receipt = source._receipt_path(42, SourceLane.IMPLEMENTATION)
    receipt_before = receipt.read_bytes()
    registration_before = _git(repo, "worktree", "list", "--porcelain")
    inode_before = predecessor.cwd.stat().st_ino
    journal_path = source._transition_path(42)
    journal_at_stop: list[bytes] = []
    errors: list[BaseException] = []
    shutdown = threading.Event()
    clock = [time.monotonic()]
    expires_at = clock[0] + 60.0
    deadline = _PreparationDeadline(expires_at, lambda: clock[0], shutdown)
    monkeypatch.setattr(
        git_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep)
    )
    real_run = git_runtime.run
    stop_error: BaseException = (
        subprocess.TimeoutExpired("git worktree remove", 0)
        if stop == "deadline"
        else InterruptedError("Git operation cancelled")
    )

    def stop_removal(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:4] == ["git", "worktree", "remove", "--force"]:
            journal_at_stop.append(journal_path.read_bytes())
            if stop == "deadline":
                clock[0] = expires_at + 1.0
            else:
                shutdown.set()
            if phase == "git_removal":
                raise stop_error
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="removal failed")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(worktree_manager, "run", stop_removal)
    with pytest.raises((SourceWorkspaceError, InterruptedError, subprocess.TimeoutExpired)):
        with source.implementation_writer_handoff(42, deadline=deadline) as handoff:
            source.authorize_adopted_implementation_writer_transition(
                42, branch="adopted-writer", expected_head=second, handoff=handoff
            )
            try:
                manager.create_worktree(
                    42,
                    "adopted-writer",
                    source_lane="impl",
                    implementation_adoption_head=second,
                    implementation_writer_handoff=handoff,
                )
            except BaseException as exc:
                errors.append(exc)
                raise

    assert len(journal_at_stop) == 1
    assert json.loads(journal_at_stop[0])["phase"] == "predecessor_removing"
    assert predecessor.cwd.exists(), "The stopped operation removed its predecessor."
    assert predecessor.cwd.stat().st_ino == inode_before
    assert (predecessor.cwd / "tracked.txt").read_text(encoding="utf-8") == "first\n"
    assert _git(predecessor.cwd, "rev-parse", "HEAD") == first
    assert receipt.read_bytes() == receipt_before
    assert journal_path.read_bytes() == journal_at_stop[0]
    assert _git(repo, "worktree", "list", "--porcelain") == registration_before
    assert len(errors) == 1
    if phase == "git_removal":
        assert errors[0] is stop_error
    else:
        assert isinstance(errors[0], type(stop_error))


@pytest.mark.parametrize("stop", ["deadline", "cancellation"])
def test_worktree_removal_preserves_prune_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """A prune operation must return its original stop cause to its caller."""
    repo, _, _ = _repository(tmp_path)
    manager = WorktreeManager(repo_root=repo)
    path = manager.base_dir / "absent-writer"
    stop_error = (
        subprocess.TimeoutExpired("git worktree prune", 0)
        if stop == "deadline"
        else InterruptedError("Git operation cancelled")
    )
    commands: list[list[str]] = []

    def stop_prune(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        if cmd == ["git", "worktree", "prune"]:
            raise stop_error
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="path is absent")

    monkeypatch.setattr(worktree_manager, "run", stop_prune)
    with pytest.raises(type(stop_error)) as raised:
        manager._remove_worktree_path_forcefully(path)
    assert raised.value is stop_error
    assert commands == [
        ["git", "worktree", "remove", "--force", str(path)],
        ["git", "worktree", "prune"],
    ]


def test_worktree_removal_fallback_stops_during_directory_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation during fallback deletion preserves the path for recovery."""
    repo = tmp_path / "repository"
    repo.mkdir()
    worktree_path = repo / "build" / ".worktrees" / "writer"
    worktree_path.mkdir(parents=True)
    (worktree_path / "tracked.txt").write_text("preserve\n", encoding="utf-8")
    manager = WorktreeManager(repo_root=repo)
    shutdown = threading.Event()
    git_commands: list[list[str]] = []
    fallback_calls: list[tuple[list[str], dict[str, Any]]] = []
    fallback_started = threading.Event()
    fallback_released = threading.Event()

    def fake_git_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        git_commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="failed")

    def stop_fallback(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        fallback_calls.append((cmd, kwargs))
        fallback_started.set()
        if not fallback_released.wait(timeout=5.0):
            raise AssertionError("fallback deletion did not receive the stop request")
        if not shutdown.is_set():
            raise AssertionError("fallback deletion was released without cancellation")
        raise InterruptedError("directory deletion cancelled")

    monkeypatch.setattr(worktree_manager, "run", fake_git_run)
    monkeypatch.setattr(worktree_manager, "run_subprocess", stop_fallback, raising=False)

    def request_shutdown() -> None:
        if not fallback_started.wait(timeout=5.0):
            raise AssertionError("fallback deletion did not start")
        shutdown.set()
        fallback_released.set()

    stop_thread = threading.Thread(target=request_shutdown)
    stop_thread.start()
    try:
        with git_runtime.operation_deadline(time.monotonic() + 60.0, shutdown=shutdown):
            with pytest.raises(InterruptedError, match="directory deletion cancelled"):
                manager._remove_worktree_path_forcefully(worktree_path)
    finally:
        fallback_released.set()
        stop_thread.join(timeout=5.0)
    assert not stop_thread.is_alive()

    assert git_commands == [
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        ["git", "worktree", "prune", "--expire", "now"],
    ]
    assert len(fallback_calls) == 1
    command, options = fallback_calls[0]
    assert command[0:3] == [sys.executable, "-I", "-c"]
    assert command[-1] != str(worktree_path)
    assert Path(command[-1]).is_dir()
    assert options["track_process_group"] is True
    assert options["shutdown"] is shutdown
    assert callable(options["remaining_timeout"])
    assert not worktree_path.exists()


def test_worktree_removal_stop_before_staging_preserves_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop before staging must preserve the exact registered predecessor."""
    repo, first, _ = _repository(tmp_path)
    worktree_path = repo / "build" / ".worktrees" / "writer"
    _git(repo, "worktree", "add", "--detach", str(worktree_path), first)
    inode_before = worktree_path.stat().st_ino
    contents_before = (worktree_path / "tracked.txt").read_bytes()
    registration_before = _git(repo, "worktree", "list", "--porcelain")
    manager = WorktreeManager(repo_root=repo)
    shutdown = threading.Event()
    real_run = git_runtime.run

    def failed_git_remove(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:4] == ["git", "worktree", "remove", "--force"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="removal failed")
        return real_run(cmd, **kwargs)

    def stop_after_initial_check(_size: int) -> str:
        shutdown.set()
        return "race"

    monkeypatch.setattr(worktree_manager, "run", failed_git_remove)
    monkeypatch.setattr(secrets, "token_hex", stop_after_initial_check)

    with git_runtime.operation_deadline(time.monotonic() + 60.0, shutdown=shutdown):
        with pytest.raises(InterruptedError):
            manager._remove_worktree_path_forcefully(worktree_path)

    assert worktree_path.exists()
    assert worktree_path.stat().st_ino == inode_before
    assert (worktree_path / "tracked.txt").read_bytes() == contents_before
    assert _git(repo, "worktree", "list", "--porcelain") == registration_before


def test_partial_staged_deletion_failure_stops_then_restores_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed staged deletion must defer exact predecessor recovery."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", f"{second}:refs/heads/adopted-writer")
    source = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source.prepare(42, SourceLane.IMPLEMENTATION, first)
    manager = WorktreeManager(
        repo_root=repo,
        base_dir=source.base_dir,
        remote_git_env=build_git_child_env(),
        remote_git_config=("-c", "protocol.file.allow=always"),
    )
    registration_before = _git(repo, "worktree", "list", "--porcelain")
    journal_path = source._transition_path(42)
    commands: list[list[str]] = []
    fallback_targets: list[Path] = []
    deletion_failures: list[subprocess.CalledProcessError] = []
    real_run = git_runtime.run

    def fake_git_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        if cmd[:4] == ["git", "worktree", "remove", "--force"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="removal failed")
        return real_run(cmd, **kwargs)

    def partially_delete_then_fail(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        target = Path(cmd[-1])
        fallback_targets.append(target)
        (target / "tracked.txt").unlink()
        failure = subprocess.CalledProcessError(1, cmd, stderr="directory deletion failed")
        deletion_failures.append(failure)
        raise failure

    monkeypatch.setattr(worktree_manager, "run", fake_git_run)
    monkeypatch.setattr(
        worktree_manager, "run_subprocess", partially_delete_then_fail, raising=False
    )

    with pytest.raises(SourceWorkspaceTerminalError) as raised:
        with source.implementation_writer_handoff(42) as handoff:
            source.authorize_adopted_implementation_writer_transition(
                42, branch="adopted-writer", expected_head=second, handoff=handoff
            )
            manager.create_worktree(
                42,
                "adopted-writer",
                source_lane="impl",
                implementation_adoption_head=second,
                implementation_writer_handoff=handoff,
            )

    assert len(fallback_targets) == 1
    assert len(deletion_failures) == 1
    creation_failure = raised.value.__cause__
    assert type(creation_failure) is RuntimeError
    assert creation_failure.__cause__ is deletion_failures[0]
    assert deletion_failures[0].cmd[-1] == str(fallback_targets[0])
    assert deletion_failures[0].stderr == "directory deletion failed"
    assert raised.value.terminal_reference is not None
    assert fallback_targets[0] != predecessor.cwd
    assert fallback_targets[0].is_dir()
    assert not (fallback_targets[0] / "tracked.txt").exists()
    assert not predecessor.cwd.exists()
    assert str(predecessor.cwd) not in _git(repo, "worktree", "list", "--porcelain")
    assert json.loads(journal_path.read_bytes())["phase"] == "predecessor_removing"
    assert ["git", "worktree", "prune", "--expire", "now"] in commands
    assert not any(
        "worktree" in command and "add" in command and "adopted-writer" in command
        for command in commands
    )

    with source.implementation_writer_handoff(42):
        pass

    assert predecessor.cwd.is_dir()
    assert (predecessor.cwd / "tracked.txt").read_text(encoding="utf-8") == "first\n"
    assert _git(predecessor.cwd, "rev-parse", "HEAD") == first
    assert _git(repo, "worktree", "list", "--porcelain") == registration_before
    assert not journal_path.exists()


def test_worktree_removal_partial_fallback_keeps_predecessor_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped fallback must leave the predecessor available for recovery."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", f"{second}:refs/heads/adopted-writer")
    source = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source.prepare(42, SourceLane.IMPLEMENTATION, first)
    manager = WorktreeManager(
        repo_root=repo,
        base_dir=source.base_dir,
        remote_git_env=build_git_child_env(),
        remote_git_config=("-c", "protocol.file.allow=always"),
    )
    shutdown = threading.Event()
    fallback_started = threading.Event()
    fallback_released = threading.Event()
    fallback_targets: list[Path] = []
    real_run = git_runtime.run

    def fake_git_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:4] == ["git", "worktree", "remove", "--force"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="removal failed")
        return real_run(cmd, **kwargs)

    def partially_delete_then_stop(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        target = Path(cmd[-1])
        fallback_targets.append(target)
        fallback_started.set()
        if not fallback_released.wait(timeout=5.0):
            raise AssertionError("fallback deletion did not receive the stop request")
        (target / "tracked.txt").unlink()
        raise InterruptedError("directory deletion cancelled after partial deletion")

    monkeypatch.setattr(worktree_manager, "run", fake_git_run)
    monkeypatch.setattr(
        worktree_manager, "run_subprocess", partially_delete_then_stop, raising=False
    )

    def request_shutdown() -> None:
        if not fallback_started.wait(timeout=5.0):
            raise AssertionError("fallback deletion did not start")
        shutdown.set()
        fallback_released.set()

    stop_thread = threading.Thread(target=request_shutdown)
    stop_thread.start()
    try:
        with pytest.raises((SourceWorkspaceError, InterruptedError, subprocess.TimeoutExpired)):
            with source.implementation_writer_handoff(
                42, deadline=_PreparationDeadline(time.monotonic() + 60.0, time.monotonic, shutdown)
            ) as handoff:
                source.authorize_adopted_implementation_writer_transition(
                    42, branch="adopted-writer", expected_head=second, handoff=handoff
                )
                manager.create_worktree(
                    42,
                    "adopted-writer",
                    source_lane="impl",
                    implementation_adoption_head=second,
                    implementation_writer_handoff=handoff,
                )
    finally:
        fallback_released.set()
        stop_thread.join(timeout=5.0)
    assert not stop_thread.is_alive()
    assert len(fallback_targets) == 1
    assert fallback_targets[0] != predecessor.cwd
    assert fallback_targets[0].is_dir()
    assert not (fallback_targets[0] / "tracked.txt").exists()
    assert not predecessor.cwd.exists()

    with source.implementation_writer_handoff(42):
        pass

    assert predecessor.cwd.exists()
    assert (predecessor.cwd / "tracked.txt").read_text(encoding="utf-8") == "first\n"
    assert _git(predecessor.cwd, "rev-parse", "HEAD") == first
    assert not source._transition_path(42).exists()


def test_worktree_removal_fallback_ignores_repository_shutil_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback child must import only the standard-library shutil module."""
    repo = tmp_path / "repository"
    repo.mkdir()
    marker = repo / "shutil-imported.txt"
    (repo / "shutil.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )
    worktree_path = repo / "build" / ".worktrees" / "writer"
    worktree_path.mkdir(parents=True)
    (worktree_path / "tracked.txt").write_text("remove\n", encoding="utf-8")
    manager = WorktreeManager(repo_root=repo)

    def failed_git_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not a worktree")

    monkeypatch.setattr(worktree_manager, "run", failed_git_run)
    manager._remove_worktree_path_forcefully(worktree_path, timeout=60)

    assert not marker.exists()
    assert not worktree_path.exists()
