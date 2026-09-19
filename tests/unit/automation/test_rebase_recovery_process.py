"""Check durable rebase admission after an owned worker process stops."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import signal
import subprocess
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_utils, rebase_recovery
from hephaestus.automation.direct_review_recovery import _write_receipt
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.host_capabilities import (
    QUOTA_AVAILABLE_TOKEN,
    HostCapabilityReceipt,
    WorkerCapabilities,
)
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
from hephaestus.automation.pipeline.stages import JobRequest
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.rebase_recovery import PendingRebaseStore
from hephaestus.automation.source_worktree import SourceWorkspaceManager, _PreparationDeadline
from tests.unit.automation.test_rebase_recovery import (
    _abort_worker_case,
    _normal_publication_job,
    _publication_validation_seams,
    _registered_git_fixture,
    _restart_publication_stage,
    _store,
)
from tests.unit.automation.test_source_worktree import _git

pytestmark = [
    pytest.mark.requires_posix,
    pytest.mark.skipif(os.name != "posix", reason="Source ownership requires POSIX locks."),
]

_PUBLICATION_BOUNDARIES = {"before_push", "after_push"}
_RECOVERABLE_BOUNDARIES = {"after_pending", *_PUBLICATION_BOUNDARIES}
_MUTATED_BOUNDARIES = {"after_mutation", "after_receipt", *_RECOVERABLE_BOUNDARIES}


def _publication_checkpoint(
    root: Path, case: Any, boundary: str, patch: pytest.MonkeyPatch, stopped: Callable[[], Any]
) -> None:
    """Stop after durable intent or after the exact-lease external write."""
    write_phase = case.pool._write_rebase_publication_phase

    def phase(recovery: Any, name: Any) -> Any:
        retained = write_phase(recovery, name)
        assert not isinstance(retained, JobResult), retained
        assert retained.phase == "publication_intent"
        assert retained.request == case.job.capability_target
        assert retained.remote_head_sha == case.original
        assert _store(case.manager.common_dir).read(1, retained.request.request_id) == retained
        if boundary == "before_push":
            return stopped()
        return retained

    def publish(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
        assert boundary == "after_push"
        assert branch == case.job.kwargs["branch"] and cwd == case.binding.cwd
        assert expected == case.original
        assert (root / "remote-head").read_text(encoding="ascii") == expected
        retained = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert retained.phase == "publication_intent"
        assert retained.request == case.job.capability_target
        assert retained.remote_head_sha == expected
        head = _git(cwd, "rev-parse", "HEAD")
        assert kwargs["source_sha"] == head == retained.resulting_workspace.revision
        assert retained.resulting_tree_sha == _git(cwd, "rev-parse", "HEAD^{tree}")
        with (root / "pushes").open("a", encoding="ascii") as output:
            output.write(head + "\n")
        (root / "remote-head").write_text(head, encoding="ascii")
        stopped()

    patch.setattr(case.pool, "_write_rebase_publication_phase", phase)
    patch.setattr(git_utils, "push_head_to_branch", publish)


def _reject_external_write(root: Path, name: str) -> Callable[..., Any]:
    """Record an unexpected external call before rejecting it."""

    def reject(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        with (root / name).open("a", encoding="utf-8") as output:
            output.write("called\n")
        raise AssertionError(f"The stopped operation must not call {name}.")

    return reject


def _intent_checkpoint(
    root: Path, case: Any, boundary: str, patch: pytest.MonkeyPatch, stopped: Callable[[], Any]
) -> None:
    """Keep intent checkpoints separate from mutation checkpoints."""
    patch.setattr(git_utils, "rebase_worktree_onto", _reject_external_write(root, "mutations"))
    write = PendingRebaseStore.write

    def checkpoint(store: Any, record: Any, *, expected: Any) -> Any:
        assert expected is None and record.phase == "intent"
        assert record.request == case.job.capability_target
        assert record.publication_mode == "existing"
        assert record.remote_head_sha == case.original
        if boundary == "after_intent":
            assert write(store, record, expected=expected) == record
        return stopped()

    patch.setattr(PendingRebaseStore, "write", checkpoint)


def _mutation_checkpoint(
    root: Path,
    case: Any,
    operation: str,
    boundary: str,
    patch: pytest.MonkeyPatch,
    stopped: Callable[[], Any],
) -> None:
    """Observe real mutation and source retention without replacing admission."""
    mutation = (
        case.pool._continue_rebase_process
        if operation == "continued"
        else git_utils.rebase_worktree_onto
    )

    def mutate(*args: Any, **kwargs: Any) -> Any:
        result = mutation(*args, **kwargs)
        assert result is None if operation == "continued" else result is True
        head = _git(case.binding.cwd, "rev-parse", "HEAD")
        assert head != case.original
        with (root / "mutations").open("a", encoding="ascii") as output:
            output.write(head + "\n")
        if boundary == "after_mutation":
            stopped()
        return result

    if operation == "continued":
        patch.setattr(case.pool, "_continue_rebase_process", mutate)
    else:
        patch.setattr(git_utils, "rebase_worktree_onto", mutate)
    record_result = case.pool._record_rebase_result

    def record_pending(*args: Any) -> Any:
        if boundary == "after_receipt":
            return stopped()
        retained = record_result(*args)
        assert not isinstance(retained, JobResult), retained
        assert retained.phase == "pending_validation"
        assert retained.request == case.job.capability_target
        assert retained.resulting_workspace.revision == _git(case.binding.cwd, "rev-parse", "HEAD")
        if boundary == "after_pending":
            return stopped()
        return retained

    patch.setattr(case.pool, "_record_rebase_result", record_pending)
    if boundary in _PUBLICATION_BOUNDARIES:
        _publication_checkpoint(root, case, boundary, patch, stopped)


def _abort_checkpoint(
    root: Path, case: Any, boundary: str, patch: pytest.MonkeyPatch, stopped: Callable[[], Any]
) -> None:
    """Stop only after the real abort owner has verified restored source."""
    write = PendingRebaseStore.write
    write_receipt = _write_receipt

    def before(store: Any, record: Any, *, expected: Any) -> Any:
        if record.phase == "aborted":
            assert boundary == "before_abort_record"
            assert record.request == case.job.capability_target
            assert case.starts == [True] and case.aborts == [1]
            (root / "aborts").write_text("1\n", encoding="ascii")
            return stopped()
        return write(store, record, expected=expected)

    def after(*args: Any, **kwargs: Any) -> Any:
        result = write_receipt(*args, **kwargs)
        if json.loads(args[-1])["phase"] == "aborted":
            assert case.starts == [True] and case.aborts == [1]
            (root / "aborts").write_text("1\n", encoding="ascii")
            stopped()
        return result

    if boundary == "before_abort_record":
        patch.setattr(PendingRebaseStore, "write", before)
    else:
        patch.setattr(rebase_recovery, "_write_receipt", after)


def _stop_at_intent(root: Path, boundary: str, operation: str, channel: Any) -> None:
    """Stop the real admitted worker at one confirmed durable boundary."""
    try:
        with pytest.MonkeyPatch.context() as patch:
            with _abort_worker_case(
                root,
                patch,
                fallback=False,
                fault="clean",
                conflict=operation != "initial",
                structural=True,
            ) as case:
                (root / "remote-head").write_text(case.original, encoding="ascii")
                patch.setattr(
                    git_utils, "push_head_to_branch", _reject_external_write(root, "pushes")
                )
                submitted_job = case.job
                if operation == "continued":
                    _publication_validation_seams(case, "continued", patch)
                    submitted_job = _normal_publication_job(case, "continued")
                    assert case.starts == [True]
                    assert submitted_job.capability_target != case.job.capability_target
                    assert submitted_job.kwargs["rebase_recovery_intent_id"] == (
                        case.job.capability_target.request_id
                    )
                    (root / "conflicts").write_text("1\n", encoding="ascii")
                elif operation == "initial" and boundary in _MUTATED_BOUNDARIES:
                    _publication_validation_seams(case, "initial", patch)

                def stopped() -> Any:
                    if operation == "abort":
                        assert case.starts == [True] and case.aborts == [1]
                        (root / "conflicts").write_text("1\n", encoding="ascii")
                    channel.send(
                        ("checkpoint", os.getpid(), case.job, submitted_job.workspace, case.tree)
                    )
                    if not channel.poll(30.0):
                        raise AssertionError("The parent did not stop the worker.")
                    raise AssertionError("The stopped worker must not resume.")

                if operation == "abort":
                    _abort_checkpoint(root, case, boundary, patch, stopped)
                elif boundary in {"before_intent", "after_intent"}:
                    _intent_checkpoint(root, case, boundary, patch, stopped)
                else:
                    _mutation_checkpoint(root, case, operation, boundary, patch, stopped)
                result = case.pool._run_git(submitted_job)
                raise AssertionError(f"The worker missed the checkpoint: {result}")
    except BaseException as error:
        channel.send(
            ("error", type(error).__name__, str(error), traceback.format_exc(limit=8)[-8000:])
        )
        raise
    finally:
        channel.close()


def _resume_at_b(
    root: Path,
    boundary: str,
    job: Any,
    manager: Any,
    binding: Any,
    stage: Any,
    item: Any,
    ctx: Any,
    discovery: JobResult,
    patch: pytest.MonkeyPatch,
) -> None:
    """Use a stage-owned candidate and new execution providers at retained B."""
    stage.on_job_done(item, discovery, ctx)
    submitted = stage.step(item, ctx)
    assert isinstance(submitted, JobRequest) and isinstance(submitted.job, GitJob)
    resume = submitted.job
    assert resume.rebase_recovery_candidate == job.capability_target.request_id
    assert resume.workspace == binding
    assert resume.capability_target is not None
    assert resume.capability_target.expected_head_sha == binding.revision
    seen: list[str] = []

    def available(target: Any, *, deadline: Any) -> HostCapabilityReceipt:
        assert deadline.remaining() > 0
        assert target.request == resume.capability_target
        assert target.source_head_sha == binding.revision
        seen.append("quota")
        return HostCapabilityReceipt(
            True,
            QUOTA_AVAILABLE_TOKEN,
            None,
            "scratch",
            "d" * 32,
            target=target,
            cleanup_state="complete",
        )

    def semantic(path: Path) -> None:
        assert path == binding.cwd
        assert _git(path, "rev-parse", "HEAD") == binding.revision
        seen.append("semantic")

    def execute(request: Any) -> JobResult:
        assert request.expected_head_sha == binding.revision
        assert request.immutable_source is True
        seen.append("structural")
        return JobResult(ok=True, value={"head_sha": binding.revision, "immutable_source": True})

    signing = Mock()
    signing.environment.return_value = {"GIT_CONFIG_GLOBAL": os.devnull}
    backend = Mock(backend_id="hdiutil-v1", preflight=Mock(side_effect=available))
    policy = RebaseValidationPolicy("hephaestus-adr-v1", semantic, ("test.py",))
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=root / "locks",
        host_capabilities=WorkerCapabilities(backend, "fresh-process", signing),
        rebase_policy_selector=lambda repo: policy,
    )
    prior = _store(manager.common_dir).read(1, resume.rebase_recovery_candidate)
    assert prior is not None
    assert prior.phase == (
        "publication_intent" if boundary in _PUBLICATION_BOUNDARIES else "pending_validation"
    )
    assert prior.request == job.capability_target
    assert prior.remote_head_sha == job.capability_target.expected_head_sha
    initial_remote = (root / "remote-head").read_text(encoding="ascii")
    assert initial_remote == (
        binding.revision if boundary == "after_push" else prior.remote_head_sha
    )
    if boundary == "after_push":
        assert (root / "pushes").read_text(encoding="ascii").splitlines() == [binding.revision]
    else:
        assert not (root / "pushes").exists()
    run = git_utils.run

    def metadata(argv: list[str], **kwargs: Any) -> Any:
        result = run(argv, **kwargs)
        if argv[:3] == ["git", "cat-file", "-p"]:
            assert argv[3] == binding.revision
            seen.append("metadata")
            return subprocess.CompletedProcess(
                argv,
                result.returncode,
                result.stdout + "\ngpgsig controlled-test-signature\n",
                result.stderr,
            )
        return result

    def remote(cwd: Path, **kwargs: Any) -> str:
        assert cwd == binding.cwd and kwargs["expected_repo"] == job.transport_repository
        assert kwargs["remote"] == "origin" and kwargs["branch"] == job.kwargs["branch"]
        return (root / "remote-head").read_text(encoding="ascii")

    def publish(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
        assert boundary != "after_push", "A confirmed remote B must not be published again."
        assert branch == job.kwargs["branch"] and cwd == binding.cwd
        assert expected == job.capability_target.expected_head_sha
        assert (root / "remote-head").read_text(encoding="ascii") == expected
        assert kwargs["source_sha"] == binding.revision
        retained = _store(manager.common_dir).read(1, resume.rebase_recovery_candidate)
        assert retained.phase == "publication_intent"
        assert retained.remote_head_sha == expected
        with (root / "pushes").open("a", encoding="ascii") as output:
            output.write(binding.revision + "\n")
        (root / "remote-head").write_text(binding.revision, encoding="ascii")

    patch.setattr(git_utils, "run", metadata)
    patch.setattr(git_utils, "push_head_to_branch", publish)
    patch.setattr(pool, "_run_immutable_build_test", execute)
    patch.setattr(pool, "_continue_rebase_process", _reject_external_write(root, "mutations"))
    patch.setattr(pool, "_read_remote_branch_head", remote)
    patch.setattr(
        pool,
        "_git_fetch_main",
        lambda job: JobResult(ok=True, value={"head_sha": prior.target_base_sha}),
    )
    try:
        result = pool._run_git(resume)
    finally:
        pool.shutdown()
    assert result.ok, result
    assert seen == ["quota", "structural", "semantic", "metadata"]
    signing.environment.assert_called_once()
    completed = _store(manager.common_dir).read(1, resume.rebase_recovery_candidate)
    assert completed.phase == "complete" and completed.resulting_workspace == binding
    assert completed.request == job.capability_target
    assert completed.remote_head_sha == job.capability_target.expected_head_sha
    assert completed.resulting_tree_sha == prior.resulting_tree_sha
    assert completed.resulting_tree_sha == _git(binding.cwd, "rev-parse", "HEAD^{tree}")
    assert completed.target_base_sha == prior.target_base_sha
    assert completed.publication_mode == "existing"
    assert result.value["head_sha"] == binding.revision
    assert "retained_rebase_review_proof" not in result.value
    assert "review_audit" not in result.value


def _assert_durable_source(
    root: Path,
    boundary: str,
    job: Any,
    source_input: Any,
    tree: str,
    manager: SourceWorkspaceManager,
) -> tuple[Any, str]:
    """Check the physical source and retained record without repairing either."""
    binding = job.workspace
    target = job.capability_target
    assert binding is not None and target is not None
    receipt = manager._require_receipt(1, SourceLane.IMPLEMENTATION)
    current_binding = receipt.to_binding(manager.repo_root)
    changed = boundary in _MUTATED_BOUNDARIES
    head = _git(binding.cwd, "rev-parse", "HEAD")
    if changed:
        assert head != target.expected_head_sha
        assert (root / "mutations").read_text(encoding="ascii").splitlines() == [head]
    else:
        assert head == target.expected_head_sha
        assert _git(binding.cwd, "rev-parse", "HEAD^{tree}") == tree
    if boundary in {"after_receipt", *_RECOVERABLE_BOUNDARIES}:
        assert current_binding.revision == head
        assert current_binding != binding
    else:
        assert current_binding == source_input
    deadline = _PreparationDeadline(time.monotonic() + 10, time.monotonic, threading.Event())
    if boundary != "after_mutation":
        with manager.acquire(current_binding, deadline=deadline):
            assert _git(binding.cwd, "rev-parse", "HEAD") == head
            assert _git(binding.cwd, "status", "--porcelain") == ""
    record = _store(manager.common_dir).read(1, target.request_id)
    if boundary == "before_intent":
        assert record is None
    else:
        assert record is not None
        phases = {
            "after_pending": "pending_validation",
            "before_push": "publication_intent",
            "after_push": "publication_intent",
            "after_abort_record": "aborted",
        }
        assert record.phase == phases.get(boundary, "intent")
        assert record.request == target
        assert record.remote_head_sha == target.expected_head_sha
        if boundary in _RECOVERABLE_BOUNDARIES:
            assert record.resulting_workspace == current_binding
            assert record.resulting_tree_sha == _git(binding.cwd, "rev-parse", "HEAD^{tree}")
        else:
            assert record.resulting_workspace is None
            assert record.resulting_tree_sha is None
        if boundary == "after_abort_record":
            assert record.schema_version == 2
            assert record.restored_workspace == binding
            assert record.restored_tree_sha == tree
    if boundary in {"before_abort_record", "after_abort_record"}:
        assert current_binding == binding
        assert receipt.branch == job.kwargs["branch"]
        assert (root / "aborts").read_text(encoding="ascii") == "1\n"
        for name in ("rebase-merge", "rebase-apply"):
            metadata = Path(_git(binding.cwd, "rev-parse", "--git-path", name))
            assert not (binding.cwd / metadata).exists()
    return current_binding, head


def _inspect_after_death(
    root: Path,
    boundary: str,
    operation: str,
    job: Any,
    source_input: Any,
    tree: str,
    channel: Any,
) -> None:
    """Read durable source and run discovery with new process-owned objects."""
    try:
        with pytest.MonkeyPatch.context() as patch:
            binding = job.workspace
            target = job.capability_target
            assert binding is not None and target is not None
            _registered_git_fixture(target.repository_root, patch)
            patch.setattr(
                worker_pool, "_trusted_gh_executable", lambda extra_path_root=None: "/usr/bin/gh"
            )
            patch.setattr(
                git_utils, "rebase_worktree_onto", _reject_external_write(root, "mutations")
            )
            patch.setattr(git_utils, "push_head_to_branch", _reject_external_write(root, "pushes"))
            manager = SourceWorkspaceManager(target.repository_root, repository=target.repository)
            current_binding, head = _assert_durable_source(
                root, boundary, job, source_input, tree, manager
            )
            if operation in {"continued", "abort"}:
                assert (root / "conflicts").read_text(encoding="ascii") == "1\n"
            changed = boundary in _MUTATED_BOUNDARIES
            pool = WorkerPool(
                size=1,
                shutdown=threading.Event(),
                completion_q=queue.Queue(),
                lock_dir=root / "locks",
            )

            def read_remote(cwd: Path, **kwargs: Any) -> str:
                assert cwd == binding.cwd
                assert kwargs["remote"] == "origin"
                assert kwargs["branch"] == job.kwargs["branch"]
                assert kwargs["expected_repo"] == target.repository
                return (root / "remote-head").read_text(encoding="ascii")

            patch.setattr(pool, "_read_remote_branch_head", read_remote)
            patch.setattr(
                pool, "_continue_rebase_process", _reject_external_write(root, "mutations")
            )
            try:
                case = SimpleNamespace(job=job, binding=binding)
                stage, item, ctx = _restart_publication_stage(
                    case,
                    "continued" if operation == "continued" else "initial",
                    (root / "remote-head").read_text(encoding="ascii"),
                )
                submitted = stage.step(item, ctx)
                assert isinstance(submitted, JobRequest)
                assert isinstance(submitted.job, GitJob)
                assert submitted.job.op == "discover_pending_rebase"
                result = pool._run_git(submitted.job)
                if boundary in {"before_intent", "after_abort_record"}:
                    assert result.ok, result
                    assert result.value["rebase_recovery_candidate"] is None
                elif boundary in _RECOVERABLE_BOUNDARIES:
                    assert result.ok, result
                    assert result.value["rebase_recovery_candidate"] == target.request_id
                    pool.shutdown()
                    _resume_at_b(
                        root,
                        boundary,
                        job,
                        manager,
                        current_binding,
                        stage,
                        item,
                        ctx,
                        result,
                        patch,
                    )
                else:
                    assert not result.ok, result
                    if boundary == "after_mutation":
                        assert result.error == (
                            f"workspace revision changed: expected {source_input.revision}, "
                            f"got {head}"
                        ), result
                    else:
                        assert (
                            result.error == "The pending rebase state needs operator recovery."
                        ), result
                    assert result.value["failure_kind"] == "validation_runner"
                remote_head = (root / "remote-head").read_text(encoding="ascii")
                assert remote_head == (
                    head if boundary in _RECOVERABLE_BOUNDARIES else target.expected_head_sha
                )
                if changed:
                    assert (root / "mutations").read_text(encoding="ascii").splitlines() == [head]
                else:
                    assert not (root / "mutations").exists()
                if boundary in _RECOVERABLE_BOUNDARIES:
                    assert (root / "pushes").read_text(encoding="ascii").splitlines() == [head]
                else:
                    assert not (root / "pushes").exists()
                channel.send(("verified", os.getpid()))
            finally:
                pool.shutdown()
    except BaseException as error:
        channel.send(
            ("error", type(error).__name__, str(error), traceback.format_exc(limit=8)[-8000:])
        )
        raise
    finally:
        channel.close()


@pytest.mark.parametrize(
    "operation,boundary",
    [
        ("initial", boundary)
        for boundary in (
            "before_intent",
            "after_intent",
            "after_mutation",
            "after_receipt",
            "after_pending",
            "before_push",
            "after_push",
        )
    ]
    + [
        ("continued", boundary)
        for boundary in (
            "after_mutation",
            "after_receipt",
            "after_pending",
            "before_push",
            "after_push",
        )
    ]
    + [("abort", "before_abort_record"), ("abort", "after_abort_record")],
)
def test_worker_process_death_does_not_authorize_rebase_replay(
    tmp_path: Path, operation: str, boundary: str
) -> None:
    """A fresh process must not infer mutation permission from a stopped worker."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=_stop_at_intent, args=(tmp_path, boundary, operation, child))
    inspector: Any = None
    inspect_parent: Any = None
    try:
        worker.start()
        child.close()
        assert parent.poll(30.0), "The worker did not reach the selected checkpoint."
        checkpoint = parent.recv()
        assert checkpoint[0] == "checkpoint", checkpoint
        _, worker_pid, job, source_input, tree = checkpoint
        assert worker_pid == worker.pid and worker_pid != os.getpid()
        worker.kill()
        worker.join(timeout=5.0)
        assert worker.exitcode == -signal.SIGKILL
        inspect_parent, inspect_child = context.Pipe(duplex=False)
        inspector = context.Process(
            target=_inspect_after_death,
            args=(tmp_path, boundary, operation, job, source_input, tree, inspect_child),
        )
        inspector.start()
        inspect_child.close()
        assert inspect_parent.poll(30.0), "The fresh owner did not finish within its budget."
        observed = inspect_parent.recv()
        assert observed == ("verified", inspector.pid), observed
        assert inspector.pid not in {worker_pid, os.getpid()}
        inspector.join(timeout=5.0)
        assert inspector.exitcode == 0
    finally:
        for process in (worker, inspector):
            if process is not None and process.pid is not None:
                if process.is_alive():
                    process.kill()
                process.join(timeout=5.0)
                assert not process.is_alive()
                process.close()
        parent.close()
        child.close()
        if inspect_parent is not None:
            inspect_parent.close()
