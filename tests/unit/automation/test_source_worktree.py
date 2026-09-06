"""Lifecycle tests for deterministic source-reading worktrees."""

from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import implementation_writer, source_worktree
from hephaestus.automation.pipeline.git_cleanup import run_cleanup_job
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspaceManager,
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
    _git as _source_worktree_git,
    _PreparationDeadline,
)
from hephaestus.automation.worktree_manager import (
    ImplementationWriterAuthority,
    WorktreeCreationReceiptError,
    WorktreeManager,
)
from hephaestus.utils.file_lock import LockUnavailableError, file_lock


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
            try:
                state = status_path.read_text().split()[2]
            except FileNotFoundError:
                break
            if state in {"X", "Z"}:
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
                try:
                    if status_path.read_text().split()[2] in {"X", "Z"}:
                        break
                except FileNotFoundError:
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
                try:
                    if status_path.read_text().split()[2] in {"X", "Z"}:
                        break
                except FileNotFoundError:
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


@pytest.mark.parametrize("mutation", ["dirty", "attached", "revision-drift"])
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
    else:
        _git(predecessor.cwd, "reset", "--hard", second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="predecessor is invalid"):
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    assert predecessor.cwd.exists()
    assert manager._read_receipt(9, SourceLane.IMPLEMENTATION) is not None


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
    def observed_handoff(manager: SourceWorkspaceManager, item_number: int):
        try:
            with file_lock(
                manager._lane_lock_path(item_number, SourceLane.IMPLEMENTATION),
                blocking=False,
                require_exclusive=True,
            ):
                raise AssertionError("source lease was not held")
        except LockUnavailableError:
            contention_observed.set()
        with real_handoff(manager, item_number) as handoff:
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
        with pytest.raises(SourceWorkspaceError, match="owned by another repository"):
            second_manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert _git(writer, "branch", "--show-current") == "writer-branch"


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
