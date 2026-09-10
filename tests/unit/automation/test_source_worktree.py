"""Lifecycle tests for deterministic source-reading worktrees."""

from __future__ import annotations

import errno
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation import implementation_writer, source_worktree, worktree_manager
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.git_cleanup import run_cleanup_job
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import (
    ImplementationStage,
    StageContext,
    StageOutcome,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspaceManager,
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
    SourceWorkspaceRecoveryKind,
    _git as _source_worktree_git,
    _PreparationDeadline,
)
from hephaestus.automation.worktree_manager import (
    ImplementationWriterAuthority,
    WorktreeCreationReceiptError,
    WorktreeManager,
)
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.utils.file_lock import LockUnavailableError, file_lock


def _proc_state_is_terminal(status_path: Path) -> bool:
    """Return whether the observed process has terminated or disappeared."""
    try:
        state = status_path.read_text().split()[2]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state in {"X", "Z"}


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(errno.ENOENT, "process absent"),
        ProcessLookupError(errno.ESRCH, "process gone"),
    ],
    ids=["enoent", "esrch"],
)
def test_proc_state_accepts_disappeared_process(error: OSError) -> None:
    """A process that disappears during the read is no longer active."""
    with patch.object(Path, "read_text", side_effect=error):
        assert _proc_state_is_terminal(Path("/proc/123/stat"))


@pytest.mark.parametrize("state", ["X", "Z", "R", "S", "D", "T"])
def test_proc_state_distinguishes_terminal_from_live_states(state: str) -> None:
    """Live and stopped processes must not satisfy the terminal check."""
    with patch.object(Path, "read_text", return_value=f"123 (child) {state}"):
        assert _proc_state_is_terminal(Path("/proc/123/stat")) is (state in {"X", "Z"})


@pytest.mark.parametrize(
    "error",
    [PermissionError(errno.EACCES, "read denied"), OSError(errno.EIO, "read failed")],
    ids=["permission", "io"],
)
def test_proc_state_propagates_unrelated_read_errors(error: OSError) -> None:
    """An unrelated read failure cannot establish process termination."""
    with patch.object(Path, "read_text", side_effect=error):
        with pytest.raises(type(error)) as raised:
            _proc_state_is_terminal(Path("/proc/123/stat"))
    assert raised.value is error


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-am", "second")
    return repo, first, _git(repo, "rev-parse", "HEAD")


def test_many_preparations_reuse_exactly_two_named_worktrees(tmp_path: Path) -> None:
    """Retries and rounds reuse the two deterministic lane paths."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")

    for _ in range(5):
        impl = manager.prepare(42, SourceLane.IMPLEMENTATION, second)
        review = manager.prepare(42, SourceLane.REVIEW, first)

    assert impl.cwd.name == "auto-42-impl"
    assert review.cwd.name == "auto-42-review"
    paths = {
        Path(line.removeprefix("worktree ")).name
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }
    assert paths == {"repository", "auto-42-impl", "auto-42-review"}
    assert _git(repo, "branch", "--format=%(refname:short)").splitlines() == ["main"]


def test_bounded_prepare_rejects_held_lane_lock_without_receipt_mutation(
    tmp_path: Path,
) -> None:
    """Bounded preparation reports lane contention before it can mutate state."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    lane = SourceLane.IMPLEMENTATION
    receipt_path = manager._receipt_path(42, lane)
    path = manager.path_for(42, lane)

    with file_lock(manager._lane_lock_path(42, lane), require_exclusive=True):
        bounded_prepare = getattr(manager, "prepare_bounded", None)
        assert callable(bounded_prepare)
        with pytest.raises(SourceWorkspacePreparationError) as raised:
            bounded_prepare(42, lane, second)

    assert raised.value.cause is SourceWorkspacePreparationCause.LANE_LOCK_UNAVAILABLE
    assert not receipt_path.exists()
    assert not path.exists()


def test_bounded_prepare_rejects_held_git_metadata_lock_without_receipt_mutation(
    tmp_path: Path,
) -> None:
    """Metadata contention releases the lane and preserves durable state."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    lane = SourceLane.REVIEW
    receipt_path = manager._receipt_path(42, lane)
    metadata_lock = WorktreeManager.git_metadata_lock_path(repo)

    with file_lock(metadata_lock, require_exclusive=True):
        with pytest.raises(SourceWorkspacePreparationError) as raised:
            manager.prepare_bounded(42, lane, second)

        assert raised.value.cause is SourceWorkspacePreparationCause.GIT_METADATA_LOCK_UNAVAILABLE
        with file_lock(
            manager._lane_lock_path(42, lane),
            blocking=False,
            require_exclusive=True,
        ):
            pass

    assert not receipt_path.exists()
    assert not manager.path_for(42, lane).exists()


def test_bounded_prepare_times_out_stalled_git_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled Git command uses the remaining deadline and writes no receipt."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    calls: list[float] = []
    ticks = iter((0.0, 2.0))
    deadline = _PreparationDeadline(10.0, lambda: next(ticks))
    real_run = subprocess.run

    def delayed_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        timeout: float,
        env: dict[str, str],
        log_on_error: bool,
        track_process_group: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert not log_on_error
        assert track_process_group
        calls.append(timeout)
        if command[1:3] == ["worktree", "add"]:
            raise subprocess.TimeoutExpired(command, timeout)
        return real_run(
            command,
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
            env=env,
        )

    monkeypatch.setattr(source_worktree, "run_subprocess", delayed_run)

    with pytest.raises(SourceWorkspacePreparationError) as raised:
        manager.prepare_bounded(
            42,
            SourceLane.REVIEW,
            second,
            deadline=deadline,
        )

    assert raised.value.cause is SourceWorkspacePreparationCause.GIT_TIMEOUT
    assert calls == [10.0, 8.0]
    assert not manager._receipt_path(42, SourceLane.REVIEW).exists()
    assert not manager.path_for(42, SourceLane.REVIEW).exists()


def test_bounded_prepare_includes_final_workspace_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final validation must not renew an expired preparation deadline."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    clock = [0.0]
    deadline = _PreparationDeadline(10.0, lambda: clock[0])
    head_branch = manager._head_branch

    def expire_after_branch_check(*args: Any, **kwargs: Any) -> str | None:
        branch = head_branch(*args, **kwargs)
        clock[0] = 11.0
        return branch

    monkeypatch.setattr(manager, "_head_branch", expire_after_branch_check)
    with pytest.raises(SourceWorkspacePreparationError) as raised:
        manager.prepare_bounded(42, SourceLane.REVIEW, second, deadline=deadline)

    assert raised.value.cause is SourceWorkspacePreparationCause.GIT_TIMEOUT
    assert not manager._receipt_path(42, SourceLane.REVIEW).exists()


@pytest.mark.parametrize("change", ["generation", "lane", "item", "path", "unrelated_file"])
def test_dirty_operation_rejects_source_drift(tmp_path: Path, change: str) -> None:
    """A dirty operation must keep the exact source receipt and file scope."""
    from hephaestus.agents.workspace import DirtySourceOperation

    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, second, branch="writer")
    (binding.cwd / "tracked.txt").write_text("candidate\n")
    if change == "generation":
        binding = replace(binding, generation=binding.generation + 1)
    elif change == "lane":
        binding = replace(binding, lane=SourceLane.REVIEW)
    elif change == "item":
        binding = replace(binding, item_number=43)
    elif change == "path":
        binding = replace(binding, cwd=repo)
    else:
        (binding.cwd / "unrelated.txt").write_text("outside the approved scope\n")

    with pytest.raises(SourceWorkspaceError):
        with manager.acquire(
            binding,
            source_operation=DirtySourceOperation("test-fix", ("tracked.txt",)),
        ):
            pytest.fail("source drift received a lease")


def test_dirty_operation_permit_ends_with_its_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful dirty lease must revoke its temporary validation permit."""
    from hephaestus.agents.workspace import (
        DirtySourceOperation,
        WorkspaceBindingError,
        validate_workspace_binding,
    )

    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, second, branch="writer")
    (binding.cwd / "tracked.txt").write_text("candidate\n")
    validate = validate_workspace_binding
    permits: list[object] = []

    def capture_permit(*args: Any, **kwargs: Any) -> Path:
        if kwargs.get("dirty_permit") is not None:
            permits.append(kwargs["dirty_permit"])
        return validate(*args, **kwargs)

    monkeypatch.setattr(source_worktree, "validate_workspace_binding", capture_permit)
    with manager.acquire(
        binding, source_operation=DirtySourceOperation("test-fix", ("tracked.txt",))
    ) as leased:
        assert leased == binding.cwd
    assert permits
    with pytest.raises(WorkspaceBindingError):
        validate(binding, dirty_permit=permits[-1])


@pytest.mark.parametrize("changed_pin", ["head", "index", "content"])
def test_rebase_operation_rejects_changed_pause_pins(tmp_path: Path, changed_pin: str) -> None:
    """The edit lease requires the exact host-captured paused rebase."""
    from hephaestus.agents.workspace import DirtySourceOperation
    from hephaestus.automation.worktree_snapshot import _dirty_worktree_content_snapshot

    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, first, branch="writer")
    (binding.cwd / "tracked.txt").write_text("writer\n")
    _git(binding.cwd, "commit", "-am", "writer")
    binding = manager.prepare(
        42, SourceLane.IMPLEMENTATION, _git(binding.cwd, "rev-parse", "HEAD"), branch="writer"
    )
    paused = subprocess.run(
        ["git", "rebase", second], cwd=binding.cwd, capture_output=True, text=True, check=False
    )
    assert paused.returncode != 0
    snapshot = _dirty_worktree_content_snapshot(binding.cwd, timeout=30)
    if changed_pin == "index":
        snapshot["index_sha256"] = "0" * 64
    operation = DirtySourceOperation(
        "rebase-conflict",
        ("tracked.txt",),
        content_snapshot=tuple(sorted(snapshot.items())),
        paused_head_sha=second if changed_pin != "head" else first,
    )
    if changed_pin == "content":
        (binding.cwd / "tracked.txt").write_text("changed after the host snapshot\n")
    with pytest.raises(SourceWorkspaceError):
        with manager.acquire(binding, source_operation=operation):
            pytest.fail("changed rebase pins received a lease")


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc").is_dir(),
    reason="requires POSIX process groups and process state",
)
def test_bounded_git_timeout_stops_child_and_reaps_group_leader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded Git timeout stops a child before it reports the timeout."""
    child_pid_path = tmp_path / "child.pid"
    child_ready_path = tmp_path / "child.ready"
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[str]] = []
    parent_script = '''
import pathlib
import subprocess
import sys
import time

child_script = """
import pathlib
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text("ready", encoding="utf-8")
time.sleep(30)
"""
child = subprocess.Popen(
    [sys.executable, "-c", child_script, sys.argv[2]],
)
ready = pathlib.Path(sys.argv[2])
while not ready.exists():
    time.sleep(0.01)
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
time.sleep(30)
'''

    def spawn_process(**kwargs: Any) -> subprocess.Popen[str]:
        process = real_popen(
            [
                sys.executable,
                "-c",
                parent_script,
                str(child_pid_path),
                str(child_ready_path),
            ],
            cwd=kwargs.get("cwd"),
            env=kwargs.get("env"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(process)
        limit = time.monotonic() + 2.0
        while not child_pid_path.exists() and time.monotonic() < limit:
            time.sleep(0.01)
        assert child_pid_path.exists(), "test child did not start"
        return process

    def substitute_popen(*_args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        assert kwargs.get("start_new_session") is True
        return spawn_process(**kwargs)

    monkeypatch.setattr(subprocess, "Popen", substitute_popen)
    try:
        with pytest.raises(SourceWorkspacePreparationError) as raised:
            _source_worktree_git(
                tmp_path,
                "worktree",
                "add",
                deadline=_PreparationDeadline(time.monotonic() + 0.1, time.monotonic),
            )

        assert raised.value.cause is SourceWorkspacePreparationCause.GIT_TIMEOUT
        assert processes[0].returncode is not None
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        limit = time.monotonic() + 1.0
        while time.monotonic() < limit:
            status_path = Path("/proc") / str(child_pid) / "stat"
            if _proc_state_is_terminal(status_path):
                break
            time.sleep(0.01)
        else:
            pytest.fail("timed-out Git child process is still running")
    finally:
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.returncode is None:
                process.wait(timeout=1.0)


def test_bounded_prepare_stops_git_process_group_before_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git timeout stops resistant children before preparation unlocks."""
    if not hasattr(os, "killpg") or not hasattr(os, "getpgid"):
        pytest.skip("process-group signals are not available")

    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    real_git = shutil.which("git")
    assert real_git is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    child_pid = tmp_path / "git-child.pid"
    heartbeat = tmp_path / "git-child.heartbeat"
    fake_git = fake_bin / "git"
    child_code = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(child_pid)!r}, 'w').write(str(os.getpid()) + ' ' + str(os.getpgrp())); "
        f"stream = open({str(heartbeat)!r}, 'ab', buffering=0); "
        "[(stream.write(b'x'), time.sleep(0.02)) for _ in iter(int, 1)]"
    )
    fake_git.write_text(
        f"""#!{sys.executable}
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1:3] == ["worktree", "add"]:
    child = subprocess.Popen(
        [sys.executable, "-c", {child_code!r}],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    while not Path({str(child_pid)!r}).exists() or not Path({str(heartbeat)!r}).exists():
        time.sleep(0.01)
    time.sleep(30)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    parent_path = os.environ.get("PATH", os.defpath)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{parent_path}")
    monotonic = time.monotonic
    deadline = _PreparationDeadline(monotonic() + 0.5, monotonic)

    try:
        with pytest.raises(SourceWorkspacePreparationError) as raised:
            manager.prepare_bounded(
                42,
                SourceLane.REVIEW,
                second,
                deadline=deadline,
            )

        assert raised.value.cause is SourceWorkspacePreparationCause.GIT_TIMEOUT
        with file_lock(
            manager._lane_lock_path(42, SourceLane.REVIEW),
            blocking=False,
            require_exclusive=True,
        ):
            pass
        assert child_pid.exists()
        assert heartbeat.exists()
        pid, _pgid = (int(value) for value in child_pid.read_text(encoding="utf-8").split())
        heartbeat_size = heartbeat.stat().st_size
        time.sleep(0.15)
        assert heartbeat.stat().st_size == heartbeat_size
        child_deadline = monotonic() + 1.0
        while monotonic() < child_deadline:
            status_path = Path("/proc") / str(pid) / "stat"
            if status_path.exists():
                if _proc_state_is_terminal(status_path):
                    break
            else:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("the timed-out Git child process is still running")
    finally:
        if child_pid.exists():
            pid = int(child_pid.read_text(encoding="utf-8").split()[0])
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_bounded_prepare_releases_locks_when_a_pipe_holding_child_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git timeout stays bounded when a child escapes its process group."""
    if not hasattr(os, "killpg"):
        pytest.skip("process-group signals are not available")

    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    real_git = shutil.which("git")
    assert real_git is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    child_pid = tmp_path / "escaped-child.pid"
    heartbeat = tmp_path / "escaped-child.heartbeat"
    fake_git = fake_bin / "git"
    child_code = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(child_pid)!r}, 'w').write(str(os.getpid())); "
        f"stream = open({str(heartbeat)!r}, 'ab', buffering=0); "
        "[(stream.write(b'x'), os.write(1, b'x'), time.sleep(0.02)) "
        "for _ in iter(int, 1)]"
    )
    fake_git.write_text(
        f"""#!{sys.executable}
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1:3] == ["worktree", "add"]:
    subprocess.Popen(
        [sys.executable, "-c", {child_code!r}],
        start_new_session=True,
    )
    while not Path({str(child_pid)!r}).exists() or not Path({str(heartbeat)!r}).exists():
        time.sleep(0.01)
    time.sleep(4)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    parent_path = os.environ.get("PATH", os.defpath)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{parent_path}")
    monotonic = time.monotonic
    started = monotonic()

    try:
        with pytest.raises(SourceWorkspacePreparationError) as raised:
            manager.prepare_bounded(
                42,
                SourceLane.REVIEW,
                second,
                deadline=_PreparationDeadline(started + 0.5, monotonic),
            )

        assert raised.value.cause is SourceWorkspacePreparationCause.GIT_TIMEOUT
        assert monotonic() - started < 2.5
        with file_lock(
            manager._lane_lock_path(42, SourceLane.REVIEW),
            blocking=False,
            require_exclusive=True,
        ):
            pass
        assert child_pid.exists()
        assert heartbeat.exists()
        pid = int(child_pid.read_text(encoding="utf-8"))
        child_deadline = monotonic() + 1.0
        while monotonic() < child_deadline:
            status_path = Path("/proc") / str(pid) / "stat"
            if status_path.exists():
                if _proc_state_is_terminal(status_path):
                    break
            else:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("the escaped Git child process is still running")
        heartbeat_size = heartbeat.stat().st_size
        time.sleep(0.15)
        assert heartbeat.stat().st_size == heartbeat_size
    finally:
        if child_pid.exists():
            pid = int(child_pid.read_text(encoding="utf-8"))
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_bounded_prepare_restarts_after_replacement_timeout(
    tmp_path: Path,
) -> None:
    """A retry reconciles a clean partial replacement before writing its receipt."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    real_replace = manager._replace_worktree

    def replace_then_timeout(*args: Any, **kwargs: Any) -> None:
        real_replace(*args, **kwargs)
        raise SourceWorkspacePreparationError(SourceWorkspacePreparationCause.GIT_TIMEOUT)

    with patch.object(manager, "_replace_worktree", side_effect=replace_then_timeout):
        with pytest.raises(SourceWorkspacePreparationError) as raised:
            manager.prepare_bounded(42, SourceLane.REVIEW, second)

    assert raised.value.cause is SourceWorkspacePreparationCause.GIT_TIMEOUT
    assert manager.path_for(42, SourceLane.REVIEW).exists()
    assert not manager._receipt_path(42, SourceLane.REVIEW).exists()

    binding = manager.prepare_bounded(42, SourceLane.REVIEW, second)

    assert binding.revision == second
    assert manager._read_receipt(42, SourceLane.REVIEW) is not None


def test_source_manager_uses_shared_git_common_dir_from_linked_checkout(
    tmp_path: Path,
) -> None:
    """A linked checkout shares identity, locks, and receipt storage with its base."""
    repo, _, second = _repository(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "linked", str(linked), second)
    base_manager = SourceWorkspaceManager(repo, repository="example/project")
    linked_manager = SourceWorkspaceManager(linked, repository="example/project")

    assert linked_manager.common_dir == base_manager.common_dir
    assert linked_manager.repository_identity == base_manager.repository_identity
    assert linked_manager._lane_lock_path(42, SourceLane.REVIEW) == (
        base_manager._lane_lock_path(42, SourceLane.REVIEW)
    )
    assert linked_manager._receipt_path(42, SourceLane.REVIEW) == (
        base_manager._receipt_path(42, SourceLane.REVIEW)
    )


def test_review_rebinds_same_path_to_exact_revision(tmp_path: Path) -> None:
    """A changed review head rebinds in place without a review branch."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(9, SourceLane.REVIEW, first)

    rebound = manager.prepare(9, SourceLane.REVIEW, second)

    assert rebound.cwd == original.cwd
    assert rebound.generation == original.generation + 1
    assert _git(rebound.cwd, "rev-parse", "HEAD") == second
    assert _git(rebound.cwd, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_review_rebinds_when_receipt_revision_does_not_match_physical_head(
    tmp_path: Path,
) -> None:
    """A clean detached lane is reusable only after proving its physical HEAD."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(9, SourceLane.REVIEW, second)
    _git(original.cwd, "reset", "--hard", first)

    rebound = manager.prepare(9, SourceLane.REVIEW, second)

    assert rebound.generation == original.generation + 1
    assert _git(rebound.cwd, "rev-parse", "HEAD") == second


def test_review_rebinds_when_physical_checkout_is_attached(tmp_path: Path) -> None:
    """A review receipt cannot hide an attached physical checkout."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(9, SourceLane.REVIEW, second)
    _git(original.cwd, "switch", "-c", "wrong-review-branch")

    rebound = manager.prepare(9, SourceLane.REVIEW, second)

    assert rebound.generation == original.generation + 1
    assert _git(rebound.cwd, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_implementation_rebinds_when_physical_branch_is_wrong(tmp_path: Path) -> None:
    """An attached lane is reusable only on its expected physical branch."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="expected-implementation-branch",
    )
    _git(original.cwd, "switch", "-c", "wrong-implementation-branch")

    rebound = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="expected-implementation-branch",
    )

    assert rebound.generation == original.generation + 1
    assert _git(rebound.cwd, "symbolic-ref", "HEAD") == "refs/heads/expected-implementation-branch"


def test_implementation_rebinds_clean_stale_branch_to_exact_revision(
    tmp_path: Path,
) -> None:
    """A clean implementation lane moves its branch to the requested revision."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="expected-implementation-branch",
    )

    rebound = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="expected-implementation-branch",
    )

    physical_revision = _git(rebound.cwd, "rev-parse", "HEAD")
    assert rebound.cwd == original.cwd
    assert rebound.generation == original.generation + 1
    assert physical_revision == second
    assert rebound.revision == physical_revision
    assert _git(rebound.cwd, "symbolic-ref", "HEAD") == "refs/heads/expected-implementation-branch"


def test_implementation_preserves_branch_held_by_another_worktree(
    tmp_path: Path,
) -> None:
    """A branch that another worktree holds causes a safe typed failure."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="expected-implementation-branch",
    )
    _git(repo, "worktree", "remove", str(original.cwd))
    holder = tmp_path / "branch-holder"
    _git(repo, "worktree", "add", str(holder), "expected-implementation-branch")

    with pytest.raises(
        SourceWorkspaceError,
        match="source workspace branch could not be synchronized safely",
    ):
        manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="expected-implementation-branch",
        )

    assert _git(holder, "rev-parse", "HEAD") == first
    assert not original.cwd.exists()


def test_implementation_preserves_inactive_unowned_branch(tmp_path: Path) -> None:
    """A branch without a lane receipt cannot be reset during preparation."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    _git(repo, "branch", "unowned-implementation-branch", first)

    with pytest.raises(
        SourceWorkspaceError,
        match="source workspace branch is not owned by this lane",
    ):
        manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="unowned-implementation-branch",
        )

    assert _git(repo, "rev-parse", "unowned-implementation-branch") == first
    assert not manager.path_for(9, SourceLane.IMPLEMENTATION).exists()


def test_claim_implementation_writer_records_the_controlled_checkout(
    tmp_path: Path,
) -> None:
    """A deterministic, clean writer can become its source lane."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        authority = worktree_manager.implementation_writer_authority(writer)

        binding = manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=authority,
            handoff=handoff,
        )

    rebound = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="writer-branch",
    )
    assert binding.cwd == writer
    assert binding.revision == second
    assert rebound == binding


def test_direct_writer_replaces_owned_detached_source_before_rebinding(
    tmp_path: Path,
) -> None:
    """A direct writer can replace its exact owned detached predecessor."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with source_manager.implementation_writer_handoff(9) as handoff:
        source_manager.authorize_direct_implementation_writer_transition(
            9,
            branch="writer-branch",
            base_sha=second,
            handoff=handoff,
        )
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            base_sha=second,
            remote_branch_reserved=True,
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        binding = source_manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )

    rebound = source_manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="writer-branch",
    )
    assert writer == predecessor.cwd
    assert binding.revision == second
    assert rebound == binding


def test_direct_writer_replaces_owned_attached_source_before_rebinding(
    tmp_path: Path,
) -> None:
    """A direct writer can replace an exact owned attached predecessor."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with source_manager.implementation_writer_handoff(9) as handoff:
        source_manager.authorize_direct_implementation_writer_transition(
            9,
            branch="writer-branch",
            base_sha=second,
            handoff=handoff,
        )
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            base_sha=second,
            remote_branch_reserved=True,
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        binding = source_manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )

    assert writer == predecessor.cwd
    assert _git(repo, "rev-parse", "old-writer-branch") == first
    assert binding.revision == second
    assert (
        source_manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="writer-branch",
        )
        == binding
    )


def test_direct_worker_preserves_existing_writer_without_receipt(tmp_path: Path) -> None:
    """A direct worker rejects an existing writer that has no receipt."""
    repo, first, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = source_manager.path_for(9, SourceLane.IMPLEMENTATION)
    writer.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(writer), first)
    assert not source_manager._receipt_path(9, SourceLane.IMPLEMENTATION).exists()

    completion_q: CompletionQueue = queue.Queue()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    job = GitJob(
        repo="example/project",
        op="create_worktree",
        timeout_s=60,
        kwargs={
            "issue_number": 9,
            "branch_name": "writer-branch",
            "repo_root": str(repo),
            "source_lane": SourceLane.IMPLEMENTATION.value,
            "base_sha": second,
        },
    )

    with (
        patch.object(
            pool,
            "_prepare_direct_scope_worktree",
            return_value=(second, "writer-branch"),
        ),
        patch.object(
            pool,
            "_authenticated_remote_git_configuration",
            return_value=({}, ()),
        ),
        patch.object(WorktreeManager, "create_worktree", autospec=True) as create_worktree,
    ):
        pool.submit(job, StageName.REPO)
        _, result = completion_q.get(timeout=10)

    create_worktree.assert_not_called()
    assert result.ok is False
    assert result.error == "source_workspace_terminal"
    assert isinstance(result.value, dict)
    assert result.value["failure_kind"] == "source_workspace_terminal"
    assert result.value["source_workspace_preserve"] is True
    assert result.value["source_workspace_terminal"] is None
    assert result.value["path"] == str(writer)
    assert result.value["direct_scope_reservation"] == {
        "branch": "writer-branch",
        "base_sha": second,
    }
    assert writer.exists()
    assert _git(writer, "rev-parse", "HEAD") == first
    assert _git(writer, "branch", "--show-current") == ""


def test_direct_writer_transition_rejects_receipt_free_stale_registration(
    tmp_path: Path,
) -> None:
    """A receipt-free stale Git registration stays available for recovery."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(
        repo,
        repository="example/project",
        base_dir=tmp_path / "source\nworktrees",
    )
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    receipt_path.unlink()
    shutil.rmtree(predecessor.cwd)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(
            SourceWorkspaceError,
            match="implementation writer predecessor is unproven",
        ) as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    recovery = captured.value.recovery
    assert recovery is not None
    assert recovery.kind is SourceWorkspaceRecoveryKind.UNPROVEN_PREDECESSOR
    assert recovery.path == predecessor.cwd.resolve()
    assert recovery.receipt_path == receipt_path.absolute()
    registered_paths = {
        field.removeprefix("worktree ")
        for field in _git(repo, "worktree", "list", "--porcelain", "-z").split("\0")
        if field.startswith("worktree ")
    }
    assert str(predecessor.cwd.resolve()) in registered_paths


@pytest.mark.parametrize("receipt_kind", ["malformed", "unreadable", "symlink"])
def test_direct_writer_receipt_read_failure_has_unproven_recovery(
    tmp_path: Path, receipt_kind: str
) -> None:
    """A receipt that cannot be read preserves the writer with recovery guidance."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    if receipt_kind == "malformed":
        receipt_path.write_text("not-json\n", encoding="utf-8")
    elif receipt_kind == "unreadable":
        receipt_path.write_bytes(b"\xff\n")
    else:
        replacement = tmp_path / "replacement-receipt.json"
        replacement.write_text("{}\n", encoding="utf-8")
        receipt_path.unlink()
        receipt_path.symlink_to(replacement)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError) as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    recovery = captured.value.recovery
    assert recovery is not None
    assert recovery.kind is SourceWorkspaceRecoveryKind.UNPROVEN_PREDECESSOR
    assert recovery.item_number == 9
    assert recovery.path == predecessor.cwd.resolve()
    assert recovery.receipt_path == receipt_path.absolute()
    assert "Inspect and preserve" in recovery.manual_action
    assert predecessor.cwd.exists()
    assert _git(predecessor.cwd, "symbolic-ref", "HEAD") == "refs/heads/old-writer-branch"


@pytest.mark.parametrize(
    "probe_error",
    [
        SourceWorkspaceError("probe failed"),
        OSError("probe failed"),
        subprocess.SubprocessError("probe failed"),
    ],
    ids=["workspace-error", "os-error", "subprocess-error"],
)
def test_direct_writer_probe_failure_has_unproven_recovery(
    tmp_path: Path, probe_error: Exception
) -> None:
    """A failed physical predecessor probe preserves the writer with recovery guidance."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)

    with manager.implementation_writer_handoff(9) as handoff:
        with (
            patch.object(
                manager,
                "_is_dirty",
                side_effect=probe_error,
            ),
            pytest.raises(SourceWorkspaceError) as captured,
        ):
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    recovery = captured.value.recovery
    assert recovery is not None
    assert recovery.kind is SourceWorkspaceRecoveryKind.UNPROVEN_PREDECESSOR
    assert recovery.item_number == 9
    assert recovery.path == predecessor.cwd.resolve()
    assert recovery.receipt_path == receipt_path.resolve()
    assert "Inspect and preserve" in recovery.manual_action
    assert predecessor.cwd.exists()


@pytest.mark.parametrize("receipt_kind", ["malformed", "unreadable", "symlink"])
def test_direct_worker_serializes_unproven_recovery_for_unreadable_receipt(
    tmp_path: Path, receipt_kind: str
) -> None:
    """A direct worker returns durable recovery when its predecessor receipt cannot be read."""
    repo, first, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = source_manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    if receipt_kind == "malformed":
        receipt_path.write_text("not-json\n", encoding="utf-8")
    elif receipt_kind == "unreadable":
        receipt_path.write_bytes(b"\xff\n")
    else:
        replacement = tmp_path / "replacement-receipt.json"
        replacement.write_text("{}\n", encoding="utf-8")
        receipt_path.unlink()
        receipt_path.symlink_to(replacement)

    completion_q: CompletionQueue = queue.Queue()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    job = GitJob(
        repo="example/project",
        op="create_worktree",
        timeout_s=60,
        kwargs={
            "issue_number": 9,
            "branch_name": "writer-branch",
            "repo_root": str(repo),
            "source_lane": SourceLane.IMPLEMENTATION.value,
            "base_sha": second,
        },
    )

    with (
        patch.object(
            pool,
            "_prepare_direct_scope_worktree",
            return_value=(second, "writer-branch"),
        ),
        patch.object(
            pool,
            "_authenticated_remote_git_configuration",
            return_value=({}, ()),
        ),
        patch.object(WorktreeManager, "create_worktree", autospec=True) as create_worktree,
    ):
        pool.submit(job, StageName.REPO)
        _, result = completion_q.get(timeout=10)

    create_worktree.assert_not_called()
    assert result.ok is False
    assert result.error == "source_workspace_terminal"
    assert isinstance(result.value, dict)
    assert result.value["failure_kind"] == "source_workspace_terminal"
    assert result.value["source_workspace_preserve"] is True
    assert result.value["path"] == str(predecessor.cwd)
    reference_value = result.value["source_workspace_terminal"]
    if receipt_kind == "symlink":
        assert reference_value is None
        assert receipt_path.is_symlink()
    else:
        reference = source_worktree.SourceWorkspaceTerminalReference.from_dict(reference_value)
        terminal = source_manager.read_terminal_failure(9, reference)
        assert terminal.cause == "source_workspace_recovery_receipt_invalid"
    assert result.value["direct_scope_reservation"] == {
        "branch": "writer-branch",
        "base_sha": second,
    }
    assert predecessor.cwd.exists()

    item = WorkItem(
        repo="example/project",
        kind=ItemKind.ISSUE,
        issue=9,
        stage=StageName.IMPLEMENTATION,
        state="WORKTREE_WAIT",
        branch="writer-branch",
    )
    context = StageContext(
        config=PipelineConfig(org="example", repos=["project"]),
        org="example",
        dry_run=False,
        github=cast(Any, MagicMock()),
        paths=SimpleNamespace(repo_root=repo),
    )
    stage = ImplementationStage()
    stage.on_job_done(item, result, context)
    item.state = "DIRTY_DECISION_WAIT"

    outcome = stage.step(item, context)

    assert outcome == StageOutcome(
        Disposition.FINISH_FAIL, "source_workspace_terminal: Preserve the writer."
    )
    assert item.payload["source_workspace_preserve"] is True
    assert item.payload["source_workspace_terminal"] == reference_value
    assert item.worktree == str(predecessor.cwd)


def test_direct_writer_preserves_attached_source_with_durable_obligations(
    tmp_path: Path,
) -> None:
    """A stale attached writer with obligations stays preserved."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    manager.add_obligation(9, SourceLane.IMPLEMENTATION, "durable-cleanup")

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(
            SourceWorkspaceError,
            match="predecessor has durable obligations",
        ) as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    recovery = captured.value.recovery
    assert recovery is not None
    assert recovery.kind is SourceWorkspaceRecoveryKind.DURABLE_OBLIGATIONS
    assert "owning pipeline" in recovery.manual_action
    assert predecessor.cwd.exists()
    assert _git(predecessor.cwd, "symbolic-ref", "HEAD") == ("refs/heads/old-writer-branch")
    receipt = manager._read_receipt(9, SourceLane.IMPLEMENTATION)
    assert receipt is not None
    assert receipt.obligations == ("durable-cleanup",)


def test_direct_writer_checkout_change_after_authorization_has_recovery(
    tmp_path: Path,
) -> None:
    """A changed predecessor remains preserved with a typed recovery action."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first, branch="old-writer-branch")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with manager.implementation_writer_handoff(9) as handoff:
        manager.authorize_direct_implementation_writer_transition(
            9, branch="writer-branch", base_sha=second, handoff=handoff
        )
        original_validate = type(handoff)._validate_direct_transition

        def validate_then_change(
            active_handoff: Any,
            **kwargs: Any,
        ) -> None:
            original_validate(active_handoff, **kwargs)
            _git(predecessor.cwd, "reset", "--hard", second)

        with patch.object(
            type(handoff),
            "_validate_direct_transition",
            autospec=True,
            side_effect=validate_then_change,
        ):
            with pytest.raises(WorktreeCreationReceiptError) as captured:
                worktree_manager.create_worktree(
                    9,
                    "writer-branch",
                    base_sha=second,
                    remote_branch_reserved=True,
                    source_lane=SourceLane.IMPLEMENTATION.value,
                    implementation_writer_handoff=handoff,
                )

    assert predecessor.cwd.exists()
    assert captured.value.recovery is not None
    assert captured.value.recovery["kind"] == "unproven_predecessor"
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "implementation writer direct transition is invalid"


@pytest.mark.parametrize("mutation", ["dirty", "branch", "revision"])
def test_direct_writer_preserves_checkout_changed_after_transition_consumption(
    tmp_path: Path, mutation: str
) -> None:
    """A predecessor change after transition consumption prevents its removal."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first, branch="old-writer-branch")
    writer_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with manager.implementation_writer_handoff(9) as handoff:
        manager.authorize_direct_implementation_writer_transition(
            9, branch="writer-branch", base_sha=second, handoff=handoff
        )
        original_consume = type(handoff)._consume_direct_transition

        def consume_then_change(active_handoff: Any, **kwargs: Any) -> object:
            evidence = original_consume(active_handoff, **kwargs)
            if mutation == "dirty":
                (predecessor.cwd / "pending-change").write_text("preserve\n", encoding="utf-8")
            elif mutation == "branch":
                _git(predecessor.cwd, "switch", "-c", "unexpected-branch")
            else:
                _git(predecessor.cwd, "reset", "--hard", second)
            return evidence

        with patch.object(
            type(handoff),
            "_consume_direct_transition",
            autospec=True,
            side_effect=consume_then_change,
        ):
            with pytest.raises(WorktreeCreationReceiptError) as captured:
                writer_manager.create_worktree(
                    9,
                    "writer-branch",
                    base_sha=second,
                    remote_branch_reserved=True,
                    source_lane=SourceLane.IMPLEMENTATION.value,
                    implementation_writer_handoff=handoff,
                )

    assert predecessor.cwd.exists()
    assert captured.value.recovery is not None
    assert captured.value.recovery["kind"] == "unproven_predecessor"
    if mutation == "dirty":
        assert (predecessor.cwd / "pending-change").read_text(encoding="utf-8") == "preserve\n"
    elif mutation == "branch":
        assert _git(predecessor.cwd, "symbolic-ref", "HEAD") == "refs/heads/unexpected-branch"
    else:
        assert _git(predecessor.cwd, "rev-parse", "HEAD") == second


def test_direct_writer_preserves_change_during_removal(tmp_path: Path) -> None:
    """A change at the removal boundary prevents destructive writer cleanup."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    writer_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    original_run = worktree_manager.__dict__["run"]

    def change_at_remove(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "remove"]:
            (predecessor.cwd / "late-change").write_text("preserve\n", encoding="utf-8")
        return original_run(argv, **kwargs)

    with manager.implementation_writer_handoff(9) as handoff:
        manager.authorize_direct_implementation_writer_transition(
            9,
            branch="writer-branch",
            base_sha=second,
            handoff=handoff,
        )
        with (
            patch.dict(worktree_manager.__dict__, {"run": change_at_remove}),
            pytest.raises(WorktreeCreationReceiptError) as captured,
        ):
            writer_manager.create_worktree(
                9,
                "writer-branch",
                base_sha=second,
                remote_branch_reserved=True,
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )

    assert predecessor.cwd.exists()
    assert (predecessor.cwd / "late-change").read_text(encoding="utf-8") == "preserve\n"
    assert captured.value.recovery is not None
    assert captured.value.recovery["kind"] == "unproven_predecessor"


def test_direct_writer_failed_branch_probe_has_recovery(tmp_path: Path) -> None:
    """A failed predecessor branch probe returns a typed recovery action."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first, branch="old-writer-branch")
    writer_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    original_run = worktree_manager.__dict__["run"]

    def fail_branch_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "symbolic-ref", "--quiet", "HEAD"]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="probe failed")
        return original_run(argv, **kwargs)

    with manager.implementation_writer_handoff(9) as handoff:
        manager.authorize_direct_implementation_writer_transition(
            9, branch="writer-branch", base_sha=second, handoff=handoff
        )
        with (
            patch.dict(worktree_manager.__dict__, {"run": fail_branch_probe}),
            pytest.raises(WorktreeCreationReceiptError) as captured,
        ):
            writer_manager.create_worktree(
                9,
                "writer-branch",
                base_sha=second,
                remote_branch_reserved=True,
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )

    assert predecessor.cwd.exists()
    assert captured.value.recovery is not None
    assert captured.value.recovery["kind"] == "unproven_predecessor"
    assert captured.value.recovery["item_number"] == 9
    assert captured.value.recovery["path"] == str(predecessor.cwd)
    manual_action = captured.value.recovery["manual_action"]
    assert isinstance(manual_action, str)
    assert "Inspect it before cleanup" in manual_action


def test_direct_writer_failed_revision_probe_has_recovery(tmp_path: Path) -> None:
    """A failed predecessor revision probe returns a typed recovery action."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    writer_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    original_run = worktree_manager.__dict__["run"]

    def fail_revision_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            raise subprocess.CalledProcessError(128, argv, stderr="probe failed")
        return original_run(argv, **kwargs)

    with manager.implementation_writer_handoff(9) as handoff:
        manager.authorize_direct_implementation_writer_transition(
            9, branch="writer-branch", base_sha=second, handoff=handoff
        )
        with (
            patch.dict(worktree_manager.__dict__, {"run": fail_revision_probe}),
            pytest.raises(WorktreeCreationReceiptError) as captured,
        ):
            writer_manager.create_worktree(
                9,
                "writer-branch",
                base_sha=second,
                remote_branch_reserved=True,
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )

    assert predecessor.cwd.exists()
    assert captured.value.recovery is not None
    assert captured.value.recovery["kind"] == "unproven_predecessor"
    assert captured.value.recovery["item_number"] == 9


@pytest.mark.parametrize("mutation", ["dirty", "attached", "revision-drift", "obligations"])
def test_direct_writer_transition_preserves_invalid_predecessor(
    tmp_path: Path, mutation: str
) -> None:
    """An invalid detached predecessor cannot arm a direct transition."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    if mutation == "dirty":
        (predecessor.cwd / "pending-change").write_text("preserve\n", encoding="utf-8")
    elif mutation == "attached":
        _git(predecessor.cwd, "switch", "-c", "unexpected-branch")
    elif mutation == "obligations":
        manager.add_obligation(9, SourceLane.IMPLEMENTATION, "durable-cleanup")
    else:
        _git(predecessor.cwd, "reset", "--hard", second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError) as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    assert predecessor.cwd.exists()
    assert manager._read_receipt(9, SourceLane.IMPLEMENTATION) is not None
    assert captured.value.recovery is not None
    assert (
        captured.value.recovery.kind.value
        == {
            "dirty": "dirty_worktree",
            "attached": "branch_mismatch",
            "revision-drift": "revision_drift",
            "obligations": "durable_obligations",
        }[mutation]
    )
    assert captured.value.recovery.item_number == 9
    assert str(captured.value.recovery.path) == str(predecessor.cwd)
    assert str(captured.value.recovery.receipt_path).endswith("9-impl.json")


def test_direct_writer_transition_rejects_receipt_when_path_is_missing(
    tmp_path: Path,
) -> None:
    """A receipt without its checkout remains available for manual recovery."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "remove", str(predecessor.cwd))

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="receipt path is missing") as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    assert captured.value.recovery is not None
    assert captured.value.recovery.kind is SourceWorkspaceRecoveryKind.RECEIPT_PATH_MISSING
    assert receipt_path.exists()
    assert not manager.path_for(9, SourceLane.IMPLEMENTATION).exists()


def test_implementation_writer_handoff_cannot_be_forged_outside_manager(
    tmp_path: Path,
) -> None:
    """The handoff capability requires the issuer context manager."""
    assert not hasattr(implementation_writer, "_CONSTRUCTION_TOKEN")
    assert not hasattr(implementation_writer, "_new_implementation_writer_handoff")
    assert not hasattr(implementation_writer, "_set_implementation_writer_handoff_active")
    handoff_type: Any = implementation_writer.ImplementationWriterHandoff
    with pytest.raises(TypeError, match="issuer context manager"):
        handoff_type(tmp_path, 9, token=object())
    assert not hasattr(implementation_writer.ImplementationWriterHandoff, "_activate")

    repo, _, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        base_branch=second,
    )
    forged = object.__new__(implementation_writer.ImplementationWriterHandoff)
    object.__setattr__(forged, "_active", True)
    object.__setattr__(forged, "_construction_token", object())
    object.__setattr__(forged, "_item_number", 9)
    object.__setattr__(
        forged,
        "_lock_path",
        worktree_manager.source_lane_lock_path(repo, 9, "impl"),
    )
    object.__setattr__(forged, "_repo_root", repo.resolve())
    with pytest.raises(WorktreeCreationReceiptError, match="inactive"):
        worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=forged,
        )


def test_implementation_writer_handoff_rejects_a_noncanonical_lock_path(
    tmp_path: Path,
) -> None:
    """A handoff issued on another lock cannot authorize writer creation."""
    repo, _, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        base_branch=second,
    )
    wrong_lock = tmp_path / "not-the-source-lane.lock"

    with implementation_writer.implementation_writer_handoff(repo, 9, wrong_lock) as handoff:
        with pytest.raises(WorktreeCreationReceiptError, match="inactive"):
            worktree_manager.create_worktree(
                9,
                "writer-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )


def test_implementation_writer_creation_requires_an_active_handoff(tmp_path: Path) -> None:
    """An implementation writer cannot allocate without its lane handoff."""
    repo, _, second = _repository(tmp_path)
    manager = WorktreeManager(repo_root=repo, base_branch=second)

    with pytest.raises(WorktreeCreationReceiptError, match="handoff"):
        manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
        )


def test_fresh_writer_claim_blocks_concurrent_adoption_until_claim_finishes(
    tmp_path: Path,
) -> None:
    """A fresh writer stays intact while a competing adoption waits for its claim."""
    repo, _, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        base_branch=second,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    adopted_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    adoption_errors: list[Exception] = []
    contention_observed = threading.Event()
    adoption_mutation_started = threading.Event()
    adoption_finished = threading.Event()

    adopted_source = SourceWorkspaceManager(repo, repository="example/project")

    def adopt() -> None:
        try:
            with file_lock(
                adopted_source._lane_lock_path(9, SourceLane.IMPLEMENTATION),
                blocking=False,
                require_exclusive=True,
            ):
                raise AssertionError("source lease was not held")
        except LockUnavailableError:
            contention_observed.set()
        try:
            with adopted_source.implementation_writer_handoff(9) as adopted_handoff:
                adopted_source.authorize_adopted_implementation_writer_transition(
                    9,
                    branch="writer-branch",
                    expected_head=second,
                    handoff=adopted_handoff,
                )
                adopted_manager.create_worktree(
                    9,
                    "writer-branch",
                    source_lane=SourceLane.IMPLEMENTATION.value,
                    implementation_adoption_head=second,
                    implementation_writer_handoff=adopted_handoff,
                )
        except Exception as exc:  # pragma: no cover - assertion below reports failures
            adoption_errors.append(exc)
        finally:
            adoption_finished.set()

    real_adoption = adopted_manager._add_authenticated_adopted_implementation_writer

    def observe_adoption(*args: Any, **kwargs: Any) -> None:
        adoption_mutation_started.set()
        real_adoption(*args, **kwargs)

    worker = threading.Thread(target=adopt)
    with patch.object(
        adopted_manager,
        "_add_authenticated_adopted_implementation_writer",
        side_effect=observe_adoption,
    ):
        with source_manager.implementation_writer_handoff(9) as handoff:
            writer = worktree_manager.create_worktree(
                9,
                "writer-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )
            authority = worktree_manager.implementation_writer_authority(writer)
            _git(repo, "push", "origin", "writer-branch")
            worker.start()
            assert contention_observed.wait(timeout=5)
            assert not adoption_mutation_started.is_set()
            assert writer.exists()
            assert _git(writer, "rev-parse", "HEAD") == second

            source_manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=authority,
                handoff=handoff,
            )

        assert adoption_mutation_started.wait(timeout=5)

    assert adoption_finished.wait(timeout=5)
    assert not adoption_errors


def test_worker_pool_adoption_waits_for_active_source_lease(
    tmp_path: Path,
) -> None:
    """WorkerPool adoption preserves a leased writer until the lease releases."""
    repo, _, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        base_branch=second,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    with source_manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        binding = source_manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )
    _git(repo, "push", "origin", "writer-branch")

    completion_q: CompletionQueue = queue.Queue()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    contention_observed = threading.Event()
    adoption_mutation_started = threading.Event()
    sync_completed = threading.Event()
    real_handoff = SourceWorkspaceManager.implementation_writer_handoff

    @contextmanager
    def observed_handoff(
        manager: SourceWorkspaceManager,
        item_number: int,
        *,
        deadline: _PreparationDeadline | None = None,
    ):
        try:
            with file_lock(
                manager._lane_lock_path(item_number, SourceLane.IMPLEMENTATION),
                blocking=False,
                require_exclusive=True,
            ):
                raise AssertionError("source lease was not held")
        except LockUnavailableError:
            contention_observed.set()
        with real_handoff(manager, item_number, deadline=deadline) as handoff:
            yield handoff

    real_sync = pool._sync_worktree_to_remote_branch

    def observed_sync(*args: Any, **kwargs: Any) -> None:
        real_sync(*args, **kwargs)
        sync_completed.set()

    real_adoption = WorktreeManager._add_authenticated_adopted_implementation_writer

    def observed_adoption(manager: WorktreeManager, *args: Any, **kwargs: Any) -> None:
        adoption_mutation_started.set()
        real_adoption(manager, *args, **kwargs)

    job = GitJob(
        repo="example/project",
        op="create_worktree",
        timeout_s=60,
        kwargs={
            "issue_number": 9,
            "branch_name": "writer-branch",
            "repo_root": str(repo),
            "source_lane": SourceLane.IMPLEMENTATION.value,
            "sync_to_remote": True,
            "pr_number": 90,
            "implementation_adoption_head": second,
        },
    )
    try:
        with (
            patch.object(
                SourceWorkspaceManager,
                "implementation_writer_handoff",
                observed_handoff,
            ),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=({}, ("-c", "credential.helper=")),
            ) as remote_configuration,
            patch.object(pool, "_sync_worktree_to_remote_branch", observed_sync),
            patch.object(
                WorktreeManager,
                "_add_authenticated_adopted_implementation_writer",
                observed_adoption,
            ),
        ):
            with source_manager.acquire(binding):
                pool.submit(job, StageName.REPO)
                assert contention_observed.wait(timeout=5)
                assert not adoption_mutation_started.is_set()
                assert writer.exists()
                assert _git(writer, "rev-parse", "HEAD") == second
                assert _git(writer, "branch", "--show-current") == "writer-branch"

            _, result = completion_q.get(timeout=10)
            assert adoption_mutation_started.is_set()
            assert result.ok is True, (result.error, remote_configuration.call_args_list)
            assert sync_completed.is_set()
            assert result.value == {
                "path": str(writer),
                "impl_source_revision": second,
                "dirty": False,
                "status": "",
                "diff": "",
                "source_workspace": replace(binding, generation=binding.generation + 1).to_dict(),
                "source_receipt": source_manager._require_receipt(
                    9, SourceLane.IMPLEMENTATION
                ).to_dict(),
            }
        rebound = source_manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="writer-branch",
        )
        assert rebound.cwd == binding.cwd
        assert rebound.revision == binding.revision
        assert rebound.generation == binding.generation + 1
    finally:
        pool.shutdown(mark_interrupted=False)


def test_claim_does_not_reacquire_the_active_lane_lock(tmp_path: Path) -> None:
    """Claim uses the caller's active handoff without nesting its file lock."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    lock_paths: list[Path] = []

    @contextmanager
    def tracked_lock(path: Path, **kwargs: Any):
        lock_paths.append(path)
        with file_lock(path, **kwargs):
            yield

    with patch("hephaestus.automation.implementation_writer.file_lock", side_effect=tracked_lock):
        with manager.implementation_writer_handoff(9) as handoff:
            writer = worktree_manager.create_worktree(
                9,
                "writer-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=worktree_manager.implementation_writer_authority(writer),
                handoff=handoff,
            )

    assert lock_paths == [manager._lane_lock_path(9, SourceLane.IMPLEMENTATION)]


def test_implementation_writer_rejects_wrong_or_inactive_handoff(tmp_path: Path) -> None:
    """A handoff for another item or an ended handoff cannot allocate a writer."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(WorktreeCreationReceiptError, match="handoff"):
            worktree_manager.create_worktree(
                10,
                "writer-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )
    with pytest.raises(WorktreeCreationReceiptError, match="inactive"):
        worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )


def test_claim_implementation_writer_converts_receipt_write_error(
    tmp_path: Path,
) -> None:
    """A receipt write failure is an ownership error, not a retryable Git error."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        authority = worktree_manager.implementation_writer_authority(writer)

        with (
            patch.object(manager, "_write_receipt", side_effect=OSError("disk full")),
            pytest.raises(
                SourceWorkspaceError, match="cannot record implementation writer receipt"
            ),
        ):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=authority,
                handoff=handoff,
            )

    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_authority_failure_preserves_existing_receipt(
    tmp_path: Path,
) -> None:
    """An authority failure does not alter an existing durable receipt."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )

        with (
            patch.object(manager, "_write_receipt") as write_receipt,
            pytest.raises(SourceWorkspaceError, match="authority is invalid"),
        ):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    write_receipt.assert_not_called()
    assert (
        manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="writer-branch",
        ).cwd
        == writer
    )


def test_claim_implementation_writer_restart_cannot_adopt_unconsumed_authority(
    tmp_path: Path,
) -> None:
    """A stopped authority handoff leaves no durable writer ownership record."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        authority = worktree_manager.implementation_writer_authority(writer)

        with (
            patch(
                "hephaestus.automation.source_worktree.consume_implementation_writer_authority",
                side_effect=RuntimeError("worker stopped"),
            ),
            pytest.raises(SourceWorkspaceError, match="authority is invalid"),
        ):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=authority,
                handoff=handoff,
            )

    restarted = SourceWorkspaceManager(repo, repository="example/project")
    assert not (restarted.state_dir / "9-impl.json").exists()
    with pytest.raises(SourceWorkspaceError, match="not owned by this lane"):
        restarted.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="writer-branch",
        )


def test_claim_implementation_writer_rejects_clean_unauthorized_worktree(
    tmp_path: Path,
) -> None:
    """A clean deterministic path is not writer evidence without an authority."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "writer-branch", str(writer), second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="authority"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_constructed_authority(
    tmp_path: Path,
) -> None:
    """An opaque authority that the manager did not mint cannot claim a writer."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "writer-branch", str(writer), second)
    constructed = cast(ImplementationWriterAuthority, object())

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="authority"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=constructed,
                handoff=handoff,
            )

    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_stale_authority(
    tmp_path: Path,
) -> None:
    """An authority from before a clean writer move cannot authorize the lane."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        authority = worktree_manager.implementation_writer_authority(writer)
        _git(writer, "reset", "--hard", first)

        with pytest.raises(SourceWorkspaceError, match="authority"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=authority,
                handoff=handoff,
            )

    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_non_lane_path(tmp_path: Path) -> None:
    """A writer outside the deterministic lane cannot become source state."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    foreign_writer = tmp_path / "foreign-writer"
    _git(repo, "worktree", "add", "-b", "writer-branch", str(foreign_writer), second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="does not match the deterministic lane"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=foreign_writer,
                handoff=handoff,
            )

    assert not manager.path_for(9, SourceLane.IMPLEMENTATION).exists()


def test_claim_implementation_writer_rejects_an_incompatible_receipt(
    tmp_path: Path,
) -> None:
    """A controlled handoff cannot replace a lane's recorded branch."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="previous-writer-branch",
    ).cwd
    _git(writer, "switch", "-c", "writer-branch")

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="incompatible source workspace receipt"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert _git(writer, "branch", "--show-current") == "writer-branch"


def test_claim_implementation_writer_rejects_a_wrong_attached_branch(
    tmp_path: Path,
) -> None:
    """A deterministic path does not authorize a different writer branch."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "other-writer-branch", str(writer), second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="does not match the requested branch"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert _git(writer, "branch", "--show-current") == "other-writer-branch"
    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_preserves_a_dirty_writer(tmp_path: Path) -> None:
    """A dirty controlled writer remains available for recovery."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "writer-branch", str(writer), second)
    dirty_file = writer / "recover-me.txt"
    dirty_file.write_text("preserve this\n", encoding="utf-8")

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="dirty and preserved"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert dirty_file.read_text(encoding="utf-8") == "preserve this\n"
    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_foreign_receipt(tmp_path: Path) -> None:
    """A different repository identity cannot refresh a writer receipt."""
    repo, _, second = _repository(tmp_path)
    first = SourceWorkspaceManager(repo, repository="one/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=first.base_dir,
        base_branch=second,
    )
    with first.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        first.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )
    second_manager = SourceWorkspaceManager(repo, repository="two/project")

    with second_manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="owned by another repository") as captured:
            second_manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert _git(writer, "branch", "--show-current") == "writer-branch"
    assert captured.value.recovery is not None
    assert captured.value.recovery.kind is SourceWorkspaceRecoveryKind.FOREIGN_OWNER
    assert "one/project" in captured.value.recovery.manual_action


def test_current_review_lane_can_be_cleaned_by_pipeline_contract(tmp_path: Path) -> None:
    """A review lane created with the current deterministic name is removable."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(7, SourceLane.REVIEW, second)

    result = run_cleanup_job(
        GitJob(
            repo="example/project",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(binding.cwd),
                "repo_root": str(repo),
                "issue_number": 7,
                "expected_head": second,
                "expected_detached": True,
                "source_lane": SourceLane.REVIEW.value,
            },
        )
    )

    assert result.ok is True
    assert not binding.cwd.exists()
    assert not manager._receipt_path(7, SourceLane.REVIEW).exists()


def test_review_cleanup_reconciles_receipt_after_physical_cleanup(tmp_path: Path) -> None:
    """A valid stale review receipt is removed after physical cleanup."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(7, SourceLane.REVIEW, second)
    receipt_path = manager._receipt_path(7, SourceLane.REVIEW)
    _git(repo, "worktree", "remove", str(binding.cwd))
    _git(repo, "worktree", "prune")

    result = run_cleanup_job(
        GitJob(
            repo="example/project",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(binding.cwd),
                "repo_root": str(repo),
                "issue_number": 7,
                "expected_head": second,
                "expected_detached": True,
                "source_lane": SourceLane.REVIEW.value,
            },
        )
    )

    assert result.ok is True
    assert not binding.cwd.exists()
    assert not receipt_path.exists()


def test_review_cleanup_without_receipt_removes_worktree_idempotently(tmp_path: Path) -> None:
    """A pre-binding review checkout can be removed twice without a receipt."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    path = manager.path_for(8, SourceLane.REVIEW)
    _git(repo, "worktree", "add", "--detach", str(path), second)
    assert not manager._receipt_path(8, SourceLane.REVIEW).exists()
    job = GitJob(
        repo="example/project",
        op="remove_worktree",
        timeout_s=60,
        kwargs={
            "worktree_path": str(path),
            "repo_root": str(repo),
            "issue_number": 8,
            "expected_head": second,
            "expected_detached": True,
            "source_lane": SourceLane.REVIEW.value,
        },
    )

    first = run_cleanup_job(job)
    second_result = run_cleanup_job(job)

    assert first.ok is True
    assert second_result.ok is True
    assert not path.exists()


def test_review_cleanup_rejects_invalid_present_receipt(tmp_path: Path) -> None:
    """A present receipt with a changed revision cannot use absent-receipt cleanup."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(9, SourceLane.REVIEW, second)
    receipt_path = manager._receipt_path(9, SourceLane.REVIEW)

    result = run_cleanup_job(
        GitJob(
            repo="example/project",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(binding.cwd),
                "repo_root": str(repo),
                "issue_number": 9,
                "expected_head": first,
                "expected_detached": True,
                "source_lane": SourceLane.REVIEW.value,
            },
        )
    )

    assert result.ok is False
    assert result.error == "source workspace receipt revision changed"
    assert binding.cwd.exists()
    assert receipt_path.exists()


def test_review_cleanup_receipt_error_names_operation_path_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt-removal error preserves its exact metadata path and cause."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(10, SourceLane.REVIEW, second)
    receipt_path = manager._receipt_path(10, SourceLane.REVIEW)
    original_unlink = Path.unlink

    def refuse_receipt_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == receipt_path:
            raise OSError("receipt access denied")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_receipt_unlink)
    result = run_cleanup_job(
        GitJob(
            repo="example/project",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(binding.cwd),
                "repo_root": str(repo),
                "issue_number": 10,
                "expected_head": second,
                "expected_detached": True,
                "source_lane": SourceLane.REVIEW.value,
            },
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert "source workspace receipt removal failed" in result.error
    assert str(receipt_path) in result.error
    assert "receipt access denied" in result.error
    assert receipt_path.exists()


def test_dirty_lane_is_preserved_and_rejected(tmp_path: Path) -> None:
    """Failure state is never erased by preparation or cleanup."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(5, SourceLane.IMPLEMENTATION, second)
    (binding.cwd / "untracked.txt").write_text("recover me\n", encoding="utf-8")

    with pytest.raises(SourceWorkspaceError, match="dirty"):
        manager.prepare(5, SourceLane.IMPLEMENTATION, second)
    with pytest.raises(SourceWorkspaceError, match="dirty"):
        manager.cleanup(5, SourceLane.IMPLEMENTATION)

    assert (binding.cwd / "untracked.txt").read_text(encoding="utf-8") == "recover me\n"


def test_repository_identity_prevents_equal_number_collision(tmp_path: Path) -> None:
    """Repository-qualified ownership prevents cross-repository adoption."""
    repo, _, second = _repository(tmp_path)
    first_manager = SourceWorkspaceManager(repo, repository="one/project")
    second_manager = SourceWorkspaceManager(repo, repository="two/project")

    first = first_manager.prepare(6, SourceLane.REVIEW, second)

    with pytest.raises(SourceWorkspaceError, match="owned by another repository"):
        second_manager.prepare(6, SourceLane.REVIEW, second)

    assert first.cwd.name == "auto-6-review"


def test_direct_writer_transition_revalidates_predecessor_branch_before_removal(
    tmp_path: Path,
) -> None:
    """A predecessor branch change after authorization preserves the checkout."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", f"{second}:refs/heads/writer-branch")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with pytest.raises(
        source_worktree.SourceWorkspaceTerminalError, match="changed after authorization"
    ) as terminal:
        with source_manager.implementation_writer_handoff(9) as handoff:
            source_manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )
            _git(predecessor.cwd, "switch", "-c", "foreign-predecessor")

            worktree_manager.create_worktree(
                9,
                "writer-branch",
                base_sha=second,
                remote_branch_reserved=True,
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )

    assert isinstance(terminal.value.__cause__, WorktreeCreationReceiptError)
    assert source_manager._transition_path(9).exists()
    assert predecessor.cwd.exists()
    assert _git(predecessor.cwd, "symbolic-ref", "--short", "HEAD") == "foreign-predecessor"
    assert _git(predecessor.cwd, "rev-parse", "HEAD") == first


def test_direct_writer_transition_journal_reconciles_after_restart(tmp_path: Path) -> None:
    """A pending handoff journal restores a removed predecessor after restart."""
    repo, first, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    journal_path = source_manager.state_dir / "9-impl-transition.json"

    with source_manager.implementation_writer_handoff(9) as handoff:
        source_manager.authorize_direct_implementation_writer_transition(
            9,
            branch="writer-branch",
            base_sha=second,
            handoff=handoff,
        )

        assert journal_path.is_file()
        assert journal_path.stat().st_mode & 0o777 == 0o600
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        assert journal["schema_version"] == 1
        assert journal["repository"] == "example/project"
        assert journal["item_number"] == 9
        assert journal["lane"] == "impl"
        assert journal["predecessor"]["revision"] == first
        assert journal["successor"]["branch"] == "writer-branch"

        worktree_manager = WorktreeManager(repo_root=repo, base_dir=source_manager.base_dir)
        worktree_manager._remove_worktree_path_forcefully(predecessor.cwd)

    restarted = SourceWorkspaceManager(repo, repository="example/project")
    with restarted.implementation_writer_handoff(9):
        pass

    assert predecessor.cwd.exists()
    assert _git(predecessor.cwd, "rev-parse", "HEAD") == first
    assert _git(predecessor.cwd, "branch", "--show-current") == ""
    assert not journal_path.exists()


def test_adopted_writer_cannot_bypass_the_common_handoff_lifecycle(tmp_path: Path) -> None:
    """An adopted writer replacement requires consumed source transition evidence."""
    repo, _, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = source_manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="adopted-branch",
    )
    _git(repo, "push", "origin", "HEAD:refs/heads/adopted-branch")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with source_manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(WorktreeCreationReceiptError, match="transition"):
            worktree_manager.create_worktree(
                9,
                "adopted-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_adoption_head=second,
                implementation_writer_handoff=handoff,
            )

    assert writer.cwd.exists()
    assert _git(writer.cwd, "symbolic-ref", "--short", "HEAD") == "adopted-branch"


def test_writer_transition_rejects_a_valid_replacement_journal(tmp_path: Path) -> None:
    """A J1 capability cannot consume a different valid J2 journal."""
    repo, first, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    worktree_manager = WorktreeManager(repo_root=repo, base_dir=source_manager.base_dir)
    branch = f"9-auto-impl-direct-{'a' * 32}"

    with source_manager.implementation_writer_handoff(9) as handoff:
        source_manager.authorize_direct_implementation_writer_transition(
            9,
            branch=branch,
            base_sha=second,
            handoff=handoff,
        )
        first_journal = source_manager._read_writer_transition(9)
        assert first_journal is not None
        replacement_journal = source_worktree._ImplementationWriterTransitionJournal.create(
            repository=first_journal.repository,
            repository_identity=first_journal.repository_identity,
            ownership_key=first_journal.ownership_key,
            item_number=first_journal.item_number,
            predecessor=first_journal.predecessor,
            successor=replace(first_journal.successor, branch="other-writer-branch"),
            transition=first_journal.transition,
            target_ref_revision=first_journal.target_ref_revision,
        )
        source_manager._write_writer_transition(replacement_journal)

        with pytest.raises(WorktreeCreationReceiptError, match="changed after authorization"):
            worktree_manager.create_worktree(
                9,
                branch,
                source_lane="impl",
                base_sha=second,
                remote_branch_reserved=True,
                direct_worktree_nonce="a" * 32,
                implementation_writer_handoff=handoff,
            )
        current = source_manager._read_writer_transition(9)
        assert current is not None
        assert current.journal_digest == replacement_journal.journal_digest

    assert predecessor.cwd.exists()
    assert _git(predecessor.cwd, "rev-parse", "HEAD") == first


def test_writer_transition_recovery_rejects_a_foreign_registered_checkout(
    tmp_path: Path,
) -> None:
    """Recovery rejects an exact checkout from a different Git common directory."""
    repo, first, second = _repository(tmp_path)
    foreign_repo = tmp_path / "foreign-repository"
    _git(tmp_path, "clone", str(repo), str(foreign_repo))
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    journal_path = source_manager.state_dir / "9-impl-transition.json"

    with pytest.raises(RuntimeError, match="simulated restart"):
        with source_manager.implementation_writer_handoff(9) as handoff:
            source_manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )
            handoff._consume_writer_transition(
                path=predecessor.cwd,
                predecessor_revision=first,
                predecessor_detached=True,
                predecessor_branch=None,
                successor_branch="writer-branch",
                successor_revision=second,
                transition="direct",
                target_ref_revision=None,
            )
            _git(repo, "worktree", "remove", str(predecessor.cwd))
            handoff._mark_transition_phase("successor_creating")
            _git(
                foreign_repo,
                "worktree",
                "add",
                "-b",
                "writer-branch",
                str(predecessor.cwd),
                second,
            )
            handoff._mark_transition_phase("successor_created")
            raise RuntimeError("simulated restart")

    assert journal_path.exists()
    restarted = SourceWorkspaceManager(repo, repository="example/project")
    with pytest.raises(SourceWorkspaceError, match=r"registered.*repository"):
        with restarted.implementation_writer_handoff(9):
            pass

    assert journal_path.exists()
    receipt = restarted._read_receipt(9, SourceLane.IMPLEMENTATION)
    assert receipt is not None
    assert receipt.revision == first
    assert _git(predecessor.cwd, "rev-parse", "HEAD") == second


@pytest.mark.parametrize("failure", [None, "remote", "receipt", "branch", "dirty", "write"])
def test_writer_publication_advances_receipt_and_permits_reuse(
    tmp_path: Path, failure: str | None
) -> None:
    """A controlled published commit permits the next writer acquisition."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktrees = WorktreeManager(repo_root=repo, base_dir=manager.base_dir, base_branch=second)
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktrees.create_worktree(
            9, "writer-branch", source_lane="impl", implementation_writer_handoff=handoff
        )
        original = manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktrees.implementation_writer_authority(writer),
            handoff=handoff,
        )
    remote = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(remote))
    _git(writer, "remote", "add", "origin", str(remote))
    _git(writer, "push", "origin", "writer-branch")
    (writer / "tracked.txt").write_text("implementation\n", encoding="utf-8")
    with manager.implementation_publication(9, branch="writer-branch", path=writer) as advance:
        _git(writer, "commit", "-am", "implementation")
        head = _git(writer, "rev-parse", "HEAD")
        _git(writer, "push", "origin", "writer-branch")
        remote_head = _git(writer, "ls-remote", "origin", "refs/heads/writer-branch").split()[0]
        original_receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        if failure == "remote":
            remote_head = second
        elif failure == "receipt":
            manager._write_receipt(replace(original_receipt, generation=100))
        elif failure == "branch":
            _git(writer, "switch", "-c", "changed-branch")
        elif failure == "dirty":
            (writer / "tracked.txt").write_text("later edit\n", encoding="utf-8")
        if failure:
            with (
                patch.object(manager, "_write_receipt", side_effect=OSError("write failed"))
                if failure == "write"
                else nullcontext()
            ):
                with pytest.raises((SourceWorkspaceError, OSError)):
                    advance(head, remote_head)
            assert _git(writer, "rev-parse", "HEAD") == head
            assert (
                manager._require_receipt(9, SourceLane.IMPLEMENTATION).revision == original.revision
            )
            return
        updated = advance(head, remote_head)
    assert updated.revision == head
    assert updated.generation == original.generation + 1
    with pytest.raises(SourceWorkspaceError, match="authority expired"):
        advance(head, remote_head)
    manager.add_obligation(9, SourceLane.IMPLEMENTATION, "review")
    with manager.implementation_publication(9, branch="writer-branch", path=writer) as no_op:
        unchanged = no_op(head, remote_head)
    assert unchanged.generation == updated.generation
    assert manager._require_receipt(9, SourceLane.IMPLEMENTATION).obligations == ("review",)
    rebound = manager.prepare(9, SourceLane.IMPLEMENTATION, head, branch="writer-branch")
    with manager.acquire(rebound):
        assert (writer / "tracked.txt").read_text(encoding="utf-8") == "implementation\n"


@pytest.mark.parametrize("publication_failure", [False, True])
def test_worker_publication_records_head_before_review_reuse(
    tmp_path: Path, publication_failure: bool
) -> None:
    """The worker records its commit before a later review remediation."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(
        repo, repository="example/project", base_dir=repo / "build" / ".worktrees"
    )
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=CompletionQueue(),
        lock_dir=tmp_path / "locks",
    )
    job = GitJob(
        repo="example/project",
        op="create_worktree",
        timeout_s=60,
        kwargs={
            "issue_number": 9,
            "branch_name": "writer-branch",
            "repo_root": str(repo),
            "source_lane": "impl",
            "base_sha": second,
        },
    )
    remote = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    with patch.object(
        pool,
        "_authenticated_remote_git_configuration",
        return_value=(build_git_child_env(), ("-c", "protocol.file.allow=always")),
    ):
        created = pool._run_git(job)
    assert created.ok, created.error
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(writer, "push", "origin", "writer-branch")
    (writer / "tracked.txt").write_text("implementation\n", encoding="utf-8")

    def commit(*args: Any, **kwargs: Any) -> bool:
        with (
            pytest.raises(LockUnavailableError),
            file_lock(
                manager._lane_lock_path(9, SourceLane.IMPLEMENTATION),
                blocking=False,
                require_exclusive=True,
            ),
        ):
            pass
        _git(writer, "commit", "-am", "implementation")
        return True

    publish = GitJob(
        repo="example/project",
        op="commit_push",
        timeout_s=60,
        workspace=WorkspaceBinding.from_dict(created.value["source_workspace"]),
        kwargs={
            "issue_number": 9,
            "branch": "writer-branch",
            "repo_root": str(repo),
            "worktree_path": str(writer),
            "source_lane": "impl",
            "agent": "codex",
            "allowed_paths": ("tracked.txt",),
            "scope_history_base_sha": second,
        },
    )
    with (
        patch.object(pool, "_commit_if_changes_with_controlled_signing", side_effect=commit),
        patch.object(
            pool,
            "_authenticated_remote_git_configuration",
            return_value=(build_git_child_env(), ("-c", "protocol.file.allow=always")),
        ),
    ):
        with (
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.push_branch",
                side_effect=RuntimeError("push failed"),
            )
            if publication_failure
            else nullcontext()
        ):
            result = pool._run_git(publish)
    assert result.ok is not publication_failure, result.error
    if publication_failure:
        assert result.value["publication_state"] == "remote_unchanged"
    head = _git(writer, "rev-parse", "HEAD")
    assert manager._require_receipt(9, SourceLane.IMPLEMENTATION).revision == head
    rebound = manager.prepare(9, SourceLane.IMPLEMENTATION, head, branch="writer-branch")
    with manager.acquire(rebound):
        assert _git(writer, "rev-parse", "HEAD") == head


def test_writer_publication_failure_preserves_ownership_error_class(tmp_path: Path) -> None:
    """An invalid publication request keeps the ownership failure prefix."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=CompletionQueue(),
        lock_dir=tmp_path / "locks",
    )
    result = pool._git_commit_push(
        GitJob(
            repo="example/project",
            op="commit_push",
            timeout_s=60,
            kwargs={"source_lane": "impl"},
        )
    )
    assert not result.ok
    assert result.error == "source_workspace_ownership_unavailable: publication binding invalid"


@pytest.mark.parametrize("failure", [None, "dirty", "head", "receipt", "write", "readback"])
def test_controlled_local_commit_records_unpublished_writer(
    tmp_path: Path, failure: str | None
) -> None:
    """A failed publication can retain exact local ownership for one retry."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktrees = WorktreeManager(repo_root=repo, base_dir=manager.base_dir, base_branch=second)
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktrees.create_worktree(
            9, "writer-branch", source_lane="impl", implementation_writer_handoff=handoff
        )
        original = manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktrees.implementation_writer_authority(writer),
            handoff=handoff,
        )
    manager.add_obligation(9, SourceLane.IMPLEMENTATION, "review")
    with manager.implementation_local_commit(9, branch="writer-branch", path=writer) as record:
        (writer / "tracked.txt").write_text("local implementation\n", encoding="utf-8")
        _git(writer, "commit", "-am", "implementation")
        head = _git(writer, "rev-parse", "HEAD")
        original_receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        if failure == "dirty":
            (writer / "tracked.txt").write_text("later edit\n", encoding="utf-8")
        elif failure == "head":
            _git(writer, "commit", "--allow-empty", "-m", "later change")
        elif failure == "receipt":
            manager._write_receipt(replace(original_receipt, generation=100))
        if failure is not None:
            with ExitStack() as faults:
                if failure == "write":
                    faults.enter_context(
                        patch.object(manager, "_write_receipt", side_effect=OSError("write failed"))
                    )
                if failure == "readback":
                    faults.enter_context(
                        patch.object(manager, "_read_receipt", side_effect=[original_receipt, None])
                    )
                with pytest.raises((SourceWorkspaceError, OSError)):
                    record(head)
            assert _git(writer, "cat-file", "-t", head) == "commit"
            if failure == "readback":
                assert manager._require_receipt(9, SourceLane.IMPLEMENTATION).revision == head
            else:
                assert (
                    manager._require_receipt(9, SourceLane.IMPLEMENTATION).revision
                    == original.revision
                )
            return
        updated = record(head)
        with pytest.raises(SourceWorkspaceError, match="authority expired"):
            record(head)
    with pytest.raises(SourceWorkspaceError, match="authority expired"):
        record(head)
    assert updated.revision == head
    assert updated.generation == original.generation + 1
    assert manager._require_receipt(9, SourceLane.IMPLEMENTATION).obligations == ("review",)
    with manager.implementation_local_commit(9, branch="writer-branch", path=writer) as no_op:
        assert no_op(head).generation == updated.generation


@pytest.mark.parametrize(
    "failure", ["baseline", "observed", "phase", "pushed", "write", "readback"]
)
def test_worker_rejects_uncertain_local_publication_receipt(tmp_path: Path, failure: str) -> None:
    """Invalid facts or receipt uncertainty cannot produce publication success."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(
        repo, repository="example/project", base_dir=repo / "build" / ".worktrees"
    )
    worktrees = WorktreeManager(repo_root=repo, base_dir=manager.base_dir, base_branch=second)
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktrees.create_worktree(
            9, "writer", source_lane="impl", implementation_writer_handoff=handoff
        )
        manager.claim_implementation_writer(
            9,
            branch="writer",
            path=writer,
            authority=worktrees.implementation_writer_authority(writer),
            handoff=handoff,
        )
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=CompletionQueue(),
        lock_dir=tmp_path / "locks",
    )
    head = second

    def commit(*args: Any, **kwargs: Any) -> JobResult:
        nonlocal head
        (writer / "tracked.txt").write_text("controlled change\n", encoding="utf-8")
        _git(writer, "commit", "-am", "implementation")
        head = _git(writer, "rev-parse", "HEAD")
        receipt: dict[str, Any] = {
            "publication_state": "published",
            "head_sha": head,
            "baseline_remote_sha": second,
            "observed_remote_sha": head,
            "pushed": True,
            "refresh_phase": None,
        }
        if failure == "baseline":
            receipt["baseline_remote_sha"] = "invalid"
        elif failure == "observed":
            receipt["observed_remote_sha"] = second
        elif failure == "phase":
            receipt["refresh_phase"] = "publish"
        elif failure == "pushed":
            receipt["pushed"] = 1
        return JobResult(ok=True, value=receipt)

    job = GitJob(
        repo="example/project",
        op="commit_push",
        timeout_s=60,
        kwargs={
            "issue_number": 9,
            "branch": "writer",
            "repo_root": str(repo),
            "worktree_path": str(writer),
            "source_lane": "impl",
        },
    )
    real_write = SourceWorkspaceManager._write_receipt

    def write_then_fail(self: SourceWorkspaceManager, receipt: Any) -> None:
        real_write(self, receipt)
        raise OSError("receipt completion unavailable")

    with patch.object(pool, "_git_commit_push_inner", side_effect=commit), ExitStack() as faults:
        if failure in {"write", "readback"}:
            faults.enter_context(
                patch.object(
                    SourceWorkspaceManager,
                    "_write_receipt",
                    side_effect=OSError("write unavailable"),
                )
                if failure == "write"
                else patch.object(SourceWorkspaceManager, "_write_receipt", new=write_then_fail)
            )
        result = pool._git_commit_push(job)
    assert result.ok is False
    assert isinstance(result.error, str)
    assert result.error.startswith("source_workspace_ownership_unavailable:")
    assert _git(writer, "rev-parse", "HEAD") == head
    assert manager._require_receipt(9, SourceLane.IMPLEMENTATION).revision == (
        head if failure == "readback" else second
    )


def test_terminal_failure_preserves_journal_on_handoff_exception(tmp_path: Path) -> None:
    """A failed handoff keeps the journal for terminal verification."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    with pytest.raises(RuntimeError, match="terminal failure"):
        with manager.implementation_writer_handoff(9) as handoff:
            manager.authorize_direct_implementation_writer_transition(
                9, branch="writer-branch", base_sha=second, handoff=handoff
            )
            raise RuntimeError("terminal failure")
    assert manager._transition_path(9).exists()
    assert (manager.state_dir / "9-impl-terminal.json").exists()


def _terminal_failed_transition(
    tmp_path: Path, phase: str = "prepared"
) -> tuple[SourceWorkspaceManager, source_worktree.SourceWorkspaceTerminalReference]:
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    with pytest.raises(source_worktree.SourceWorkspaceTerminalError) as raised:
        with manager.implementation_writer_handoff(9) as handoff:
            manager.authorize_direct_implementation_writer_transition(
                9, branch="writer-branch", base_sha=second, handoff=handoff
            )
            journal = manager._read_writer_transition(9)
            assert journal is not None
            manager._update_writer_transition_phase(9, journal.journal_digest, phase)
            raise source_worktree.SourceWorkspaceTerminalError(
                "terminal failure", requested_branch="writer-branch", requested_base_sha=second
            )
    reference = raised.value.terminal_reference
    assert reference is not None
    return manager, reference


@pytest.mark.parametrize("phase", sorted(source_worktree._TRANSITION_PHASES))
def test_terminal_failure_retains_each_durable_phase(tmp_path: Path, phase: str) -> None:
    """Each incomplete phase preserves its cause and exact retry request."""
    manager, reference = _terminal_failed_transition(tmp_path, phase)
    view = manager.read_terminal_failure(9, reference)
    assert view.phase == phase
    assert view.outcome == "incomplete"
    assert view.cause == "source_workspace_transition_incomplete"
    assert phase in view.action and "writer-branch" in view.action
    assert view.reservation_disposition == "preserve"
    assert manager._transition_path(9).exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "phase",
        "journal_delete",
        "terminal_delete",
        "cause",
        "action",
        "identity",
        "source_delete",
        "source_json",
        "source_bytes",
        "source_link",
        "source_null",
        "duplicate",
        "unknown",
    ],
)
def test_terminal_failure_rejects_changed_evidence(tmp_path: Path, mutation: str) -> None:
    """Changed durable evidence invalidates the terminal reference."""
    manager, reference = _terminal_failed_transition(tmp_path)
    terminal = manager.state_dir / reference.identity
    journal = manager._transition_path(9)
    receipt = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    if mutation == "phase":
        payload = json.loads(journal.read_text())
        payload["phase"] = "successor_created"
        journal.write_text(json.dumps(payload))
    elif mutation.endswith("_delete"):
        {"journal_delete": journal, "terminal_delete": terminal, "source_delete": receipt}[
            mutation
        ].unlink()
    elif mutation == "source_json":
        payload = json.loads(receipt.read_text())
        payload["generation"] += 1
        receipt.write_text(json.dumps(payload))
    elif mutation == "source_bytes":
        receipt.write_bytes(receipt.read_bytes() + b"\n")
    elif mutation == "source_link":
        other = receipt.with_suffix(".copy")
        receipt.rename(other)
        receipt.symlink_to(other)
    elif mutation == "duplicate":
        terminal.write_text(terminal.read_text().replace("{", '{"schema_version":1,', 1))
    else:
        payload = json.loads(terminal.read_text())
        if mutation == "source_null":
            payload["source_receipt_sha256"] = None
            payload.pop("terminal_content_sha256")
            payload["terminal_content_sha256"] = source_worktree._terminal_json_digest(payload)
            reference = source_worktree.SourceWorkspaceTerminalReference(
                reference.identity, payload["terminal_content_sha256"]
            )
        elif mutation == "identity":
            payload["item_number"] = 10
        elif mutation == "unknown":
            payload["extra"] = True
        else:
            payload[mutation] = "changed"
        terminal.write_text(json.dumps(payload))
    with pytest.raises(SourceWorkspaceError):
        manager.read_terminal_failure(9, reference)


def test_terminal_failure_rejects_source_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor and path must still identify the same source file."""
    manager, reference = _terminal_failed_transition(tmp_path)
    receipt = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    original = os.read
    replaced = False

    def read_then_replace(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        value = original(descriptor, size)
        if not replaced and os.fstat(descriptor).st_ino == receipt.stat().st_ino:
            replacement = receipt.with_suffix(".replacement")
            replacement.write_bytes(receipt.read_bytes())
            replacement.replace(receipt)
            replaced = True
        return value

    monkeypatch.setattr(os, "read", read_then_replace)
    with pytest.raises(SourceWorkspaceError):
        manager.read_terminal_failure(9, reference)
    assert replaced


def test_terminal_snapshot_write_failure_preserves_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed snapshot cannot authorize cleanup or erase the journal."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    from hephaestus.io.utils import write_secure

    original = write_secure

    def corrupt_terminal(path: Path, content: str) -> None:
        original(path, "{}" if path.name.endswith("-terminal.json") else content)

    monkeypatch.setattr(source_worktree, "write_secure", corrupt_terminal)
    with pytest.raises(source_worktree.SourceWorkspaceTerminalError) as raised:
        with manager.implementation_writer_handoff(9) as handoff:
            manager.authorize_direct_implementation_writer_transition(
                9, branch="writer-branch", base_sha=second, handoff=handoff
            )
            raise source_worktree.SourceWorkspaceTerminalError(
                "terminal failure", requested_branch="writer-branch", requested_base_sha=second
            )
    assert raised.value.terminal_reference is None
    assert raised.value.preserve
    assert manager._transition_path(9).exists()
    assert manager._read_receipt(9, SourceLane.IMPLEMENTATION) is not None


@pytest.mark.parametrize(
    "branch", ["foreign-writer", "9-auto-impl", "9-auto-impl-direct-" + "a" * 32]
)
def test_terminal_legacy_mismatch_never_changes_writer(tmp_path: Path, branch: str) -> None:
    """Branch names and shared ancestry cannot replace missing ownership proof."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    receipt = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    before = receipt.read_bytes()
    _git(writer.cwd, "checkout", "-b", branch, second)
    (repo / "diagnostic-event.json").write_text('{"reservation_released":true}')
    refs = _git(repo, "show-ref")
    with pytest.raises(source_worktree.SourceWorkspaceTerminalError) as raised:
        with manager.implementation_writer_handoff(9) as handoff:
            try:
                manager.authorize_direct_implementation_writer_transition(
                    9, branch="next-writer", base_sha=second, handoff=handoff
                )
            except SourceWorkspaceError as exc:
                raise source_worktree.SourceWorkspaceTerminalError(
                    str(exc), requested_branch="next-writer", requested_base_sha=second
                ) from exc
    reference = raised.value.terminal_reference
    assert reference is not None
    view = manager.read_terminal_failure(9, reference)
    assert view.cause == "source_workspace_legacy_unproven"
    assert branch in view.action and first in view.action and second in view.action
    assert "ownership record" in view.action
    assert receipt.read_bytes() == before
    assert _git(repo, "show-ref") == refs
    assert _git(writer.cwd, "branch", "--show-current") == branch
    assert not manager._transition_path(9).exists()


@pytest.mark.parametrize("corruption", ["json", "generation", "coerced_type"])
def test_terminal_capture_classifies_invalid_source_evidence(
    tmp_path: Path, corruption: str
) -> None:
    """An invalid source receipt cannot become a valid incomplete result."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    with pytest.raises(source_worktree.SourceWorkspaceTerminalError) as raised:
        with manager.implementation_writer_handoff(9) as handoff:
            manager.authorize_direct_implementation_writer_transition(
                9, branch="writer-branch", base_sha=second, handoff=handoff
            )
            receipt = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
            if corruption == "json":
                receipt.write_text("invalid")
            else:
                payload = json.loads(receipt.read_text())
                if corruption == "coerced_type":
                    payload["generation"] = str(payload["generation"])
                else:
                    payload["generation"] += 42
                receipt.write_text(json.dumps(payload))
            raise source_worktree.SourceWorkspaceTerminalError(
                "terminal failure", requested_branch="writer-branch", requested_base_sha=second
            )
    reference = raised.value.terminal_reference
    assert reference is not None
    assert (
        manager.read_terminal_failure(9, reference).cause
        == "source_workspace_recovery_receipt_invalid"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("item_number", True),
        ("repository", "other/project"),
        ("ownership_key", "foreign"),
        ("path", "/tmp/../tmp/foreign"),
        ("requested_branch", "../bad"),
        ("requested_base_sha", None),
        ("phase", []),
        ("cause", "unknown"),
        ("outcome", "complete"),
        ("action", ""),
        ("reservation_disposition", "release"),
        ("transition_identity", "10-impl-transition.json"),
        ("transition_journal_digest", "a" * 64),
        ("source_receipt_sha256", "a" * 64),
    ],
)
def test_terminal_schema_rejects_invalid_fields_with_matching_digest(
    tmp_path: Path, field: str, value: object
) -> None:
    """A matching content digest cannot replace the closed field contract."""
    manager, reference = _terminal_failed_transition(tmp_path)
    path = manager.state_dir / reference.identity
    payload = json.loads(path.read_text())
    payload[field] = value
    payload.pop("terminal_content_sha256")
    digest = source_worktree._terminal_json_digest(payload)
    payload["terminal_content_sha256"] = digest
    path.write_text(json.dumps(payload))
    reference = source_worktree.SourceWorkspaceTerminalReference(reference.identity, digest)
    with pytest.raises(SourceWorkspaceError):
        manager.read_terminal_failure(9, reference)


def test_terminal_source_unreadable_preserves_existing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source read error invalidates the result without changing the file."""
    manager, reference = _terminal_failed_transition(tmp_path)
    receipt = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    before = receipt.read_bytes()
    original = os.open

    def deny_receipt(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == receipt:
            raise PermissionError("source receipt read denied")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_receipt)
    with pytest.raises(SourceWorkspaceError):
        manager.read_terminal_failure(9, reference)
    assert receipt.read_bytes() == before


def test_terminal_context_rejects_and_preserves_broken_journal_link(tmp_path: Path) -> None:
    """An invalid journal link still requires terminal state preservation."""
    repo, first, _ = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    journal = manager._transition_path(9)
    journal.symlink_to(manager.state_dir / "absent.json")
    with pytest.raises(source_worktree.SourceWorkspaceTerminalError) as raised:
        with manager.implementation_writer_handoff(9):
            pytest.fail("An invalid journal must stop the handoff.")
    assert journal.is_symlink()
    reference = raised.value.terminal_reference
    assert reference is not None
    assert (
        manager.read_terminal_failure(9, reference).cause
        == "source_workspace_recovery_receipt_invalid"
    )


def test_terminal_failure_does_not_restore_removed_predecessor(tmp_path: Path) -> None:
    """A failed creation preserves the absent path and last durable phase."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    receipt = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    source_bytes = receipt.read_bytes()
    refs = _git(repo, "show-ref")
    with pytest.raises(source_worktree.SourceWorkspaceTerminalError) as raised:
        with manager.implementation_writer_handoff(9) as handoff:
            manager.authorize_direct_implementation_writer_transition(
                9, branch="writer-branch", base_sha=second, handoff=handoff
            )
            journal = manager._read_writer_transition(9)
            assert journal is not None
            _git(repo, "worktree", "remove", str(predecessor.cwd))
            manager._update_writer_transition_phase(9, journal.journal_digest, "successor_creating")
            raise source_worktree.SourceWorkspaceTerminalError(
                "writer creation failed",
                requested_branch="writer-branch",
                requested_base_sha=second,
            )
    assert not predecessor.cwd.exists()
    assert receipt.read_bytes() == source_bytes
    assert _git(repo, "show-ref") == refs
    reference = raised.value.terminal_reference
    assert reference is not None
    restarted = SourceWorkspaceManager(repo, repository="example/project")
    view = restarted.read_terminal_failure(9, reference)
    assert view.phase == "successor_creating"
    assert view.cause == "source_workspace_transition_incomplete"
    assert not predecessor.cwd.exists()


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_NONBLOCK"])
def test_terminal_missing_secure_read_capability_preserves_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """Unsupported secure reads produce explicit preservation without a snapshot claim."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    with pytest.raises(source_worktree.SourceWorkspaceTerminalError) as raised:
        with manager.implementation_writer_handoff(9) as handoff:
            manager.authorize_direct_implementation_writer_transition(
                9, branch="writer-branch", base_sha=second, handoff=handoff
            )
            monkeypatch.delattr(os, flag)
            raise source_worktree.SourceWorkspaceTerminalError(
                "writer creation failed",
                requested_branch="writer-branch",
                requested_base_sha=second,
            )
    assert raised.value.preserve
    assert raised.value.terminal_reference is None
    assert manager._transition_path(9).exists()


def test_prepared_same_identity_recovery_preserves_registered_predecessor(tmp_path: Path) -> None:
    """Recovery retains an exact predecessor, including its ignored content."""
    repo, first, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    for revision in (first, head, first, head):
        writer = manager.prepare(9, SourceLane.IMPLEMENTATION, revision, branch="adopted-writer")
    assert writer.generation == 4
    exclude = repo / _git(repo, "rev-parse", "--git-path", "info/exclude")
    with exclude.open("a") as stream:
        stream.write("\npreserved-sentinel\n")
    sentinel = writer.cwd / "preserved-sentinel"
    sentinel.write_bytes(b"ignored content must survive recovery\x00\n")
    sentinel.chmod(0o640)
    assert _git(writer.cwd, "status", "--porcelain") == ""
    assert _git(writer.cwd, "check-ignore", "preserved-sentinel") == "preserved-sentinel"
    with pytest.raises(source_worktree.SourceWorkspaceTerminalError):
        with manager.implementation_writer_handoff(9) as handoff:
            manager.authorize_adopted_implementation_writer_transition(
                9, branch="adopted-writer", expected_head=head, handoff=handoff
            )
            raise source_worktree.SourceWorkspaceTerminalError("test stop before adoption")
    journal = manager._read_writer_transition(9)
    assert journal is not None and journal.phase == "prepared"
    assert journal.predecessor.generation == 4
    assert journal.successor.generation == 5
    assert journal.predecessor.revision == journal.successor.revision == head
    assert journal.predecessor.branch == journal.successor.branch == "adopted-writer"
    receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    receipt_before = receipt_path.read_bytes()
    registration_before = _git(repo, "worktree", "list", "--porcelain")
    paths = (writer.cwd, writer.cwd / ".git", sentinel)
    index_before = _git(writer.cwd, "ls-files", "--stage")
    identities = [(path.stat().st_dev, path.stat().st_ino, path.stat().st_mode) for path in paths]
    contents = [path.read_bytes() for path in paths[1:]]
    restarted = SourceWorkspaceManager(repo, repository="example/project")
    with patch.object(source_worktree, "_git", wraps=_source_worktree_git) as commands:
        with restarted.implementation_writer_handoff(9):
            pass
    mutations = [
        call.args
        for call in commands.call_args_list
        if len(call.args) >= 3 and call.args[1:3] in (("worktree", "remove"), ("worktree", "add"))
    ]
    assert mutations == []
    assert [
        (path.stat().st_dev, path.stat().st_ino, path.stat().st_mode) for path in paths
    ] == identities
    assert [path.read_bytes() for path in paths[1:]] == contents
    assert _git(writer.cwd, "ls-files", "--stage") == index_before
    assert receipt_path.read_bytes() == receipt_before
    assert restarted._read_receipt(9, SourceLane.IMPLEMENTATION) == journal.predecessor
    assert not restarted._transition_path(9).exists()
    assert _git(repo, "worktree", "list", "--porcelain") == registration_before
    assert _git(writer.cwd, "rev-parse", "HEAD") == head
    assert _git(writer.cwd, "symbolic-ref", "--short", "HEAD") == "adopted-writer"
    assert _git(writer.cwd, "status", "--porcelain") == ""
