"""Test publication recovery after an owned worker process stops."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import signal
import subprocess
import sys
import threading
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_utils
from hephaestus.automation.first_publication_recovery import FirstPublicationStore
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.host_capabilities import WorkerCapabilities
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.config.child_environments import build_git_child_env
from tests.unit.automation.pipeline.conftest import FakeSigningProvider
from tests.unit.automation.pipeline.test_worker_pool import _git, _worker_repository
from tests.unit.automation.test_rebase_recovery import (
    _registered_git_fixture,
    _source_registration_fixture,
)

pytestmark = [
    pytest.mark.requires_posix,
    pytest.mark.skipif(os.name != "posix", reason="Source ownership requires POSIX locks."),
]


def _pool(root: Path, patch: pytest.MonkeyPatch) -> WorkerPool:
    """Use real local Git transport and an explicit signing provider."""
    _registered_git_fixture(root, patch)
    _source_registration_fixture(root, patch)
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=root.parent / "locks",
        host_capabilities=WorkerCapabilities(None, "process-test", FakeSigningProvider()),
    )
    patch.setattr(
        pool,
        "_authenticated_remote_git_configuration",
        lambda **kwargs: (build_git_child_env(), ("-c", "protocol.file.allow=always")),
    )
    return pool


def _publication_job(tmp_path: Path, patch: pytest.MonkeyPatch) -> GitJob:
    """Prepare one signed source commit without publication intent."""
    root, _, base = _worker_repository(tmp_path)
    pool = _pool(root, patch)
    manager = SourceWorkspaceManager(root, repository="example/project")
    try:
        created = pool._run_git(
            GitJob(
                repo="example/project",
                op="create_worktree",
                timeout_s=60,
                kwargs={
                    "repo_root": str(root),
                    "issue_number": 9,
                    "branch_name": "writer",
                    "source_lane": "impl",
                },
            )
        )
        assert created.ok, created.error
        receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        key = tmp_path / "signing-key"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
            capture_output=True,
        )
        with manager.implementation_local_commit(
            9, branch="writer", path=receipt.path, expected_binding=manager._binding(receipt)
        ) as record:
            (receipt.path / "local.txt").write_text("Local change.\n", encoding="utf-8")
            _git(receipt.path, "add", "local.txt")
            _git(
                receipt.path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "gpg.format=ssh",
                "-c",
                f"user.signingkey={key}",
                "commit",
                "-S",
                "-s",
                "-m",
                "fix: prepare first publication",
            )
            binding = record(_git(receipt.path, "rev-parse", "HEAD"))
        return GitJob(
            repo="example/project",
            op="commit_push",
            workspace=binding,
            timeout_s=60,
            kwargs={
                "repo_root": str(root),
                "issue_number": 9,
                "source_lane": "impl",
                "worktree_path": str(binding.cwd),
                "branch": "writer",
                "agent": "codex",
                "allowed_paths": ("local.txt",),
                "scope_history_base_sha": base,
                "publish_base_sha": base,
            },
        )
    finally:
        pool.shutdown()


def _observe_contention(patch: pytest.MonkeyPatch, channel: Any) -> None:
    """Report a real failed lock attempt without changing lock behavior."""
    import fcntl

    flock = fcntl.flock
    contended = False

    def observe_lock(descriptor: int, operation: int) -> Any:
        nonlocal contended
        try:
            return flock(descriptor, operation)
        except BlockingIOError:
            if not contended:
                channel.send(("contended", os.getpid()))
                contended = True
            raise

    patch.setattr(fcntl, "flock", observe_lock)


def _install_checkpoints(
    root: Path, patch: pytest.MonkeyPatch, boundary: str | None, channel: Any, role: str
) -> None:
    """Observe real storage and transport effects before each selected stop."""
    write = FirstPublicationStore.write
    run = git_utils.run
    held = False

    def checkpoint() -> None:
        channel.send(("checkpoint", os.getpid()))
        if not channel.poll(30):
            raise AssertionError("The parent did not stop its worker.")
        raise AssertionError("The stopped worker must not resume.")

    def write_record(store: Any, record: Any, *, expected: Any) -> Any:
        if boundary == "before_intent" and record.phase == "publication_intent":
            assert store.read(9, record.operation_id) is None
            checkpoint()
        retained = write(store, record, expected=expected)
        assert store.read(9, record.operation_id) == retained
        with (root.parent / "record-writes").open("a", encoding="ascii") as output:
            output.write(record.phase + "\n")
        if (boundary, record.phase) in {
            ("after_intent", "publication_intent"),
            ("after_complete", "complete"),
        }:
            checkpoint()
        return retained

    def transport(argv: Any, **kwargs: Any) -> Any:
        nonlocal held
        if role == "holder" and "fetch" in argv and not held:
            held = True
            channel.send(("held", os.getpid()))
            assert channel.poll(30), "The parent did not release the lock owner."
            assert channel.recv() == "release"
        if "push" in argv:
            with (root.parent / "push-attempts").open("a", encoding="ascii") as output:
                output.write("attempted\n")
        result = run(argv, **kwargs)
        if "push" in argv:
            assert result.returncode == 0
            with (root.parent / "pushes").open("a", encoding="ascii") as output:
                output.write("published\n")
            if boundary == "after_push":
                checkpoint()
        return result

    patch.setattr(FirstPublicationStore, "write", write_record)
    patch.setattr(git_utils, "run", transport)


def _stop_before_publication_intent(job: GitJob) -> None:
    """Stop an owned worker at the observed pre-intent boundary."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=_process, args=(job, "before_intent", child))
    try:
        worker.start()
        child.close()
        assert parent.poll(30), "The worker did not reach the pre-intent checkpoint."
        assert parent.recv() == ("checkpoint", worker.pid)
        worker.kill()
        worker.join(timeout=5)
        assert worker.exitcode == -signal.SIGKILL
        root = Path(job.kwargs["repo_root"])
        manager = SourceWorkspaceManager(root, repository=job.repo)
        assert not list((manager.state_dir / "first-publications").glob("9-*.json"))
        assert not (root.parent / "push-attempts").exists()
        assert not (root.parent / "record-writes").exists()
    finally:
        if worker.pid is not None:
            if worker.is_alive():
                worker.kill()
            worker.join(timeout=5)
            assert not worker.is_alive()
            worker.close()
        parent.close()
        child.close()


def _process(job: GitJob, boundary: str | None, channel: Any, role: str = "single") -> None:
    """Run a real publication owner, with checkpoints at external boundaries."""
    mask = os.umask(0o022)
    root = Path(job.kwargs["repo_root"])
    pool: WorkerPool | None = None
    try:
        with pytest.MonkeyPatch.context() as patch:
            pool = _pool(root, patch)
            if role == "waiter":
                _observe_contention(patch, channel)
            _install_checkpoints(root, patch, boundary, channel, role)
            if boundary is None:
                discovery = pool._run_git(
                    GitJob(
                        repo=job.repo,
                        op="discover_first_publication",
                        timeout_s=60,
                        kwargs={
                            "repo_root": str(root),
                            "issue_number": 9,
                            "branch": "",
                            "publication_discovery_request_id": "d" * 32,
                        },
                    )
                )
                assert discovery.ok, discovery.error
                assert job.workspace is not None
                assert discovery.value["source_workspace"] == job.workspace.to_dict()
                candidate = discovery.value["first_publication_candidate"]
                assert candidate is not None
                validation = (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "assert Path('local.txt').read_text() == 'Local change.\\n'; "
                    f"p = Path({str(root.parent / 'validations')!r}); "
                    "f = p.open('a'); f.write('validated\\n'); f.close()",
                )
                job = replace(
                    job,
                    kwargs={
                        **job.kwargs,
                        "first_publication_candidate": candidate,
                        "publication_recovery_request_id": "e" * 32,
                        "publication_test_argv": validation,
                    },
                )

                def reject_mutation(*args: Any, **kwargs: Any) -> Any:
                    raise AssertionError("Recovery must not commit or rebase the retained source.")

                patch.setattr(git_utils, "commit_if_changes", reject_mutation)
                patch.setattr(git_utils, "rebase_worktree_onto", reject_mutation)
            result = pool._run_git(job)
            assert boundary is None, result
            assert result.ok, result
            assert job.workspace is not None
            assert result.value["head_sha"] == job.workspace.revision
            assert result.value["first_publication_validation"]["argv"] == validation
            channel.send(("recovered", os.getpid(), result.value))
    except BaseException as error:
        channel.send(("error", type(error).__name__, str(error), traceback.format_exc()[-8000:]))
        raise
    finally:
        if pool is not None:
            pool.shutdown()
        os.umask(mask)
        channel.close()


@pytest.mark.parametrize("boundary", ["after_intent", "after_push", "after_complete"])
@pytest.mark.parametrize("contenders", [1, 2])
def test_process_death_recovers_exact_publication_once(
    tmp_path: Path, boundary: str, contenders: int
) -> None:
    """Fresh processes preserve H and complete one publication after SIGKILL."""
    context = multiprocessing.get_context("spawn")
    processes: list[Any] = []
    channels: list[Any] = []
    mask = os.umask(0o022)
    try:
        with pytest.MonkeyPatch.context() as patch:
            job = _publication_job(tmp_path, patch)
        assert job.workspace is not None
        root = Path(job.kwargs["repo_root"])
        manager = SourceWorkspaceManager(root, repository=job.repo)
        receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        tree = _git(receipt.path, "rev-parse", "HEAD^{tree}")
        parent, child = context.Pipe()
        channels.extend((parent, child))
        worker = context.Process(target=_process, args=(job, boundary, child))
        processes.append(worker)
        worker.start()
        child.close()
        assert parent.poll(30), "The worker did not reach its checkpoint."
        assert parent.recv() == ("checkpoint", worker.pid)
        worker.kill()
        worker.join(timeout=5)
        assert worker.exitcode == -signal.SIGKILL
        records = list((manager.state_dir / "first-publications").glob("9-*.json"))
        assert len(records) == 1
        content = records[0].read_bytes()
        identity = records[0].stat()
        assert json.loads(content)["phase"] == (
            "complete" if boundary == "after_complete" else "publication_intent"
        )
        readers = []
        for index in range(contenders):
            parent, child = context.Pipe()
            channels.extend((parent, child))
            role = ("holder" if index == 0 else "waiter") if contenders == 2 else "single"
            process = context.Process(target=_process, args=(job, None, child, role))
            processes.append(process)
            readers.append((parent, process))
            process.start()
            child.close()
            if contenders == 2:
                assert parent.poll(30), "The contender did not reach its lock checkpoint."
                assert parent.recv() == ("held" if index == 0 else "contended", process.pid)
                if index == 1:
                    readers[0][0].send("release")
        for parent, process in readers:
            assert parent.poll(30), "The fresh worker did not finish within its budget."
            result = parent.recv()
            assert result[:2] == ("recovered", process.pid), result
            assert process.pid not in {os.getpid(), worker.pid}
            process.join(timeout=5)
            assert process.exitcode == 0
        assert (tmp_path / "pushes").read_text().splitlines() == ["published"]
        assert (tmp_path / "push-attempts").read_text().splitlines() == ["attempted"]
        assert (tmp_path / "record-writes").read_text().splitlines() == [
            "publication_intent",
            "complete",
        ]
        assert (tmp_path / "validations").read_text().splitlines() == ["validated"] * contenders
        assert json.loads(records[0].read_bytes())["phase"] == "complete"
        if boundary == "after_complete":
            assert records[0].read_bytes() == content
            assert records[0].stat().st_ino == identity.st_ino
            assert records[0].stat().st_mtime_ns == identity.st_mtime_ns
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
        assert _git(receipt.path, "rev-parse", "HEAD") == job.workspace.revision
        assert _git(receipt.path, "rev-parse", "HEAD^{tree}") == tree
        assert _git(receipt.path, "status", "--porcelain") == ""
        assert _git(root, "ls-remote", "origin", "refs/heads/writer") == (
            f"{job.workspace.revision}\trefs/heads/writer"
        )
    finally:
        for process in processes:
            if process.pid is not None:
                if process.is_alive():
                    process.kill()
                process.join(timeout=5)
                assert not process.is_alive()
                process.close()
        for channel in channels:
            channel.close()
        os.umask(mask)
