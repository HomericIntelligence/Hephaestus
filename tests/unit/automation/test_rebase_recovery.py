"""Tests for bound pending-rebase records and protected state transitions."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.pipeline import git_jobs
from hephaestus.automation.pipeline.host_capabilities import CapabilityRequestTarget
from hephaestus.automation.source_worktree import (
    SourceWorkspaceManager,
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
    SourceWorkspaceReceipt,
    _PreparationDeadline,
)
from tests.unit.automation.test_source_worktree import _repository


def _alter_discovery_fixture(
    scenario: str, pending: Any, manager: SourceWorkspaceManager, binding: WorkspaceBinding
) -> None:
    """Change one retained identity after the real worker has produced it."""
    if scenario == "dirty":
        (binding.cwd / "unexpected.txt").write_text("unreviewed change\n", encoding="utf-8")
        return
    if scenario not in {"intent", "tree", "generation"}:
        return
    if scenario == "intent":
        changed = replace(
            pending, phase="intent", resulting_workspace=None, resulting_tree_sha=None
        )
    elif scenario == "tree":
        changed = replace(pending, resulting_tree_sha="d" * 40)
    else:
        changed = replace(
            pending,
            resulting_workspace=replace(
                pending.resulting_workspace,
                generation=pending.resulting_workspace.generation + 1,
            ),
        )
    path = manager.state_dir / "pending-rebases" / f"1-{pending.request.request_id}.json"
    path.write_text(json.dumps(changed.to_dict()), encoding="utf-8")


def _registered_git_fixture(root: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Adapt only the unsupported listing protocol from actual registration facts."""
    import subprocess

    from hephaestus.automation import worktree_manager
    from hephaestus.automation.git_utils import run
    from tests.unit.automation.test_source_worktree import _git

    def registered_listing(argv: list[str], **kwargs: Any) -> Any:
        if argv == ["git", "worktree", "list", "--porcelain", "-z"]:
            assert kwargs["cwd"] == root
            listing = _git(root, "worktree", "list", "--porcelain")
            return subprocess.CompletedProcess(argv, 0, listing.replace("\n", "\0") + "\0\0", "")
        return run(argv, **kwargs)

    monkeypatch.setattr(worktree_manager, "run", registered_listing)
    return registered_listing


def _source_registration_fixture(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adapt the source owner's exact listing from real registered worktree facts."""
    import subprocess

    from hephaestus.automation import source_worktree

    source_git = source_worktree._git

    def registered(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if args == ("worktree", "list", "--porcelain", "-z"):
            assert cwd == root
            listed = source_git(cwd, "worktree", "list", "--porcelain", **kwargs)
            return subprocess.CompletedProcess(
                ["git", *args],
                listed.returncode,
                listed.stdout.replace("\n", "\0") + "\0\0",
                listed.stderr,
            )
        return source_git(cwd, *args, **kwargs)

    monkeypatch.setattr(source_worktree, "_git", registered)


def _resolve_real_conflict(stage: Any, item: Any, ctx: Any, pool: Any, manager: Any) -> Any:
    """Use real paused-source and edit validation with a controlled agent edit."""
    from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
    from hephaestus.automation.pipeline.stages import Continue, JobRequest

    item.state = "REBASE_CONFLICT_WAIT"
    edit = stage.step(item, ctx)
    assert isinstance(edit, JobRequest), edit
    assert isinstance(edit.job, AgentJob), edit
    assert edit.job.workspace is not None
    assert edit.job.source_operation is not None
    with manager.acquire(
        edit.job.workspace,
        source_operation=edit.job.source_operation,
        allowed_tools=edit.job.allowed_tools,
    ):
        attempt = item.attempts.get("rebase_conflict", 0)
        (edit.job.cwd / "tracked.txt").write_text(
            f"resolved conflict {attempt}\n", encoding="utf-8"
        )
    stage.on_job_done(item, JobResult(ok=True, value="Resolved the tracked conflict."), ctx)
    item.state = str(edit.on_done_state)
    validation = stage.step(item, ctx)
    assert isinstance(validation, JobRequest), validation
    assert isinstance(validation.job, GitJob)
    checked = pool._run_git(validation.job)
    assert checked.ok, checked
    assert checked.value["conflict_resolution"] == "resolved_content"
    stage.on_job_done(item, checked, ctx)
    following = stage.step(item, ctx)
    assert isinstance(following, Continue), following
    item.state = str(following.next_state)
    continuation = stage.step(item, ctx)
    assert isinstance(continuation, JobRequest), continuation
    assert isinstance(continuation.job, GitJob)
    assert continuation.job.op == "continue_rebase"
    return continuation.job


@pytest.mark.parametrize("pr_number", [None, 1001], ids=["before-pr", "with-pr"])
@pytest.mark.parametrize(
    "scenario",
    ["handoff", "complete", "missing_id", "foreign_id", "stale_snapshot", "repeated", "restart"],
)
def test_known_conflict_keeps_one_intent_through_admitted_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr_number: int | None, scenario: str
) -> None:
    """A paused conflict requires independent edit authority and its original intent."""
    import queue
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import Mock

    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline import worker_pool
    from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
    from hephaestus.automation.pipeline.host_capabilities import (
        QUOTA_UNAVAILABLE_TOKEN,
        HostCapabilityReceipt,
        WorkerCapabilities,
    )
    from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
    from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.stages import (
        Continue,
        ImplementationStage,
        JobRequest,
        StageContext,
    )
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
    from tests.unit.automation.test_source_worktree import _git

    previous_umask = os.umask(0o022)
    try:
        root, first, base = _repository(tmp_path, origin_repository="acme/repository")
        manager = SourceWorkspaceManager(root, repository="acme/repository")
        binding = manager.prepare(1, SourceLane.IMPLEMENTATION, first, branch="1-repair")
        (binding.cwd / "tracked.txt").write_text("writer conflict\n", encoding="utf-8")
        (binding.cwd / "test.py").write_text("# Structural input.\n", encoding="utf-8")
        _git(binding.cwd, "add", "tracked.txt", "test.py")
        _git(binding.cwd, "commit", "-s", "-m", "first writer change")
        if scenario == "repeated":
            (binding.cwd / "tracked.txt").write_text("second writer conflict\n", encoding="utf-8")
            _git(binding.cwd, "commit", "-s", "-am", "second writer change")
        original = _git(binding.cwd, "rev-parse", "HEAD")
        binding = manager.prepare(1, SourceLane.IMPLEMENTATION, original, branch="1-repair")
    finally:
        os.umask(previous_umask)
    _registered_git_fixture(root, monkeypatch)
    monkeypatch.setattr(
        worker_pool, "_trusted_gh_executable", lambda extra_path_root=None: "/usr/bin/gh"
    )
    backend = Mock(backend_id="hdiutil-v1")
    backend.preflight.side_effect = lambda target, **kwargs: HostCapabilityReceipt(
        False,
        QUOTA_UNAVAILABLE_TOKEN,
        "backend",
        "scratch",
        "e" * 32,
        target=target,
        cleanup_state="not_started",
    )
    signing = Mock()
    signing.environment.return_value = {"GIT_CONFIG_GLOBAL": os.devnull}
    policy = RebaseValidationPolicy("hephaestus-adr-v1", lambda path: None, ("test.py",))
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(backend, "conflict-test", signing),
        rebase_policy_selector=lambda repo: policy,
    )
    monkeypatch.setattr(
        pool, "_git_fetch_main", lambda job: JobResult(ok=True, value={"head_sha": base})
    )

    def remote_head(
        cwd: Path, *, remote: str, branch: str, expected_repo: str, timeout: int
    ) -> str:
        assert cwd == binding.cwd
        assert remote == "origin"
        assert expected_repo == "acme/repository"
        assert timeout > 0
        assert branch in {"main", "1-repair"}
        return base if branch == "main" else original

    monkeypatch.setattr(pool, "_read_remote_branch_head", remote_head)
    starts: list[str] = []
    continuations: list[Path] = []
    continue_process = WorkerPool._continue_rebase_process

    def counted_continuation(worker: Any, cwd: Path, **kwargs: Any) -> Any:
        continuations.append(cwd)
        return continue_process(worker, cwd, **kwargs)

    monkeypatch.setattr(WorkerPool, "_continue_rebase_process", counted_continuation)

    def rebase(**kwargs: Any) -> bool:
        assert kwargs["preserve_conflicts"] is True
        starts.append(original)
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "rebase", base],
            cwd=binding.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, result
        return False

    monkeypatch.setattr(git_utils, "rebase_worktree_onto", rebase)
    pushes = Mock(side_effect=AssertionError("A failed capability cannot publish."))
    monkeypatch.setattr(git_utils, "push_head_to_branch", pushes)
    ctx = StageContext(
        PipelineConfig(org="acme", repos=["repository"]),
        "acme",
        False,
        FakeStageGitHub(
            pr_state={
                "state": "OPEN",
                "headRefOid": original,
                "autoMergeRequest": None,
                "baseRefName": "main",
            }
        ),
        SimpleNamespace(repo_root=root, worktree=binding.cwd),
    )
    stage = ImplementationStage()
    item = WorkItem(
        "repository",
        ItemKind.ISSUE,
        issue=1,
        pr=pr_number,
        stage=StageName.IMPLEMENTATION,
        state="REBASE_WAIT",
        branch="1-repair",
        worktree=str(binding.cwd),
        payload={
            "rebase_reason": "manual",
            "rebase_restart_base_sha": base,
            "rebase_restart_head_sha": original,
            "_impl_source_workspace": binding.to_dict(),
            "_impl_source_revision": original,
            "_impl_source_receipt": manager._require_receipt(1, SourceLane.IMPLEMENTATION),
        },
    )
    try:
        discovery = stage.step(item, ctx)
        assert isinstance(discovery, JobRequest)
        assert isinstance(discovery.job, GitJob)
        read = pool._run_git(discovery.job)
        assert read.ok, read
        stage.on_job_done(item, read, ctx)
        preparation = stage.step(item, ctx)
        assert isinstance(preparation, JobRequest), preparation
        assert isinstance(preparation.job, AgentJob)
        stage.on_job_done(item, JobResult(ok=True), ctx)
        item.state = str(preparation.on_done_state)
        transition = stage.step(item, ctx)
        assert isinstance(transition, Continue), transition
        item.state = str(transition.next_state)
        first_job = stage.step(item, ctx)
        assert isinstance(first_job, JobRequest), first_job
        assert isinstance(first_job.job, GitJob)
        assert first_job.job.capability_target is not None
        operation_id = first_job.job.capability_target.request_id
        paused = pool._run_git(first_job.job)
        assert not paused.ok
        assert paused.error == "mechanical rebase hit conflicts; resolution required", paused
        assert paused.value["expected_remote_sha"] == original
        intent = _store(manager.common_dir).read(1, operation_id)
        assert intent.phase == "intent"
        assert intent.request.expected_head_sha == original
        stage.on_job_done(item, paused, ctx)
        continuation = _resolve_real_conflict(stage, item, ctx, pool, manager)
        assert continuation.workspace == binding
        assert continuation.capability_target is not None
        assert continuation.capability_target.request_id != operation_id
        if scenario == "handoff":
            assert paused.value.get("rebase_recovery_intent_id") == operation_id
            assert continuation.kwargs.get("rebase_recovery_intent_id") == operation_id
            return
        kwargs = {**continuation.kwargs, "rebase_recovery_intent_id": operation_id}
        if scenario == "missing_id":
            kwargs.pop("rebase_recovery_intent_id")
        elif scenario == "foreign_id":
            kwargs["rebase_recovery_intent_id"] = "f" * 32
        elif scenario == "stale_snapshot":
            kwargs["conflict_index_snapshot"] = "f" * 64
        result = pool._run_git(replace(continuation, kwargs=kwargs))
        if scenario in {"missing_id", "foreign_id", "stale_snapshot"}:
            assert not result.ok
            backend.preflight.assert_not_called()
            assert _store(manager.common_dir).read(1, operation_id) == intent
            assert _git(binding.cwd, "rev-parse", "HEAD") == paused.value["paused_head_sha"]
            return
        if scenario == "repeated":
            assert not result.ok
            assert (result.error or "").startswith("rebase conflict resolution required"), result
            assert result.value.get("rebase_recovery_intent_id") == operation_id
            assert _store(manager.common_dir).read(1, operation_id) == intent
            stage.on_job_done(item, result, ctx)
            following = _resolve_real_conflict(stage, item, ctx, pool, manager)
            assert (
                following.capability_target.request_id != continuation.capability_target.request_id
            )
            assert following.kwargs["rebase_recovery_intent_id"] == operation_id
            result = pool._run_git(following)
        backend.preflight.assert_called_once()
        assert not result.ok
        assert result.value["failure_kind"] == "validation_runner"
        retained = _store(manager.common_dir).read(1, operation_id)
        assert retained.phase == "pending_validation"
        assert retained.request == intent.request
        assert retained.operation == "rebase"
        assert retained.remote_head_sha == (original if pr_number is not None else None)
        assert retained.resulting_workspace.revision == _git(binding.cwd, "rev-parse", "HEAD")
        assert starts == [original]
        pushes.assert_not_called()
        _assert_continuation_restart_block(scenario, stage, item, ctx, result, continuations)
    finally:
        pool.shutdown()
    if scenario == "restart":
        checkout = binding.cwd
        del pool, stage, item, ctx, result, retained, intent, manager, binding, continuation
        _assert_continued_restart(
            root, checkout, pr_number, original, operation_id, starts, monkeypatch, tmp_path
        )
        assert continuations == [checkout]


def _assert_continuation_restart_block(
    scenario: str, stage: Any, item: Any, ctx: Any, result: Any, continuations: list[Path]
) -> None:
    """Require the first process to stop before the restart fixture discards it."""
    from hephaestus.automation.pipeline.stages import StageOutcome

    if scenario != "restart":
        return
    stage.on_job_done(item, result, ctx)
    blocked = stage.step(item, ctx)
    assert isinstance(blocked, StageOutcome), blocked
    assert blocked.disposition.value == "blocked"
    assert continuations == [Path(item.worktree)]


def _assert_continued_restart(
    root: Path,
    checkout: Path,
    pr_number: int | None,
    original: str,
    operation_id: str,
    starts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Discover completed continuation output without old source or conflict state."""
    import queue
    from types import SimpleNamespace

    from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.stages import (
        ImplementationStage,
        JobRequest,
        StageContext,
    )
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    ctx = StageContext(
        PipelineConfig(org="acme", repos=["repository"]),
        "acme",
        False,
        FakeStageGitHub(
            pr_state={
                "state": "OPEN",
                "headRefOid": original,
                "autoMergeRequest": None,
                "baseRefName": "main",
            }
        ),
        SimpleNamespace(repo_root=root, worktree=checkout),
    )
    stage = ImplementationStage()
    item = WorkItem(
        "repository",
        ItemKind.ISSUE,
        issue=1,
        pr=pr_number,
        stage=StageName.IMPLEMENTATION,
        state="REBASE_CONTINUE_WAIT",
        branch="1-repair",
        payload={"rebase_reason": "manual"},
    )
    discovery = stage.step(item, ctx)
    assert isinstance(discovery, JobRequest), discovery
    assert isinstance(discovery.job, git_jobs.GitJob)
    assert discovery.job.op == "discover_pending_rebase"
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        result = pool._run_git(discovery.job)
    finally:
        pool.shutdown()
    assert result.ok, result
    assert result.value["rebase_recovery_candidate"] == operation_id
    stage.on_job_done(item, result, ctx)
    binding = WorkspaceBinding.from_dict(result.value["source_workspace"])
    assert binding.revision != original
    assert not any(key.startswith("rebase_conflict") for key in item.payload)
    _assert_fresh_resume(
        "resume_stage",
        stage,
        ctx,
        item,
        binding,
        operation_id,
        original,
        starts,
        monkeypatch,
        tmp_path,
    )


def _prepare_recovery_writer_input(
    root: Path,
    first: str,
    base: str,
    manager: SourceWorkspaceManager,
    *,
    fast_forward: bool,
) -> tuple[str, str, WorkspaceBinding]:
    """Keep fast-forward input separate from an actual replayed writer commit."""
    from tests.unit.automation.test_source_worktree import _git

    if fast_forward:
        (root / "test.py").write_text("# Captured base input.\n", encoding="utf-8")
        _git(root, "add", "test.py")
        _git(root, "commit", "-s", "-m", "captured base input")
        base = _git(root, "rev-parse", "HEAD")
        binding = manager.prepare(1, SourceLane.IMPLEMENTATION, first, branch="1-repair")
        assert first != base
        assert _git(root, "merge-base", "--is-ancestor", first, base) == ""
        return first, base, binding
    binding = manager.prepare(1, SourceLane.IMPLEMENTATION, first, branch="1-repair")
    (binding.cwd / "writer.txt").write_text("writer change\n", encoding="utf-8")
    (binding.cwd / "test.py").write_text("# Controlled structural input.\n", encoding="utf-8")
    _git(binding.cwd, "add", "writer.txt", "test.py")
    _git(binding.cwd, "commit", "-s", "-m", "writer input")
    original = _git(binding.cwd, "rev-parse", "HEAD")
    binding = manager.prepare(1, SourceLane.IMPLEMENTATION, original, branch="1-repair")
    return original, base, binding


@pytest.mark.parametrize("pr_number", [None, 1001], ids=["before-pr", "with-pr"])
@pytest.mark.parametrize(
    "restart",
    [
        "none",
        "stage",
        "worker",
        "stale_item",
        "intent",
        "dirty",
        "tree",
        "generation",
        "resume_stage",
        "resume_worker",
        "resume_fast_forward",
    ],
)
def test_worker_retains_real_rebase_result_before_capability_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr_number: int | None, restart: str
) -> None:
    """A failed probe retains the original lease and the worker-created result."""
    import queue
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import Mock

    from hephaestus.automation import git_utils, worktree_manager
    from hephaestus.automation.git_utils import run
    from hephaestus.automation.pipeline import worker_pool
    from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
    from hephaestus.automation.pipeline.host_capabilities import (
        QUOTA_UNAVAILABLE_TOKEN,
        CapabilityReceiptTarget,
        HostCapabilityReceipt,
        WorkerCapabilities,
    )
    from hephaestus.automation.pipeline.job_results import JobResult
    from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
    from hephaestus.automation.pipeline.routing import Disposition, StageName
    from hephaestus.automation.pipeline.stages import (
        ImplementationStage,
        JobRequest,
        StageContext,
        StageOutcome,
    )
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
    from tests.unit.automation.test_source_worktree import _git

    previous_umask = os.umask(0o022)
    try:
        root, first, base = _repository(tmp_path, origin_repository="acme/repository")
        manager = SourceWorkspaceManager(root, repository="acme/repository")
        original, base, binding = _prepare_recovery_writer_input(
            root, first, base, manager, fast_forward=restart == "resume_fast_forward"
        )
    finally:
        os.umask(previous_umask)

    original_run = run

    def registered_listing(argv: list[str], **kwargs: Any) -> Any:
        if argv == ["git", "worktree", "list", "--porcelain", "-z"]:
            assert kwargs["cwd"] == root
            listing = _git(root, "worktree", "list", "--porcelain")
            return subprocess.CompletedProcess(argv, 0, listing.replace("\n", "\0") + "\0\0", "")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(worktree_manager, "run", registered_listing)
    monkeypatch.setattr(
        worker_pool, "_trusted_gh_executable", lambda extra_path_root=None: "/usr/bin/gh"
    )

    def context() -> StageContext:
        return StageContext(
            config=PipelineConfig(org="acme", repos=["repository"]),
            org="acme",
            dry_run=False,
            github=FakeStageGitHub(
                pr_state={
                    "state": "OPEN",
                    "headRefOid": original,
                    "autoMergeRequest": None,
                    "baseRefName": "main",
                }
            ),
            paths=SimpleNamespace(repo_root=root, worktree=binding.cwd),
        )

    stage, ctx = ImplementationStage(), context()
    item = WorkItem(
        "repository",
        ItemKind.ISSUE,
        issue=1,
        pr=pr_number,
        stage=StageName.IMPLEMENTATION,
        state="REBASE_WAIT",
        branch="1-repair",
        worktree=str(binding.cwd),
        payload={
            "rebase_reason": "manual",
            "_impl_source_workspace": binding.to_dict(),
            "_impl_source_revision": original,
            "_impl_source_receipt": manager._require_receipt(1, SourceLane.IMPLEMENTATION),
        },
    )
    probed: list[CapabilityReceiptTarget] = []

    def unavailable(target: CapabilityReceiptTarget, *, deadline: Any) -> HostCapabilityReceipt:
        assert deadline.remaining() > 0
        assert target.source_head_sha == _git(binding.cwd, "rev-parse", "HEAD")
        assert target.source_head_sha != original
        probed.append(target)
        return HostCapabilityReceipt(
            False,
            QUOTA_UNAVAILABLE_TOKEN,
            "backend",
            "scratch",
            "b" * 32,
            target=target,
            cleanup_state="not_started",
        )

    backend = Mock(backend_id="hdiutil-v1", preflight=Mock(side_effect=unavailable))
    signing = Mock()
    signing.environment.return_value = {"GIT_CONFIG_GLOBAL": os.devnull}
    policy = RebaseValidationPolicy("hephaestus-adr-v1", lambda path: None, ("test.py",))
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(backend, "recovery-test", signing),
        rebase_policy_selector=lambda repo: policy,
    )
    mutations: list[str] = []

    def rebase(**kwargs: Any) -> bool:
        assert kwargs["cwd"] == binding.cwd
        assert kwargs["base_sha"] == base
        mutations.append(original)
        _git(binding.cwd, "-c", "commit.gpgsign=false", "rebase", base)
        return True

    monkeypatch.setattr(git_utils, "rebase_worktree_onto", rebase)
    monkeypatch.setattr(
        pool, "_git_fetch_main", lambda job: JobResult(ok=True, value={"head_sha": base})
    )
    execute = Mock(side_effect=AssertionError("Execution must not start after a failed probe."))
    publish = Mock(side_effect=AssertionError("Publication must not start after a failed probe."))
    monkeypatch.setattr(pool, "_run_immutable_build_test", execute)
    monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
    try:
        discovery = stage.step(item, ctx)
        assert isinstance(discovery, JobRequest), discovery
        assert isinstance(discovery.job, git_jobs.GitJob)
        assert discovery.job.op == "discover_pending_rebase"
        initial_read = pool._run_git(discovery.job)
        assert initial_read.ok, initial_read
        assert initial_read.value["rebase_recovery_candidate"] is None
        stage.on_job_done(item, initial_read, ctx)
        submission = stage.step(item, ctx)
        assert isinstance(submission, JobRequest), submission
        assert isinstance(submission.job, git_jobs.GitJob)
        job = submission.job
        assert job.op == "rebase"
        request = job.capability_target
        assert isinstance(request, CapabilityRequestTarget)
        result = pool._run_git(job)
    finally:
        pool.shutdown()
    assert len(probed) == 1, result
    assert mutations == [original]
    assert not result.ok
    assert result.value["failure_kind"] == "validation_runner"
    resulting = _git(binding.cwd, "rev-parse", "HEAD")
    assert result.value["source_workspace"]["revision"] == resulting
    execute.assert_not_called()
    publish.assert_not_called()
    stage.on_job_done(item, result, ctx)
    blocked = stage.step(item, ctx)
    assert isinstance(blocked, StageOutcome), blocked
    assert blocked.disposition is Disposition.BLOCKED
    del pool, manager, result, stage, ctx, item, submission, job
    restarted_manager = SourceWorkspaceManager(root, repository="acme/repository")
    pending = _store(restarted_manager.common_dir).candidate(1)
    assert pending is not None
    assert pending.phase == "pending_validation"
    assert pending.request == request
    assert pending.remote_head_sha == (original if pr_number is not None else None)
    assert pending.resulting_workspace.revision == resulting
    assert pending.resulting_tree_sha == _git(binding.cwd, "rev-parse", "HEAD^{tree}")
    if restart == "none":
        return
    candidate_id = pending.request.request_id
    _alter_discovery_fixture(restart, pending, restarted_manager, binding)
    del pending, restarted_manager
    fresh_stage, fresh_ctx = ImplementationStage(), context()
    fresh_item = WorkItem(
        "repository",
        ItemKind.ISSUE,
        issue=1,
        pr=pr_number,
        stage=StageName.IMPLEMENTATION,
        state="REBASE_WAIT",
        branch="1-repair",
        payload={"rebase_reason": "manual"},
    )
    if restart == "stale_item":
        fresh_item.worktree = str(binding.cwd)
        fresh_item.payload["_impl_source_workspace"] = binding.to_dict()
        fresh_item.payload["_impl_source_revision"] = original
    if restart in {"stage", "stale_item", "resume_stage", "resume_fast_forward"}:
        discovery = fresh_stage.step(fresh_item, fresh_ctx)
        assert isinstance(discovery, JobRequest), discovery
        assert isinstance(discovery.job, git_jobs.GitJob)
        assert discovery.job.op == "discover_pending_rebase"
        discovery_job = discovery.job
    else:
        discovery_job = git_jobs.GitJob(
            "repository",
            "discover_pending_rebase",
            60,
            expected_repository="acme/repository",
            kwargs={
                "repo_root": str(root),
                "issue_number": 1,
                "pr_number": pr_number,
                "branch": "1-repair",
            },
        )
    fresh_pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        discovered = fresh_pool._run_git(discovery_job)
    finally:
        fresh_pool.shutdown()
    if restart in {"intent", "dirty", "tree", "generation"}:
        assert not discovered.ok, discovered
        assert (
            not isinstance(discovered.value, dict)
            or discovered.value.get("rebase_recovery_candidate") is None
        )
        assert mutations == [original]
        assert len(probed) == 1
        publish.assert_not_called()
        return
    assert discovered.ok, discovered
    assert discovered.value["rebase_recovery_candidate"] == candidate_id
    discovered_binding = WorkspaceBinding.from_dict(discovered.value["source_workspace"])
    assert discovered_binding.revision == resulting
    assert discovered_binding.revision != original
    assert discovered_binding.cwd == binding.cwd
    if restart in {"stage", "stale_item", "resume_stage", "resume_fast_forward"}:
        fresh_stage.on_job_done(fresh_item, discovered, fresh_ctx)
        assert fresh_item.payload["_impl_source_revision"] == resulting
        assert fresh_item.payload["rebase_recovery_candidate"] == candidate_id
        assert fresh_item.payload["_impl_source_receipt"].revision == resulting
    assert mutations == [original]
    assert len(probed) == 1
    publish.assert_not_called()
    if restart.startswith("resume_"):
        _assert_fresh_resume(
            restart,
            fresh_stage,
            fresh_ctx,
            fresh_item,
            discovered_binding,
            candidate_id,
            original,
            mutations,
            monkeypatch,
            tmp_path,
        )


def _assert_fresh_resume(
    scenario: str,
    stage: Any,
    ctx: Any,
    item: Any,
    binding: WorkspaceBinding,
    candidate_id: str,
    original: str,
    mutations: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Run fresh checks at retained B without a second mutation."""
    import queue
    import subprocess
    import time
    from unittest.mock import Mock

    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline.host_capabilities import (
        QUOTA_AVAILABLE_TOKEN,
        CapabilityReceiptTarget,
        HostCapabilityReceipt,
        WorkerCapabilities,
    )
    from hephaestus.automation.pipeline.job_results import JobResult
    from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
    from hephaestus.automation.pipeline.stages import JobRequest
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.test_source_worktree import _git

    root = binding.reusable_root
    assert root is not None
    assert isinstance(binding.revision, str)
    source_head = binding.revision
    if scenario in {"resume_stage", "resume_fast_forward"}:
        submitted = stage.step(item, ctx)
        assert isinstance(submitted, JobRequest), submitted
        assert isinstance(submitted.job, git_jobs.GitJob)
        job = submitted.job
    else:
        target = CapabilityRequestTarget(
            "acme/repository",
            1,
            item.pr,
            root,
            binding.cwd,
            binding.revision,
            "rebase",
            "scratch",
            "c" * 32,
            workspace=binding,
            generation=2,
        )
        job = git_jobs.GitJob(
            "repository",
            "rebase",
            60,
            expected_repository="acme/repository",
            workspace=binding,
            capability_target=target,
            rebase_recovery_candidate=candidate_id,
            kwargs={
                "repo_root": str(root),
                "cwd": binding.cwd,
                "issue_number": 1,
                "pr_number": item.pr,
                "branch": "1-repair",
                "rebase_reason": "manual",
                "expected_head_sha": binding.revision,
                "expected_remote_sha": original,
                "publish_rebased_head": item.pr is not None,
            },
        )
    assert job.rebase_recovery_candidate == candidate_id
    assert job.workspace == binding
    assert job.capability_target is not None
    assert job.capability_target.workspace == binding
    assert job.capability_target.expected_head_sha == binding.revision
    seen: list[str] = []

    def available(target: CapabilityReceiptTarget, *, deadline: Any) -> HostCapabilityReceipt:
        assert deadline.remaining() > 0
        assert target.source_head_sha == binding.revision
        assert target.request == job.capability_target
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

    signing = Mock()
    signing.environment.return_value = {"GIT_CONFIG_GLOBAL": os.devnull}
    backend = Mock(backend_id="hdiutil-v1", preflight=Mock(side_effect=available))
    policy = RebaseValidationPolicy("hephaestus-adr-v1", semantic, ("test.py",))
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(backend, "fresh-process", signing),
        rebase_policy_selector=lambda repo: policy,
    )
    manager = SourceWorkspaceManager(root, repository="acme/repository")
    retained = _store(manager.common_dir).read(1, candidate_id)
    assert retained is not None
    if scenario == "resume_fast_forward":
        assert source_head == retained.target_base_sha
        assert original != source_head
        assert _git(binding.cwd, "merge-base", "--is-ancestor", original, source_head) == ""
        assert _git(binding.cwd, "rev-list", f"{retained.target_base_sha}..HEAD") == ""
    original_run = git_utils.run

    def metadata(argv: list[str], **kwargs: Any) -> Any:
        result = original_run(argv, **kwargs)
        if argv[:3] == ["git", "cat-file", "-p"]:
            assert argv[3] == binding.revision
            seen.append("metadata")
            return subprocess.CompletedProcess(
                argv, 0, result.stdout + "\ngpgsig controlled-test-signature\n", result.stderr
            )
        return result

    def execute(request: Any) -> JobResult:
        assert request.expected_head_sha == binding.revision
        assert request.immutable_source is True
        seen.append("structural")
        return JobResult(ok=True, value={"head_sha": binding.revision, "immutable_source": True})

    pushes: list[str] = []

    def publish(branch: str, expected_remote_sha: str, cwd: Path, **kwargs: Any) -> None:
        assert branch == "1-repair"
        assert expected_remote_sha == original
        assert cwd == binding.cwd
        assert kwargs["source_sha"] == binding.revision
        deadline = _PreparationDeadline(time.monotonic() + 1, time.monotonic, threading.Event())
        record = _store(manager.common_dir, deadline=deadline).read(1, candidate_id)
        assert record.phase == "publication_intent"
        pushes.append(source_head)

    if scenario != "resume_fast_forward":
        monkeypatch.setattr(git_utils, "run", metadata)
    monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
    monkeypatch.setattr(pool, "_run_immutable_build_test", execute)
    monkeypatch.setattr(pool, "_read_remote_branch_head", lambda *args, **kwargs: original)
    monkeypatch.setattr(
        pool,
        "_git_fetch_main",
        lambda job: JobResult(ok=True, value={"head_sha": retained.target_base_sha}),
    )
    try:
        result = pool._run_git(job)
    finally:
        pool.shutdown()
    assert result.ok, result
    assert result.value["head_sha"] == binding.revision
    assert mutations == [original]
    assert seen == (
        ["quota", "structural", "semantic"]
        if scenario == "resume_fast_forward"
        else ["quota", "structural", "semantic", "metadata"]
    )
    signing.environment.assert_called_once()
    assert pushes == ([binding.revision] if item.pr is not None else [])
    completed = _store(manager.common_dir).read(1, candidate_id)
    assert completed.phase == "complete"
    assert completed.remote_head_sha == (original if item.pr is not None else None)
    assert completed.resulting_workspace == binding
    assert "review_audit" not in result.value


@pytest.mark.parametrize(
    "scenario", ["divergent", "same_head", "other_result", "ancestry_error", "continued_intent"]
)
def test_recovery_fast_forward_requires_exact_graph_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Reject invalid graph proof at the resume boundary without claiming lease coverage."""
    import queue
    import subprocess
    import time
    from unittest.mock import Mock

    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.test_source_worktree import _git

    root, original, base = _repository(tmp_path, origin_repository="acme/repository")
    resulting = base
    if scenario in {"divergent", "continued_intent"}:
        _git(root, "checkout", "-b", "divergent-input", original)
        (root / "writer.txt").write_text("Divergent input.\n", encoding="utf-8")
        _git(root, "add", "writer.txt")
        _git(root, "commit", "-s", "-m", "divergent input")
        original = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "main")
    if scenario == "same_head":
        original = base
    if scenario == "other_result":
        base = original
    record = _record(root, phase="pending_validation")
    workspace = replace(record.request.workspace, cwd=root, revision=original)
    request = replace(
        record.request, workspace=workspace, checkout_path=root, expected_head_sha=original
    )
    binding = replace(workspace, revision=resulting, generation=workspace.generation + 1)
    record = replace(
        record,
        request=request,
        remote_head_sha=original,
        target_base_sha=base,
        resulting_workspace=binding,
        resulting_tree_sha=_git(root, "rev-parse", "HEAD^{tree}"),
    )
    assert record.operation == "rebase"
    run = git_utils.run

    def ancestry(argv: list[str], **kwargs: Any) -> Any:
        if scenario == "ancestry_error" and argv == [
            "git",
            "merge-base",
            "--is-ancestor",
            original,
            resulting,
        ]:
            return subprocess.CompletedProcess(argv, 2, "", "Controlled ancestry failure.")
        return run(argv, **kwargs)

    monkeypatch.setattr(git_utils, "run", ancestry)
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    publish = Mock(return_value=JobResult(ok=True))
    monkeypatch.setattr(pool, "_admit_pending_rebase", lambda *args: record)
    monkeypatch.setattr(pool, "_required_signing_environment", lambda *args, **kwargs: {})
    monkeypatch.setattr(pool, "_rebase_structural_receipts", lambda *args: JobResult(ok=True))
    monkeypatch.setattr(pool, "_validate_rebased_tree", lambda *args, **kwargs: None)
    monkeypatch.setattr(pool, "_refresh_pending_rebase_source", lambda *args: None)
    monkeypatch.setattr(pool, "_publish_pending_rebase", publish)
    monkeypatch.setattr(pool, "_rebase_resume_source_result", lambda result, *args: result)
    try:
        result = pool._resume_pending_rebase(
            git_jobs.GitJob(
                "repository",
                "continue_rebase" if scenario == "continued_intent" else "rebase",
                60,
                expected_repository="acme/repository",
                workspace=binding,
                capability_target=replace(
                    request,
                    workspace=binding,
                    expected_head_sha=resulting,
                    request_id="e" * 32,
                    generation=2,
                ),
                rebase_recovery_candidate=request.request_id,
            ),
            SourceWorkspaceManager(root, repository="repository"),
            binding,
            _PreparationDeadline(time.monotonic() + 60, time.monotonic, threading.Event()),
        )
    finally:
        pool.shutdown()
    assert not result.ok, result
    publish.assert_not_called()
    strict = WorkerPool._verify_rebased_commit_metadata(root, base_sha=resulting, timeout=60)
    assert strict is not None and not strict.ok
    assert strict.error == "completed rebase produced no branch commits"


@contextmanager
def _abort_worker_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fallback: bool,
    fault: str,
    conflict: bool = True,
    structural: bool = False,
    pr_number: int | None = 1001,
) -> Any:
    """Keep real source admission and Git conflicts with controlled external services."""
    import queue
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import Mock

    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline import worker_pool
    from hephaestus.automation.pipeline.host_capabilities import WorkerCapabilities
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.test_source_worktree import _git

    prior_umask = os.umask(0o022)
    try:
        root, first, base = _repository(tmp_path, origin_repository="acme/repository")
        manager = SourceWorkspaceManager(root, repository="acme/repository")
        binding = manager.prepare(1, SourceLane.IMPLEMENTATION, first, branch="1-repair")
        writer_path = "tracked.txt" if conflict else "writer.txt"
        (binding.cwd / writer_path).write_text("writer conflict\n", encoding="utf-8")
        _git(binding.cwd, "add", writer_path)
        if structural:
            (binding.cwd / "test.py").write_text("# Structural input.\n", encoding="utf-8")
            _git(binding.cwd, "add", "test.py")
        _git(binding.cwd, "commit", "-s", "-m", "writer conflict input")
        original = _git(binding.cwd, "rev-parse", "HEAD")
        tree = _git(binding.cwd, "rev-parse", "HEAD^{tree}")
        binding = manager.prepare(1, SourceLane.IMPLEMENTATION, original, branch="1-repair")
    finally:
        os.umask(prior_umask)
    receipt = manager._require_receipt(1, SourceLane.IMPLEMENTATION)
    _registered_git_fixture(root, monkeypatch)
    monkeypatch.setattr(
        worker_pool, "_trusted_gh_executable", lambda extra_path_root=None: "/usr/bin/gh"
    )
    signing = Mock()
    signing.environment.return_value = {"GIT_CONFIG_GLOBAL": os.devnull}
    backend = Mock(backend_id="hdiutil-v1")
    policy = RebaseValidationPolicy(
        "mnemosyne-current-head-v1" if fallback else "hephaestus-adr-v1",
        lambda path: None,
        ("test.py",) if structural else (),
        allow_unrebased_writer_fallback=fallback,
    )
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(backend, "abort-test", signing),
        rebase_policy_selector=lambda repo: policy,
    )
    monkeypatch.setattr(
        pool, "_git_fetch_main", lambda job: JobResult(ok=True, value={"head_sha": base})
    )

    def remote_head(cwd: Path, **kwargs: Any) -> str:
        assert cwd == binding.cwd
        assert kwargs["remote"] == "origin"
        assert kwargs["expected_repo"] == "acme/repository"
        assert kwargs["branch"] in {"main", "1-repair"}
        return base if kwargs["branch"] == "main" else original

    monkeypatch.setattr(pool, "_read_remote_branch_head", remote_head)
    target = CapabilityRequestTarget(
        "acme/repository",
        1,
        pr_number,
        root,
        binding.cwd,
        original,
        "rebase",
        "scratch",
        "a" * 32,
        workspace=binding,
        generation=1,
    )
    job = git_jobs.GitJob(
        "repository",
        "rebase",
        60,
        expected_repository="acme/repository",
        workspace=binding,
        capability_target=target,
        kwargs={
            "repo_root": str(root),
            "cwd": binding.cwd,
            "issue_number": 1,
            "pr_number": pr_number,
            "branch": "1-repair",
            "rebase_reason": "manual",
            "expected_head_sha": original,
            "expected_remote_sha": original,
            "publish_rebased_head": pr_number is not None,
            "resolve_conflicts": fallback,
            "expected_base_sha": base,
        },
    )
    starts: list[bool] = []
    aborts: list[int] = []

    def rebase(**kwargs: Any) -> bool:
        intent = _store(manager.common_dir).read(1, target.request_id)
        assert intent is not None and intent.phase == "intent"
        assert intent.request == target
        starts.append(kwargs["preserve_conflicts"])
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "rebase", base],
            cwd=binding.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, result
        return False

    original_run = git_utils.run

    def abort_command(argv: list[str], **kwargs: Any) -> Any:
        if argv != ["git", "rebase", "--abort"]:
            if (
                fault == "wrong_tree"
                and aborts
                and argv == ["git", "rev-parse", "--verify", "HEAD^{tree}"]
            ):
                return subprocess.CompletedProcess(argv, 0, "f" * 40 + "\n", "")
            return original_run(argv, **kwargs)
        assert kwargs["cwd"] == binding.cwd
        aborts.append(1)
        if fault == "abort_failed":
            return subprocess.CompletedProcess(argv, 1, "", "Controlled abort failure.")
        if fault in {"timeout_text", "timeout_bytes"}:
            output = "x" * 5000 + "abort-stdout-tail"
            error = "Authorization: Bearer not-a-real-credential\nabort-stderr-tail"
            raise subprocess.TimeoutExpired(
                argv,
                1,
                output=output.encode() if fault == "timeout_bytes" else output,
                stderr=error.encode() if fault == "timeout_bytes" else error,
            )
        if fault == "cancelled":
            pool._shutdown.set()
            raise InterruptedError("Controlled abort cancellation.")
        result = original_run(argv, **kwargs)
        assert result.returncode == 0
        _change_restored_abort_source(fault, binding.cwd, manager, receipt, base)
        return result

    monkeypatch.setattr(git_utils, "rebase_worktree_onto", rebase)
    monkeypatch.setattr(git_utils, "run", abort_command)
    pushes = Mock(side_effect=AssertionError("An abort cannot publish."))
    monkeypatch.setattr(git_utils, "push_head_to_branch", pushes)
    try:
        yield SimpleNamespace(
            pool=pool,
            job=job,
            manager=manager,
            binding=binding,
            original=original,
            tree=tree,
            receipt=receipt,
            backend=backend,
            starts=starts,
            aborts=aborts,
            pushes=pushes,
            base=base,
        )
    finally:
        pool.shutdown()


def _change_restored_abort_source(
    fault: str, checkout: Path, manager: Any, receipt: Any, base: str
) -> None:
    """Change one fixture fact after a successful external abort command."""
    from tests.unit.automation.test_source_worktree import _git

    if fault == "dirty":
        (checkout / "unexpected.txt").write_text("post-abort change\n", encoding="utf-8")
    elif fault == "dirty_index":
        (checkout / "tracked.txt").write_text("changed index\n", encoding="utf-8")
        _git(checkout, "add", "tracked.txt")
    elif fault == "paused_metadata":
        path = Path(_git(checkout, "rev-parse", "--git-path", "rebase-apply"))
        (checkout / path).mkdir(mode=0o700)
    elif fault == "wrong_head":
        _git(checkout, "reset", "--hard", base)
    elif fault == "wrong_branch":
        _git(checkout, "checkout", "-b", "foreign-branch")
    elif fault == "wrong_owner":
        manager._write_receipt(replace(receipt, ownership_key="foreign-owner"))
    elif fault == "wrong_generation":
        manager._write_receipt(replace(receipt, generation=receipt.generation + 1))


@pytest.mark.parametrize(
    "fallback,fault",
    [
        (False, "clean"),
        (True, "clean"),
        (True, "abort_failed"),
        (True, "dirty"),
        (True, "readback_failed"),
        (True, "dirty_index"),
        (True, "paused_metadata"),
        (True, "wrong_head"),
        (True, "wrong_tree"),
        (True, "wrong_branch"),
        (True, "wrong_owner"),
        (True, "wrong_generation"),
        (True, "timeout_text"),
        (True, "timeout_bytes"),
        (True, "cancelled"),
        (True, "write_failed"),
        (True, "write_after_replace"),
    ],
)
def test_worker_conflict_abort_requires_durable_verified_restoration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool, fault: str
) -> None:
    """Real worker abort outcomes require checked restoration and terminal readback."""
    from tests.unit.automation.test_source_worktree import _git

    with _abort_worker_case(tmp_path, monkeypatch, fallback=fallback, fault=fault) as case:
        module = import_module("hephaestus.automation.rebase_recovery")
        read = module._read_record
        write = module._write_receipt
        failed_readbacks: list[bool] = []
        failed_writes: list[str] = []

        def readback(*args: Any, **kwargs: Any) -> Any:
            value = read(*args, **kwargs)
            if fault == "readback_failed" and value is not None and value[0].phase == "aborted":
                failed_readbacks.append(True)
                raise ValueError("Controlled terminal readback failure.")
            return value

        def terminal_write(*args: Any, **kwargs: Any) -> Any:
            if (
                fault not in {"write_failed", "write_after_replace"}
                or json.loads(args[-1])["phase"] != "aborted"
            ):
                return write(*args, **kwargs)
            failed_writes.append(fault)
            if fault == "write_after_replace":
                write(*args, **kwargs)
            raise OSError("Controlled terminal write failure.")

        with monkeypatch.context() as patch:
            patch.setattr(module, "_read_record", readback)
            patch.setattr(module, "_write_receipt", terminal_write)
            result = case.pool._run_git(case.job)
        assert case.starts == [True]
        assert case.aborts == [1]
        case.backend.preflight.assert_not_called()
        case.pushes.assert_not_called()
        retained = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert retained.request == case.job.capability_target
        assert retained.resulting_workspace is None
        assert retained.resulting_tree_sha is None
        if fault != "clean":
            _assert_abort_failure_evidence(fault, result, retained, failed_readbacks, failed_writes)
            return
        assert retained.phase == "aborted"
        assert retained.restored_workspace == case.binding
        assert retained.restored_tree_sha == case.tree
        assert case.manager._require_receipt(1, SourceLane.IMPLEMENTATION) == case.receipt
        assert _git(case.binding.cwd, "rev-parse", "HEAD") == case.original
        assert _git(case.binding.cwd, "rev-parse", "HEAD^{tree}") == case.tree
        assert _git(case.binding.cwd, "status", "--porcelain") == ""
        assert _git(case.binding.cwd, "symbolic-ref", "--short", "HEAD") == "1-repair"
        for directory in ("rebase-merge", "rebase-apply"):
            metadata = Path(_git(case.binding.cwd, "rev-parse", "--git-path", directory))
            assert not (case.binding.cwd / metadata).exists()
        assert _store(case.manager.common_dir).candidate(1) is None
        if fallback:
            assert result.ok, result
            assert result.value["rebase_fallback"] == "verified-current-head"
        else:
            assert not result.ok
            assert result.error == "rebase conflict restart required"
            assert result.value["rebase_restart_required"] is True


def _assert_abort_failure_evidence(
    fault: str, result: Any, retained: Any, failed_readbacks: list[bool], failed_writes: list[str]
) -> None:
    """Keep blocked outcomes distinct from the actual bytes retained on disk."""
    assert not result.ok, result
    assert result.value["failure_kind"] == "validation_runner"
    assert not result.value.get("rebase_restart_required")
    assert retained.phase == (
        "aborted" if fault in {"readback_failed", "write_after_replace"} else "intent"
    )
    assert failed_readbacks == ([True] if fault == "readback_failed" else [])
    assert failed_writes == ([fault] if fault in {"write_failed", "write_after_replace"} else [])
    if fault in {"timeout_text", "timeout_bytes"}:
        assert result.stdout_tail.endswith("abort-stdout-tail")
        assert result.stderr_tail.endswith("abort-stderr-tail")
        assert len(result.stdout_tail) <= 4000
        assert len(result.stderr_tail) <= 4000
        assert "not-a-real-credential" not in result.stderr_tail
        assert "conflict" in result.error.lower()
    if fault == "cancelled":
        assert result.interrupted is True
        assert "cancellation" in result.error


@pytest.mark.parametrize("base_changed", [False, True], ids=["admitted", "base-drift"])
def test_worker_terminal_abort_allows_only_a_fresh_exact_base_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_changed: bool
) -> None:
    """A fresh stage derives source ownership and separately admits an explicit retry."""
    import queue
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import Mock

    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
    from hephaestus.automation.pipeline.host_capabilities import WorkerCapabilities
    from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
    from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.stages import (
        Continue,
        ImplementationStage,
        JobRequest,
        StageContext,
    )
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    with _abort_worker_case(tmp_path, monkeypatch, fallback=False, fault="clean") as case:
        aborted = case.pool._run_git(case.job)
        assert aborted.error == "rebase conflict restart required", aborted
        prior_id = case.job.capability_target.request_id
        terminal = _store(case.manager.common_dir).read(1, prior_id)
        assert terminal.phase == "aborted"
        case.pool.shutdown()
        del case.pool
        stage = ImplementationStage()
        item = WorkItem(
            "repository",
            ItemKind.ISSUE,
            issue=1,
            pr=1001,
            stage=StageName.IMPLEMENTATION,
            state="REBASE_WAIT",
            branch="1-repair",
            payload={
                "rebase_reason": "manual",
                "rebase_restart_base_sha": aborted.value["base_sha"],
                "rebase_restart_head_sha": aborted.value["head_sha"],
            },
        )
        ctx = StageContext(
            PipelineConfig(org="acme", repos=["repository"]),
            "acme",
            False,
            FakeStageGitHub(
                pr_state={
                    "state": "OPEN",
                    "headRefOid": case.original,
                    "autoMergeRequest": None,
                    "baseRefName": "main",
                }
            ),
            SimpleNamespace(
                repo_root=case.job.capability_target.repository_root, worktree=case.binding.cwd
            ),
        )
        signing = Mock()
        signing.environment.return_value = {"GIT_CONFIG_GLOBAL": os.devnull}
        backend = Mock(backend_id="hdiutil-v1")
        pool = WorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=queue.Queue(),
            lock_dir=tmp_path / "locks",
            host_capabilities=WorkerCapabilities(backend, "fresh-abort-retry", signing),
            rebase_policy_selector=lambda repo: RebaseValidationPolicy(
                "hephaestus-adr-v1", lambda path: None, ()
            ),
        )
        monkeypatch.setattr(
            pool,
            "_git_fetch_main",
            lambda job: JobResult(
                ok=True, value={"head_sha": "9" * 40 if base_changed else case.base}
            ),
        )
        monkeypatch.setattr(
            pool,
            "_read_remote_branch_head",
            lambda cwd, **kwargs: case.base if kwargs["branch"] == "main" else case.original,
        )
        try:
            discovery = stage.step(item, ctx)
            assert isinstance(discovery, JobRequest), discovery
            assert isinstance(discovery.job, git_jobs.GitJob)
            assert discovery.job.op == "discover_pending_rebase"
            read = pool._run_git(discovery.job)
            assert read.ok, read
            assert read.value["rebase_recovery_candidate"] is None
            stage.on_job_done(item, read, ctx)
            preparation = stage.step(item, ctx)
            assert isinstance(preparation, JobRequest), preparation
            assert isinstance(preparation.job, AgentJob)
            stage.on_job_done(item, JobResult(ok=True), ctx)
            item.state = str(preparation.on_done_state)
            transition = stage.step(item, ctx)
            assert isinstance(transition, Continue), transition
            item.state = str(transition.next_state)
            request = stage.step(item, ctx)
            assert isinstance(request, JobRequest), request
            assert isinstance(request.job, git_jobs.GitJob)
            job = request.job
            assert job.rebase_recovery_candidate is None
            assert job.capability_target is not None
            assert job.capability_target.request_id != prior_id
            assert job.workspace == case.binding
            assert job.kwargs["expected_base_sha"] == case.base
            target = job.capability_target

            def retry(**kwargs: Any) -> bool:
                assert kwargs["preserve_conflicts"] is True
                store = _store(case.manager.common_dir)
                assert store.read(1, prior_id) == terminal
                intent = store.read(1, target.request_id)
                assert intent.phase == "intent" and intent.request == target
                case.starts.append(True)
                conflict = subprocess.run(
                    ["git", "-c", "commit.gpgsign=false", "rebase", case.base],
                    cwd=case.binding.cwd,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                assert conflict.returncode == 1
                return False

            monkeypatch.setattr(git_utils, "rebase_worktree_onto", retry)
            result = pool._run_git(job)
            assert not result.ok
            retained = _store(case.manager.common_dir).read(1, job.capability_target.request_id)
            if base_changed:
                assert result.error == "main changed before conflict restart"
                assert retained is None
                assert case.starts == [True]
            else:
                assert result.error == "mechanical rebase hit conflicts; resolution required"
                assert retained.phase == "intent"
                assert retained.request.request_id != prior_id
                assert case.starts == [True, True]
            assert _store(case.manager.common_dir).read(1, prior_id) == terminal
            assert case.aborts == [1]
            backend.preflight.assert_not_called()
            case.pushes.assert_not_called()
        finally:
            pool.shutdown()


def _restart_publication_stage(case: Any, operation: str, remote: str) -> tuple[Any, Any, Any]:
    """Create a new entry without source or conflict state from the previous process."""
    from types import SimpleNamespace

    from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.stages import ImplementationStage, StageContext
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    stage = ImplementationStage()
    item = WorkItem(
        "repository",
        ItemKind.ISSUE,
        issue=1,
        pr=case.job.capability_target.pr_number,
        stage=StageName.IMPLEMENTATION,
        state="REBASE_WAIT" if operation == "initial" else "REBASE_CONTINUE_WAIT",
        branch=case.job.kwargs["branch"],
        payload={"rebase_reason": case.job.kwargs["rebase_reason"]},
    )
    ctx = StageContext(
        PipelineConfig(org="acme", repos=["repository"]),
        "acme",
        False,
        FakeStageGitHub(
            pr_state={
                "state": "OPEN",
                "headRefOid": remote,
                "autoMergeRequest": None,
                "baseRefName": "main",
            }
        ),
        SimpleNamespace(
            repo_root=case.job.capability_target.repository_root, worktree=case.binding.cwd
        ),
    )
    return stage, item, ctx


def _restart_publication_remote(
    case: Any, outcome: str, record: Any, seen: list[str], remote_reads: list[str]
) -> Any:
    """Return the controlled authenticated remote observation for one attempt."""
    from hephaestus.automation.pipeline.jobs import JobResult

    def read_remote(cwd: Path, **kwargs: Any) -> str | JobResult:
        assert cwd == record.resulting_workspace.cwd
        assert kwargs["branch"] == record.branch
        assert kwargs["expected_repo"] == record.request.repository
        remote_reads.append("read")
        if outcome.startswith("read_error"):
            return JobResult(
                ok=False,
                error="Controlled authenticated remote read failure.",
                stdout_tail="Controlled remote probe output.",
                stderr_tail="Controlled remote probe diagnostic.",
                value=(
                    {"failure_kind": "remote_authentication"}
                    if outcome == "read_error_auth"
                    else None
                ),
                interrupted=outcome == "read_error_interrupted",
            )
        if outcome in {"remote_b", "bare_pending_b"}:
            return str(record.resulting_workspace.revision)
        if outcome == "other" or (outcome == "remote_drift" and "semantic" in seen):
            return "9" * 40
        return case.original

    return read_remote


def _change_restart_source_identity(case: Any, outcome: str, path: Path) -> None:
    """Change actual test Git identity after semantic validation has started."""
    from tests.unit.automation.test_source_worktree import _git

    commands = {
        "branch_drift": ("branch", "-m", "1-changed-during-validation"),
        "head_drift": ("reset", "--hard", case.original),
    }
    command = commands.get(outcome)
    if command is not None:
        _git(path, *command)


def _restart_metadata_result(outcome: str, binding: Any, argv: list[str], result: Any) -> Any:
    """Expose the actual unsigned test commit for the metadata failure control."""
    import subprocess

    from tests.unit.automation.test_source_worktree import _git

    if outcome != "metadata_failure":
        return result
    raw = _git(binding.cwd, "cat-file", "-p", argv[3])
    assert "\ngpgsig " not in f"\n{raw}"
    return subprocess.CompletedProcess(argv, 0, raw, "")


def _restart_publication_checks(
    case: Any, outcome: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, list[str], list[str]]:
    """Use new providers and change selected evidence during fresh checks."""
    import queue
    from unittest.mock import Mock

    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline.host_capabilities import (
        QUOTA_AVAILABLE_TOKEN,
        HostCapabilityReceipt,
        WorkerCapabilities,
    )
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
    from hephaestus.automation.pipeline.worker_pool import WorkerPool

    seen: list[str] = []
    remote_reads: list[str] = []
    store = _store(case.manager.common_dir)
    record = store.read(1, case.job.capability_target.request_id)
    binding = record.resulting_workspace

    def available(target: Any, *, deadline: Any) -> Any:
        assert deadline.remaining() > 0
        assert target.source_head_sha == binding.revision
        assert target.request.workspace == binding
        assert target.request.request_id != record.request.request_id
        seen.append("quota")
        return HostCapabilityReceipt(
            True,
            QUOTA_AVAILABLE_TOKEN,
            None,
            "scratch",
            "f" * 32,
            target=target,
            cleanup_state="complete",
        )

    def execute(job: Any) -> Any:
        assert job.expected_head_sha == binding.revision
        assert job.immutable_source is True
        seen.append("structural")
        return JobResult(ok=True, value={"head_sha": binding.revision, "immutable_source": True})

    def semantic(path: Path) -> None:
        assert path == binding.cwd
        seen.append("semantic")
        if outcome == "semantic_failure":
            raise ValueError("Controlled semantic failure on unchanged B.")
        if outcome == "source_drift":
            (path / "writer.txt").write_text("Changed after source admission.\n")
        if outcome == "record_drift":
            store.write(replace(record, phase="complete"), expected=record)
        _change_restart_source_identity(case, outcome, path)

    signing = Mock()
    signing.environment.return_value = {"GIT_CONFIG_GLOBAL": os.devnull}
    backend = Mock(backend_id="hdiutil-v1", preflight=Mock(side_effect=available))
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(backend, "fresh-publication-retry", signing),
        rebase_policy_selector=lambda repo: RebaseValidationPolicy(
            "hephaestus-adr-v1", semantic, ("test.py",)
        ),
    )
    run = git_utils.run

    def metadata(argv: list[str], **kwargs: Any) -> Any:
        result = run(argv, **kwargs)
        if argv[:3] == ["git", "cat-file", "-p"]:
            assert argv[3] == (case.original if outcome == "head_drift" else binding.revision)
            seen.append("metadata")
            return _restart_metadata_result(outcome, binding, argv, result)
        return result

    def reject_replay(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Recovery must not repeat a rebase or continuation.")

    monkeypatch.setattr(git_utils, "run", metadata)
    monkeypatch.setattr(git_utils, "rebase_worktree_onto", reject_replay)
    monkeypatch.setattr(pool, "_continue_rebase_process", reject_replay)
    monkeypatch.setattr(pool, "_run_immutable_build_test", execute)
    monkeypatch.setattr(
        pool,
        "_read_remote_branch_head",
        _restart_publication_remote(case, outcome, record, seen, remote_reads),
    )
    monkeypatch.setattr(
        pool, "_git_fetch_main", lambda job: JobResult(ok=True, value={"head_sha": case.base})
    )
    return pool, seen, remote_reads


@pytest.mark.parametrize("operation", ["initial", "continued"])
@pytest.mark.parametrize(
    "outcome",
    [
        "remote_a",
        "remote_b",
        "other",
        "read_error",
        "read_error_auth",
        "read_error_interrupted",
        "bare_pending_b",
        "remote_drift",
        "source_drift",
        "record_drift",
    ],
)
def test_uncertain_publication_restart_revalidates_the_final_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, outcome: str
) -> None:
    """Recover real retained operations without replay or cached publication authority."""
    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline.stages import JobRequest

    with _abort_worker_case(
        tmp_path,
        monkeypatch,
        fallback=False,
        fault="clean",
        conflict=operation == "continued",
        structural=True,
    ) as case:
        first_checks = _publication_validation_seams(case, operation, monkeypatch)
        job = _normal_publication_job(case, operation)
        operation_id = case.job.capability_target.request_id
        attempts: list[str] = []

        def uncertain_push(*args: Any, **kwargs: Any) -> None:
            retained = _store(case.manager.common_dir).read(1, operation_id)
            assert retained.phase == "publication_intent"
            assert args[1] == case.original
            attempts.append(kwargs["source_sha"])
            raise git_utils.BranchPublicationRemoteProbeError(failure_kind="timeout")

        module = import_module("hephaestus.automation.rebase_recovery")
        write = module._write_receipt

        def fail_intent(*args: Any, **kwargs: Any) -> Any:
            if json.loads(args[-1])["phase"] == "publication_intent":
                raise OSError("Controlled intent write failure.")
            return write(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(git_utils, "push_head_to_branch", uncertain_push)
            if outcome == "bare_pending_b":
                patch.setattr(module, "_write_receipt", fail_intent)
            failed = case.pool._run_git(job)
        assert not failed.ok, failed
        assert first_checks[:2] == ["quota", "structural"]
        store = _store(case.manager.common_dir)
        retained = store.read(1, operation_id)
        assert retained.phase == (
            "pending_validation" if outcome == "bare_pending_b" else "publication_intent"
        )
        resulting = retained.resulting_workspace.revision
        assert attempts == ([] if outcome == "bare_pending_b" else [resulting])
        case.pool.shutdown()
        del case.pool, failed, job
        live_head = resulting if outcome in {"remote_b", "bare_pending_b"} else case.original
        stage, item, ctx = _restart_publication_stage(case, operation, live_head)
        pool, checks, remote_reads = _restart_publication_checks(
            case, outcome, monkeypatch, tmp_path
        )
        retry_pushes: list[str] = []

        def retry_push(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
            assert branch == retained.branch and expected == case.original
            assert cwd == retained.resulting_workspace.cwd
            assert kwargs["source_sha"] == resulting
            assert store.read(1, operation_id).phase == "publication_intent"
            retry_pushes.append(kwargs["source_sha"])

        monkeypatch.setattr(git_utils, "push_head_to_branch", retry_push)
        try:
            discovery = stage.step(item, ctx)
            assert isinstance(discovery, JobRequest), discovery
            assert isinstance(discovery.job, git_jobs.GitJob)
            assert discovery.job.op == "discover_pending_rebase"
            discovered = pool._run_git(discovery.job)
            assert discovered.ok, discovered
            assert discovered.value["rebase_recovery_candidate"] == operation_id
            stage.on_job_done(item, discovered, ctx)
            submitted = stage.step(item, ctx)
            assert isinstance(submitted, JobRequest), submitted
            assert isinstance(submitted.job, git_jobs.GitJob)
            assert submitted.job.rebase_recovery_candidate == operation_id
            assert submitted.job.workspace == retained.resulting_workspace
            result = pool._run_git(submitted.job)
        finally:
            pool.shutdown()
        _assert_publication_restart_result(
            case, outcome, result, checks, remote_reads, retry_pushes, retained
        )


def _assert_publication_restart_result(
    case: Any,
    outcome: str,
    result: Any,
    checks: list[str],
    remote_reads: list[str],
    retry_pushes: list[str],
    prior: Any,
) -> None:
    """Keep completion, external writes, and retained evidence distinct."""
    current = _store(case.manager.common_dir).read(1, prior.request.request_id)
    assert case.starts == [True]
    assert current.request == prior.request
    assert current.remote_head_sha == case.original
    assert current.resulting_workspace == prior.resulting_workspace
    assert current.resulting_tree_sha == prior.resulting_tree_sha
    assert "review_audit" not in (result.value or {})
    if outcome in {"remote_a", "remote_b"}:
        assert result.ok, result
        assert checks == ["quota", "structural", "semantic", "metadata"]
        assert len(remote_reads) >= 2
        assert current.phase == "complete"
        assert retry_pushes == (
            [prior.resulting_workspace.revision] if outcome == "remote_a" else []
        )
    else:
        assert not result.ok, result
        assert retry_pushes == []
        expected_phase = "complete" if outcome == "record_drift" else prior.phase
        assert current.phase == expected_phase
        if outcome.startswith("read_error"):
            assert result.error == "Controlled authenticated remote read failure."
            assert result.stdout_tail == "Controlled remote probe output."
            assert result.stderr_tail == "Controlled remote probe diagnostic."
            assert result.value["failure_kind"] == (
                "remote_authentication" if outcome == "read_error_auth" else "validation_runner"
            )
            assert result.interrupted is (outcome == "read_error_interrupted")
            assert result.value["source_workspace"] == prior.resulting_workspace.to_dict()
            assert current == prior
            assert checks == []
        if outcome.endswith("_drift"):
            assert checks == ["quota", "structural", "semantic"]
            assert result.value["failure_kind"] == "validation_runner"
            assert (
                result.value["capability_receipt"].target.source_head_sha
                == prior.resulting_workspace.revision
            )
            execution = result.value["structural_execution_receipt"]
            assert execution.ok and execution.value["immutable_source"] is True
            assert execution.value["head_sha"] == prior.resulting_workspace.revision


def _observe_fixture_owner(owner: Any, errors: list[str]) -> Any:
    """Keep the original owner failure and expose its bounded cause chain."""

    def observed(*args: Any, **kwargs: Any) -> Any:
        try:
            return owner(*args, **kwargs)
        except Exception as error:
            cause: BaseException | None = error
            while cause is not None and len(errors) < 8:
                errors.append(
                    f"{type(cause).__name__}: {cause!r}; stderr={getattr(cause, 'stderr', None)!r}"
                )
                cause = cause.__cause__
            raise

    return observed


@contextmanager
def _automatic_local_worker_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, conflict: bool, reserved: bool = False
) -> Any:
    """Create the automatic start journal through the real local writer owner."""
    import queue
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import Mock

    from hephaestus.automation import git_utils, worktree_manager
    from hephaestus.automation.pipeline import worker_pool
    from hephaestus.automation.pipeline.host_capabilities import WorkerCapabilities
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.test_source_worktree import _git

    nonce = "e" * 32
    branch = f"1-auto-impl-direct-{nonce}" if reserved else "1-repair"
    previous_umask = os.umask(0o022)
    try:
        root, first, base = _repository(tmp_path, origin_repository="acme/repository")
        _git(root, "checkout", "-b", "recovery-input", first)
        writer_path = "tracked.txt" if conflict else "writer.txt"
        (root / writer_path).write_text("Automatic writer input.\n", encoding="utf-8")
        (root / "test.py").write_text("# Structural input.\n", encoding="utf-8")
        _git(root, "add", writer_path, "test.py")
        _git(root, "commit", "-s", "-m", "automatic writer input")
        original = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "recovery-input" if reserved else "main")
        _git(root, "update-ref", "refs/remotes/origin/main", original)
        _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        manager = SourceWorkspaceManager(root, repository="repository")
        manager.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    finally:
        os.umask(previous_umask)
    assert original != base
    assert _git(root, "branch", "--list", branch) == ""
    assert manager._read_receipt(1, SourceLane.IMPLEMENTATION) is None
    registered_run = _registered_git_fixture(root, monkeypatch)
    _source_registration_fixture(root, monkeypatch)
    git_run = git_utils.run
    remote_reads: list[str] = []
    remote_state: dict[str, Any] = {"head": None, "reservations": []}

    def remote_absence(argv: list[str], **kwargs: Any) -> Any:
        if "push" in argv:
            assert reserved
            tail = argv[argv.index("push") :]
            assert tail == [
                "push",
                "--no-verify",
                f"--force-with-lease=refs/heads/{branch}:",
                "origin",
                f"{original}:refs/heads/{branch}",
            ]
            assert kwargs["cwd"] == root
            assert remote_state["head"] is None
            remote_state["reservations"].append(original)
            remote_state["head"] = original
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "ls-remote" in argv:
            tail = argv[argv.index("ls-remote") :]
            assert tail in (
                ["ls-remote", "--refs", "origin", f"refs/heads/{branch}"],
                ["ls-remote", "--heads", "origin", branch],
                ["ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
            )
            assert kwargs["cwd"] == root
            remote_reads.append(branch)
            output = (
                f"{remote_state['head']}\trefs/heads/{branch}\n"
                if remote_state["head"] is not None
                else ""
            )
            return subprocess.CompletedProcess(argv, 0, output, "")
        assert "fetch" not in argv and "push" not in argv
        return git_run(argv, **kwargs)

    def registered_remote(argv: list[str], **kwargs: Any) -> Any:
        if "ls-remote" in argv or "fetch" in argv or "push" in argv:
            return remote_absence(argv, **kwargs)
        return registered_run(argv, **kwargs)

    monkeypatch.setattr(git_utils, "run", remote_absence)
    monkeypatch.setattr(worktree_manager, "run", registered_remote)
    monkeypatch.setattr(
        worker_pool, "_trusted_gh_executable", lambda extra_path_root=None: "/usr/bin/gh"
    )
    reserve = Mock(side_effect=AssertionError("Local creation must not reserve a remote branch."))
    publish = Mock(side_effect=AssertionError("Local creation must not publish."))
    if not reserved:
        monkeypatch.setattr(git_utils, "reserve_remote_branch_if_absent", reserve)
    monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
    signing = Mock()
    signing.environment.return_value = {"GIT_CONFIG_GLOBAL": os.devnull}
    backend = Mock(backend_id="hdiutil-v1")
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(backend, "automatic-local", signing),
        rebase_policy_selector=lambda repo: RebaseValidationPolicy(
            "hephaestus-adr-v1", lambda path: None, ("test.py",)
        ),
    )
    create = git_jobs.GitJob(
        "repository",
        "create_worktree",
        60,
        expected_repository="acme/repository",
        kwargs={
            "repo_root": str(root),
            "issue_number": 1,
            "branch_name": branch,
            "source_lane": "impl",
            "record_initial_creation": True,
            **({"base_sha": original, "direct_worktree_nonce": nonce} if reserved else {}),
        },
    )
    transition_errors: list[str] = []
    authorize = SourceWorkspaceManager.authorize_direct_implementation_writer_transition
    creation_errors: list[str] = []
    monkeypatch.setattr(
        SourceWorkspaceManager,
        "authorize_direct_implementation_writer_transition",
        _observe_fixture_owner(authorize, transition_errors),
    )
    monkeypatch.setattr(
        worktree_manager.WorktreeManager,
        "create_worktree",
        _observe_fixture_owner(worktree_manager.WorktreeManager.create_worktree, creation_errors),
    )
    try:
        created = pool._run_git(create)
        assert created.ok, (created, transition_errors, creation_errors)
        binding = WorkspaceBinding.from_dict(created.value["source_workspace"])
        assert binding.revision == original
        assert _git(binding.cwd, "symbolic-ref", "--short", "HEAD") == branch
        assert created.value["fresh_branch_created"] is True
        reservation = created.value.get("direct_scope_reservation")
        assert reservation == ({"branch": branch, "base_sha": original} if reserved else None)
        bound = replace(
            create,
            kwargs={**create.kwargs, "cwd": binding.cwd, "branch": branch},
        )
        start_path, identity = pool._initial_start_identity(bound)
        pending = pool._initial_record_matches(start_path.with_suffix(".pending.json"), identity)
        assert pending is not None and pending["head_sha"] == original
        assert not start_path.exists()
        receipt = manager._require_receipt(1, SourceLane.IMPLEMENTATION)
        assert receipt.to_binding(root) == binding
        assert manager._physical_matches_receipt(receipt)
        assert _store(manager.common_dir).candidate(1) is None
        target = CapabilityRequestTarget(
            "acme/repository",
            1,
            None,
            root,
            binding.cwd,
            original,
            "rebase",
            "scratch",
            "a" * 32,
            workspace=binding,
            generation=1,
        )
        job = git_jobs.GitJob(
            "repository",
            "rebase",
            60,
            expected_repository="acme/repository",
            workspace=binding,
            capability_target=target,
            kwargs={
                "repo_root": str(root),
                "cwd": binding.cwd,
                "issue_number": 1,
                "pr_number": None,
                "branch": branch,
                "rebase_reason": "implementation_start",
                "expected_head_sha": original,
                "expected_remote_sha": original,
                "publish_rebased_head": False,
                "expected_base_sha": base,
                "direct_scope_reservation": reservation,
            },
        )
        starts: list[bool] = []

        def rebase(**kwargs: Any) -> bool:
            retained = _store(manager.common_dir).read(1, target.request_id)
            assert retained.phase == "intent" and retained.request == target
            starts.append(kwargs["preserve_conflicts"])
            completed = subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "rebase", base],
                cwd=binding.cwd,
                check=False,
                capture_output=True,
                text=True,
            )
            assert completed.returncode == 1, completed
            return False

        def remote_head(cwd: Path, **kwargs: Any) -> str | JobResult:
            assert cwd == binding.cwd
            assert kwargs["branch"] in {"main", branch}
            assert kwargs["expected_repo"] == "acme/repository"
            if kwargs["branch"] == branch:
                if reserved:
                    assert isinstance(remote_state["head"], str)
                    return remote_state["head"]
                return JobResult(ok=False, error="cannot verify remote writer head")
            return base

        monkeypatch.setattr(git_utils, "rebase_worktree_onto", rebase)
        monkeypatch.setattr(pool, "_read_remote_branch_head", remote_head)
        monkeypatch.setattr(
            pool, "_git_fetch_main", lambda job: JobResult(ok=True, value={"head_sha": base})
        )
        yield SimpleNamespace(
            pool=pool,
            job=job,
            manager=manager,
            binding=binding,
            original=original,
            receipt=receipt,
            backend=backend,
            starts=starts,
            pushes=publish,
            base=base,
            reservation=reservation,
            remote_state=remote_state,
        )
    finally:
        pool.shutdown()
    assert remote_reads
    reserve.assert_not_called()
    publish.assert_not_called()


def test_automatic_local_start_uses_real_fresh_creation_without_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the real fresh-start fixture separate from rebase behavior evidence."""
    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=True) as case:
        assert case.starts == []
        case.backend.preflight.assert_not_called()


def test_reserved_initial_start_carries_the_actual_creation_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep actual reservation authority with the source and pending-start receipts."""
    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=False, reserved=True) as case:
        assert case.remote_state == {"head": case.original, "reservations": [case.original]}
        assert case.job.kwargs["direct_scope_reservation"] == case.reservation
        assert case.reservation == {
            "branch": "1-auto-impl-direct-" + "e" * 32,
            "base_sha": case.original,
        }
        assert case.job.kwargs["branch"] == case.reservation["branch"]
        assert case.job.kwargs["publish_rebased_head"] is False
        assert case.job.kwargs["rebase_reason"] == "implementation_start"
        assert case.starts == []
        case.backend.preflight.assert_not_called()


@pytest.mark.parametrize(
    "scenario",
    [
        "legacy",
        "local",
        "partial",
        "missing_reservation",
        "flag_none",
        "flag_integer",
        "head_changed",
        "reason_changed",
        "pr_contradiction",
        "reservation_malformed",
        "reserved",
        "reserved_changed",
        "reserved_absent",
        "pr",
        "pr_changed",
        "pr_read_error",
    ],
)
def test_conflict_validation_requires_explicit_local_publication_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Keep remote checks unless complete local context proves the original source."""
    import queue
    from unittest.mock import Mock

    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.worker_pool import WorkerPool

    original = "a" * 40
    kwargs: dict[str, Any] = {
        "cwd": tmp_path,
        "branch": "1-repair",
        "remote": "origin",
        "base_sha": "b" * 40,
        "expected_remote_sha": original,
        "conflict_paths": ["tracked.txt"],
        "conflict_snapshot": {"tracked.txt": {}},
        "conflict_index_snapshot": "c" * 64,
        "paused_head_sha": "d" * 40,
    }
    context: dict[str, Any] = {
        "publish_rebased_head": False,
        "expected_head_sha": original,
        "rebase_reason": "implementation_start",
        "pr_number": None,
        "direct_scope_reservation": None,
    }
    changes: dict[str, dict[str, Any]] = {
        "flag_none": {"publish_rebased_head": None},
        "flag_integer": {"publish_rebased_head": 0},
        "head_changed": {"expected_head_sha": "e" * 40},
        "reason_changed": {"rebase_reason": "unknown"},
        "pr_contradiction": {"pr_number": 1001},
        "reservation_malformed": {"direct_scope_reservation": {"branch": "other"}},
    }
    context.update(changes.get(scenario, {}))
    if scenario.startswith("reserved"):
        context["direct_scope_reservation"] = {"branch": "1-repair", "base_sha": original}
    if scenario in {"pr", "pr_changed", "pr_read_error"}:
        context.update(publish_rebased_head=True, pr_number=1001, rebase_reason="manual")
    if scenario == "partial":
        context = {"publish_rebased_head": False}
    if scenario == "missing_reservation":
        context.pop("direct_scope_reservation")
    if scenario != "legacy":
        kwargs.update(context)
    observed: str | JobResult = original
    if scenario in {"reserved_changed", "pr_changed"}:
        observed = "e" * 40
    if scenario in {"reserved_absent", "pr_read_error"}:
        observed = JobResult(ok=False, error="cannot verify remote writer head")
    remote = Mock(return_value=observed)
    classify = Mock(
        return_value=JobResult(ok=True, value={"conflict_resolution": "resolved_content"})
    )
    continuation = Mock(side_effect=AssertionError("Classification must not continue a rebase."))
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    monkeypatch.setattr(pool, "_read_remote_branch_head", remote)
    monkeypatch.setattr(pool, "_classify_rebase_conflict_edits", classify)
    monkeypatch.setattr(pool, "_continue_rebase_process", continuation)
    try:
        result = pool._git_validate_rebase_conflict(
            git_jobs.GitJob(
                "repository",
                "validate_rebase_conflict",
                60,
                expected_repository="acme/repository",
                kwargs=kwargs,
            )
        )
    finally:
        pool.shutdown()
    allowed = scenario in {"legacy", "local", "reserved", "pr"}
    assert result.ok is allowed, result
    assert classify.call_count == int(allowed)
    if scenario == "local":
        remote.assert_not_called()
    if scenario in {
        "legacy",
        "reserved",
        "reserved_changed",
        "reserved_absent",
        "pr",
        "pr_changed",
        "pr_read_error",
    }:
        remote.assert_called_once_with(
            tmp_path,
            remote="origin",
            branch="1-repair",
            expected_repo="acme/repository",
            timeout=60,
        )
    continuation.assert_not_called()


def _assert_reserved_quota_restart(
    case: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Retry the actual reserved operation through fresh stage and worker owners."""
    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline.stages import JobRequest
    from hephaestus.automation.pipeline.stages.repo import DIRECT_SCOPE_RESERVATION_KEY

    case.pool.shutdown()
    del case.pool
    case.manager = SourceWorkspaceManager(
        case.job.capability_target.repository_root, repository="repository"
    )
    operation_id = case.job.capability_target.request_id
    retained = _store(case.manager.common_dir).read(1, operation_id)
    stage, item, ctx = _restart_publication_stage(case, "initial", case.original)
    item.payload[DIRECT_SCOPE_RESERVATION_KEY] = dict(case.reservation)
    pool, checks, reads = _restart_publication_checks(case, "remote_a", monkeypatch, tmp_path)
    pushes: list[str] = []

    def publish(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
        assert branch == case.reservation["branch"]
        assert expected == case.reservation["base_sha"] == case.original
        assert cwd == retained.resulting_workspace.cwd
        assert kwargs["source_sha"] == retained.resulting_workspace.revision
        intent = _store(case.manager.common_dir).read(1, operation_id)
        assert intent.phase == "publication_intent"
        assert intent.remote_head_sha == case.original
        pushes.append(kwargs["source_sha"])

    monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
    try:
        discovery = stage.step(item, ctx)
        assert isinstance(discovery, JobRequest), discovery
        assert isinstance(discovery.job, git_jobs.GitJob)
        assert discovery.job.op == "discover_pending_rebase"
        discovered = pool._run_git(discovery.job)
        assert discovered.ok, discovered
        assert discovered.value["rebase_recovery_candidate"] == operation_id
        stage.on_job_done(item, discovered, ctx)
        submitted = stage.step(item, ctx)
        assert isinstance(submitted, JobRequest), submitted
        assert isinstance(submitted.job, git_jobs.GitJob)
        assert submitted.job.workspace == retained.resulting_workspace
        assert submitted.job.rebase_recovery_candidate == operation_id
        assert submitted.job.kwargs["direct_scope_reservation"] == case.reservation
        assert submitted.job.kwargs["rebase_reason"] == "implementation_start"
        result = pool._run_git(submitted.job)
        start_path, identity = pool._initial_start_identity(submitted.job)
        started = pool._initial_record_matches(start_path, identity)
    finally:
        pool.shutdown()
    assert result.ok, result
    assert checks == ["quota", "structural", "semantic", "metadata"]
    assert reads
    assert pushes == [retained.resulting_workspace.revision]
    assert started is not None and started["head_sha"] == retained.resulting_workspace.revision
    complete = _store(case.manager.common_dir).read(1, operation_id)
    assert complete.phase == "complete"
    assert complete.request == retained.request
    assert complete.remote_head_sha == case.original
    assert case.starts == [True]
    assert "review_audit" not in result.value


@pytest.mark.parametrize("scenario", ["quota_restart_a", "bare_remote_b"])
def test_reserved_rebase_retains_original_publication_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Keep the actual reserved A and reject B without prior durable publication intent."""
    from hephaestus.automation.pipeline.host_capabilities import (
        QUOTA_UNAVAILABLE_TOKEN,
        HostCapabilityReceipt,
    )
    from tests.unit.automation.test_source_worktree import _git

    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=False, reserved=True) as case:
        checks = _publication_validation_seams(case, "initial", monkeypatch)
        available = case.backend.preflight.side_effect
        start_path, _identity = case.pool._initial_start_identity(case.job)
        operation_id = case.job.capability_target.request_id
        probed: list[str] = []

        def preflight(target: Any, *, deadline: Any) -> Any:
            assert target.source_head_sha != case.original
            assert target.source_head_sha == _git(case.binding.cwd, "rev-parse", "HEAD")
            pending = _store(case.manager.common_dir).read(1, operation_id)
            assert pending.phase == "pending_validation"
            assert not start_path.with_suffix(".reservation.json").exists()
            probed.append(target.source_head_sha)
            if scenario == "quota_restart_a":
                return HostCapabilityReceipt(
                    False,
                    QUOTA_UNAVAILABLE_TOKEN,
                    "backend",
                    "scratch",
                    "b" * 32,
                    target=target,
                    cleanup_state="not_started",
                )
            case.remote_state["head"] = target.source_head_sha
            return available(target, deadline=deadline)

        case.backend.preflight.side_effect = preflight
        result = case.pool._run_git(case.job)
        retained = _store(case.manager.common_dir).read(1, operation_id)
        assert probed == [retained.resulting_workspace.revision]
        assert case.manager._require_receipt(1, SourceLane.IMPLEMENTATION).revision == probed[0]
        assert retained.request == case.job.capability_target
        assert case.starts == [True]
        case.pushes.assert_not_called()
        if scenario == "bare_remote_b":
            assert checks[:2] == ["quota", "structural"]
            assert not result.ok, result
            assert retained.phase == "pending_validation"
            assert not start_path.exists()
            return
        assert not result.ok and result.value["failure_kind"] == "validation_runner"
        assert checks == []
        assert not start_path.exists()
        del result
        _assert_reserved_quota_restart(case, monkeypatch, tmp_path)


def _reserved_first_publication(
    case: Any, operation: str, uncertain: bool, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """Use the real initial or continued owner before the controlled remote write."""
    from types import SimpleNamespace

    from hephaestus.automation import git_utils

    checks = _publication_validation_seams(case, operation, monkeypatch)
    continuations: list[str] = []
    continue_process = case.pool._continue_rebase_process

    def continue_once(cwd: Path, **kwargs: Any) -> Any:
        continuations.append(str(cwd))
        return continue_process(cwd, **kwargs)

    monkeypatch.setattr(case.pool, "_continue_rebase_process", continue_once)
    job = _normal_publication_job(case, operation)
    assert job.kwargs["direct_scope_reservation"] == case.reservation
    start_path, identity = case.pool._initial_start_identity(job)
    operation_id = case.job.capability_target.request_id
    pushes: list[str] = []

    def publish(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
        record = _store(case.manager.common_dir).read(1, operation_id)
        assert record.phase == "publication_intent"
        assert record.remote_head_sha == expected == case.original
        assert branch == case.reservation["branch"] == record.branch
        assert cwd == record.resulting_workspace.cwd
        assert kwargs["source_sha"] == record.resulting_workspace.revision
        assert not start_path.exists()
        pushes.append(kwargs["source_sha"])
        if uncertain:
            raise git_utils.BranchPublicationRemoteProbeError(failure_kind="timeout")
        case.remote_state["head"] = kwargs["source_sha"]

    monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
    result = case.pool._run_git(job)
    record = _store(case.manager.common_dir).read(1, operation_id)
    assert record.resulting_workspace is not None, result
    assert pushes == [record.resulting_workspace.revision], result
    assert checks[:2] == ["quota", "structural"]
    assert case.starts == [True]
    assert len(continuations) == int(operation == "continued")
    assert record.request == case.job.capability_target
    assert record.remote_head_sha == case.original
    assert result.value["source_workspace"] == record.resulting_workspace.to_dict()
    assert result.value["capability_receipt"].target.source_head_sha == pushes[0]
    assert result.value["structural_execution_receipt"].value["head_sha"] == pushes[0]
    assert "review_audit" not in result.value
    return SimpleNamespace(
        result=result,
        record=record,
        start_path=start_path,
        identity=identity,
        pushes=pushes,
        continuations=continuations,
    )


@pytest.mark.parametrize("operation", ["initial", "continued"])
def test_reserved_initial_and_continued_publication_complete_after_start_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Publish reserved A once and save the real start before recovery completes."""
    with _automatic_local_worker_case(
        tmp_path, monkeypatch, conflict=operation == "continued", reserved=True
    ) as case:
        first = _reserved_first_publication(case, operation, False, monkeypatch)
        assert first.result.ok, first.result
        assert first.record.phase == "complete"
        started = case.pool._initial_record_matches(first.start_path, first.identity)
        assert started is not None
        assert started["head_sha"] == first.record.resulting_workspace.revision
        assert first.result.value["implementation_started"] is True
        assert case.remote_state["head"] == started["head_sha"]


def _reserved_restart_job(case: Any, operation: str, pool: Any, *, completed: bool = False) -> Any:
    """Discover the actual retained source without prior callback or conflict state."""
    from hephaestus.automation.pipeline.stages import JobRequest
    from hephaestus.automation.pipeline.stages.repo import DIRECT_SCOPE_RESERVATION_KEY

    stage, item, ctx = _restart_publication_stage(case, operation, case.original)
    item.payload[DIRECT_SCOPE_RESERVATION_KEY] = dict(case.reservation)
    discovery = stage.step(item, ctx)
    assert isinstance(discovery, JobRequest), discovery
    assert isinstance(discovery.job, git_jobs.GitJob)
    assert discovery.job.op == "discover_pending_rebase"
    discovered = pool._run_git(discovery.job)
    assert discovered.ok, discovered
    candidate = None if completed else case.job.capability_target.request_id
    assert discovered.value["rebase_recovery_candidate"] == candidate
    stage.on_job_done(item, discovered, ctx)
    submitted = stage.step(item, ctx)
    assert isinstance(submitted, JobRequest), submitted
    assert isinstance(submitted.job, git_jobs.GitJob)
    assert submitted.job.kwargs["direct_scope_reservation"] == case.reservation
    assert submitted.job.kwargs["rebase_reason"] == "implementation_start"
    assert submitted.job.rebase_recovery_candidate == candidate
    record = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
    assert submitted.job.workspace == record.resulting_workspace
    return submitted.job


@pytest.mark.parametrize("operation", ["initial", "continued"])
@pytest.mark.parametrize("outcome", ["remote_a", "remote_b", "other", "read_error"])
def test_reserved_uncertain_publication_restart_preserves_original_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, outcome: str
) -> None:
    """Reconcile only an actual publication intent after fresh source discovery and checks."""
    from hephaestus.automation import git_utils

    with _automatic_local_worker_case(
        tmp_path, monkeypatch, conflict=operation == "continued", reserved=True
    ) as case:
        first = _reserved_first_publication(case, operation, True, monkeypatch)
        assert not first.result.ok, first.result
        assert first.result.value["failure_kind"] == "validation_runner"
        assert first.record.phase == "publication_intent"
        assert not first.start_path.exists()
        case.pool.shutdown()
        del case.pool, first.result
        case.manager = SourceWorkspaceManager(
            case.job.capability_target.repository_root, repository="repository"
        )
        pool, checks, reads = _restart_publication_checks(case, outcome, monkeypatch, tmp_path)
        retry_pushes: list[str] = []

        def retry_push(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
            assert outcome == "remote_a"
            assert branch == first.record.branch and expected == case.original
            assert cwd == first.record.resulting_workspace.cwd
            assert kwargs["source_sha"] == first.record.resulting_workspace.revision
            current = _store(case.manager.common_dir).read(1, first.record.request.request_id)
            assert current == first.record
            assert not first.start_path.exists()
            retry_pushes.append(kwargs["source_sha"])

        monkeypatch.setattr(git_utils, "push_head_to_branch", retry_push)
        try:
            job = _reserved_restart_job(case, operation, pool)
            result = pool._run_git(job)
            started = pool._initial_record_matches(first.start_path, first.identity)
        finally:
            pool.shutdown()
        _assert_reserved_restart_outcome(case, first, outcome, result, checks, reads, retry_pushes)
        if outcome in {"remote_a", "remote_b"}:
            assert started is not None
            assert started["head_sha"] == first.record.resulting_workspace.revision
        else:
            assert started is None


def _reserved_quota_failure(case: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Retain actual B without authorizing or attempting reserved publication."""
    from types import SimpleNamespace

    from hephaestus.automation.pipeline.host_capabilities import (
        QUOTA_UNAVAILABLE_TOKEN,
        HostCapabilityReceipt,
    )
    from tests.unit.automation.test_source_worktree import _git

    checks = _publication_validation_seams(case, "initial", monkeypatch)
    start_path, identity = case.pool._initial_start_identity(case.job)
    probes: list[str] = []

    def unavailable(target: Any, *, deadline: Any) -> Any:
        assert deadline.remaining() > 0
        assert target.source_head_sha == _git(case.binding.cwd, "rev-parse", "HEAD")
        assert target.source_head_sha != case.original
        probes.append(target.source_head_sha)
        return HostCapabilityReceipt(
            False,
            QUOTA_UNAVAILABLE_TOKEN,
            "backend",
            "scratch",
            "b" * 32,
            target=target,
            cleanup_state="not_started",
        )

    case.backend.preflight.side_effect = unavailable
    result = case.pool._run_git(case.job)
    assert not result.ok and result.value["failure_kind"] == "validation_runner", result
    record = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
    assert record.phase == "pending_validation"
    assert probes == [record.resulting_workspace.revision]
    assert record.request == case.job.capability_target
    assert record.remote_head_sha == case.original
    assert checks == []
    assert not start_path.exists()
    assert not start_path.with_suffix(".reservation.json").exists()
    case.pushes.assert_not_called()
    return SimpleNamespace(result=result, record=record, start_path=start_path, identity=identity)


def _reserved_admission_job(job: Any, scenario: str, reservation: dict[str, Any]) -> Any:
    """Change one submitted field without replacing source or durable operation evidence."""
    changes: dict[str, dict[str, Any]] = {
        "lease_none": {"expected_remote_sha": None},
        "lease_foreign": {"expected_remote_sha": "9" * 40},
        "reservation_changed": {"direct_scope_reservation": {**reservation, "base_sha": "9" * 40}},
        "source_head_changed": {"expected_head_sha": "9" * 40},
        "branch_changed": {"branch": "1-other-writer"},
    }
    kwargs = {**job.kwargs, **changes.get(scenario, {})}
    if scenario == "reservation_missing":
        kwargs.pop("direct_scope_reservation")
    return replace(job, kwargs=kwargs)


@pytest.mark.parametrize(
    "scenario",
    [
        "lease_absent",
        "lease_none",
        "lease_foreign",
        "reservation_missing",
        "reservation_changed",
        "source_head_changed",
        "branch_changed",
        "bare_remote_b",
    ],
)
def test_reserved_fresh_admission_rejects_changed_authority_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Keep explicit invalid authority distinct from an absent optional lease field."""
    from hephaestus.automation import git_utils

    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=False, reserved=True) as case:
        first = (
            _reserved_quota_failure(case, monkeypatch)
            if scenario == "bare_remote_b"
            else _reserved_first_publication(case, "initial", True, monkeypatch)
        )
        assert not first.result.ok, first.result
        assert not first.start_path.exists()
        case.pool.shutdown()
        del case.pool, first.result
        case.manager = SourceWorkspaceManager(
            case.job.capability_target.repository_root, repository="repository"
        )
        outcome = "remote_b" if scenario == "bare_remote_b" else "remote_a"
        pool, checks, reads = _restart_publication_checks(case, outcome, monkeypatch, tmp_path)
        pushes: list[str] = []

        def publish(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
            assert scenario == "lease_absent"
            assert branch == first.record.branch and expected == case.original
            assert cwd == first.record.resulting_workspace.cwd
            assert kwargs["source_sha"] == first.record.resulting_workspace.revision
            current = _store(case.manager.common_dir).read(1, first.record.request.request_id)
            assert current == first.record
            pushes.append(kwargs["source_sha"])

        monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
        try:
            submitted = _reserved_restart_job(case, "initial", pool)
            assert "expected_remote_sha" not in submitted.kwargs
            job = _reserved_admission_job(submitted, scenario, case.reservation)
            result = pool._run_git(job)
            started = pool._initial_record_matches(first.start_path, first.identity)
        finally:
            pool.shutdown()
        current = _store(case.manager.common_dir).read(1, first.record.request.request_id)
        assert case.starts == [True]
        assert current.request == first.record.request
        assert current.remote_head_sha == case.original
        receipt = case.manager._require_receipt(1, SourceLane.IMPLEMENTATION)
        assert receipt.to_binding(case.manager.repo_root) == first.record.resulting_workspace
        if scenario == "lease_absent":
            assert result.ok, result
            assert checks == ["quota", "structural", "semantic", "metadata"]
            assert reads
            assert pushes == [first.record.resulting_workspace.revision]
            assert current == replace(first.record, phase="complete")
            assert started is not None
            assert started["head_sha"] == first.record.resulting_workspace.revision
        else:
            assert not result.ok, result
            assert checks == []
            assert pushes == []
            assert current == first.record
            assert started is None
        if scenario == "bare_remote_b":
            assert reads
            assert current.phase == "pending_validation"
            assert not first.start_path.with_suffix(".reservation.json").exists()
        assert not isinstance(result.value, dict) or "review_audit" not in result.value


def _assert_reserved_validation_drift(case: Any, record: Any, scenario: str) -> None:
    """Prove the injected fault occurred and report the actual durable state."""
    from tests.unit.automation.test_source_worktree import _git

    path = record.resulting_workspace.cwd
    current = _store(case.manager.common_dir).read(1, record.request.request_id)
    assert current == (replace(record, phase="complete") if scenario == "record_drift" else record)
    if scenario == "source_drift":
        assert (path / "writer.txt").read_text() == "Changed after source admission.\n"
        assert _git(path, "status", "--porcelain")
    if scenario == "branch_drift":
        assert _git(path, "branch", "--show-current") == "1-changed-during-validation"
    if scenario == "head_drift":
        assert _git(path, "rev-parse", "HEAD") == case.original
        assert case.original != record.resulting_workspace.revision
        assert _git(path, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "scenario", ["source_drift", "record_drift", "remote_drift", "branch_drift", "head_drift"]
)
def test_reserved_restart_rejects_drift_after_fresh_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Reject changed evidence at the final boundary without another mutation or publication."""
    from unittest.mock import Mock

    from hephaestus.automation import git_utils

    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=False, reserved=True) as case:
        first = _reserved_first_publication(case, "initial", True, monkeypatch)
        assert not first.result.ok and first.record.phase == "publication_intent"
        assert not first.start_path.exists()
        case.pool.shutdown()
        del case.pool, first.result
        case.manager = SourceWorkspaceManager(
            case.job.capability_target.repository_root, repository="repository"
        )
        pool, checks, reads = _restart_publication_checks(case, scenario, monkeypatch, tmp_path)
        publish = Mock()
        monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
        try:
            job = _reserved_restart_job(case, "initial", pool)
            result = pool._run_git(job)
            started = pool._initial_record_matches(first.start_path, first.identity)
        finally:
            pool.shutdown()
        assert checks[:3] == ["quota", "structural", "semantic"]
        _assert_reserved_validation_drift(case, first.record, scenario)
        if scenario == "remote_drift":
            assert len(reads) >= 2
        assert not result.ok, result
        publish.assert_not_called()
        assert started is None
        assert case.starts == [True]
        assert first.pushes == [first.record.resulting_workspace.revision]
        assert result.value.get("failure_kind") == "validation_runner", result
        assert "review_audit" not in result.value
        assert (
            result.value["capability_receipt"].target.source_head_sha
            == first.record.resulting_workspace.revision
        )
        assert (
            result.value["structural_execution_receipt"].value["head_sha"]
            == first.record.resulting_workspace.revision
        )


@pytest.mark.parametrize("failure", ["semantic_failure", "metadata_failure"])
def test_reserved_resume_preserves_source_failure_on_unchanged_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Keep real source validation failures distinct from changed admission evidence."""
    from unittest.mock import Mock

    from hephaestus.automation import git_utils
    from tests.unit.automation.test_source_worktree import _git

    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=False, reserved=True) as case:
        first = _reserved_first_publication(case, "initial", True, monkeypatch)
        assert not first.result.ok and first.record.phase == "publication_intent"
        case.pool.shutdown()
        del case.pool, first.result
        case.manager = SourceWorkspaceManager(
            case.job.capability_target.repository_root, repository="repository"
        )
        pool, checks, reads = _restart_publication_checks(case, failure, monkeypatch, tmp_path)
        publish = Mock()
        monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
        try:
            job = _reserved_restart_job(case, "initial", pool)
            result = pool._run_git(job)
            started = pool._initial_record_matches(first.start_path, first.identity)
        finally:
            pool.shutdown()
        assert not result.ok, result
        assert result.value.get("failure_kind") not in {
            "validation_runner",
            "signing_configuration",
        }
        assert checks == (
            ["quota", "structural", "semantic"]
            if failure == "semantic_failure"
            else ["quota", "structural", "semantic", "metadata"]
        )
        if failure == "semantic_failure":
            assert result.value["failure_kind"] == "semantic_validation"
            assert "Controlled semantic failure on unchanged B." in result.error
        else:
            assert result.error == "completed rebase commit metadata invalid"
        assert reads
        publish.assert_not_called()
        assert started is None
        current = _store(case.manager.common_dir).read(1, first.record.request.request_id)
        assert current == first.record
        path = first.record.resulting_workspace.cwd
        assert _git(path, "rev-parse", "HEAD") == first.record.resulting_workspace.revision
        assert _git(path, "rev-parse", "HEAD^{tree}") == first.record.resulting_tree_sha
        _assert_reserved_failure_evidence(case, result, first.record)


def _assert_reserved_failure_evidence(case: Any, result: Any, record: Any) -> None:
    """Keep the real source and completed checks when a later boundary fails."""
    assert case.starts == [True]
    assert result.value["head_sha"] == record.resulting_workspace.revision
    assert result.value["source_workspace"] == record.resulting_workspace.to_dict()
    receipt = case.manager._require_receipt(1, SourceLane.IMPLEMENTATION)
    assert result.value["source_receipt"] == receipt.to_dict()
    assert (
        result.value["capability_receipt"].target.source_head_sha
        == record.resulting_workspace.revision
    )
    execution = result.value["structural_execution_receipt"]
    assert execution.ok and execution.value["immutable_source"] is True
    assert execution.value["head_sha"] == record.resulting_workspace.revision
    assert "review_audit" not in result.value


def _reserved_persistence_faults(
    case: Any, outcome: str, patch: pytest.MonkeyPatch, start_path: Path, faults: list[str]
) -> None:
    """Fail one actual persistence boundary without changing the publication owner."""
    from hephaestus.automation.pipeline import worker_pool
    from hephaestus.io.utils import write_secure

    module = import_module("hephaestus.automation.rebase_recovery")
    read, write = module._read_record, module._write_receipt
    match = case.pool._initial_record_matches
    record_phase = "complete" if outcome.startswith("complete_") else "publication_intent"
    record_prefix = "complete" if outcome.startswith("complete_") else "intent"
    path_prefix = "start" if outcome.startswith("start_") else "transition"
    selected_path = (
        start_path if path_prefix == "start" else start_path.with_suffix(".reservation.json")
    )

    def fail_once(boundary: str) -> None:
        if outcome == boundary and not faults:
            faults.append(boundary)
            raise OSError(f"Controlled reserved {boundary} failure.")

    def write_record(*args: Any, **kwargs: Any) -> Any:
        if json.loads(args[-1])["phase"] == record_phase:
            fail_once(f"{record_prefix}_write")
        return write(*args, **kwargs)

    def read_record(*args: Any, **kwargs: Any) -> Any:
        value = read(*args, **kwargs)
        if value is not None and value[0].phase == record_phase:
            fail_once(f"{record_prefix}_readback")
        return value

    def save(path: Path, contents: str, **kwargs: Any) -> None:
        if path == selected_path:
            fail_once(f"{path_prefix}_write")
        write_secure(path, contents, **kwargs)

    def read_transition(path: Path, identity: dict[str, object]) -> Any:
        value = match(path, identity)
        if path == selected_path and value is not None:
            fail_once(f"{path_prefix}_readback")
        return value

    patch.setattr(module, "_write_receipt", write_record)
    patch.setattr(module, "_read_record", read_record)
    patch.setattr(worker_pool, "write_secure", save)
    patch.setattr(case.pool, "_initial_record_matches", read_transition)


@pytest.mark.parametrize(
    "outcome", ["intent_write", "intent_readback", "transition_write", "transition_readback"]
)
def test_reserved_prepublication_persistence_failure_retains_actual_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Inspect actual durable state after a one-shot fault and retry with fresh owners."""
    from unittest.mock import Mock

    from hephaestus.automation import git_utils

    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=False, reserved=True) as case:
        checks = _publication_validation_seams(case, "initial", monkeypatch)
        start_path, identity = case.pool._initial_start_identity(case.job)
        faults: list[str] = []
        publish = Mock()
        with monkeypatch.context() as patch:
            patch.setattr(git_utils, "push_head_to_branch", publish)
            _reserved_persistence_faults(case, outcome, patch, start_path, faults)
            result = case.pool._run_git(case.job)
        assert faults == [outcome]
        assert not result.ok, result
        assert result.value["failure_kind"] == "validation_runner"
        assert checks[:2] == ["quota", "structural"]
        publish.assert_not_called()
        assert case.remote_state["head"] == case.original
        record = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert record.phase == (
            "pending_validation" if outcome == "intent_write" else "publication_intent"
        )
        assert record.request == case.job.capability_target
        assert record.remote_head_sha == case.original
        assert case.pool._initial_record_matches(start_path, identity) is None
        transition = case.pool._initial_record_matches(
            start_path.with_suffix(".reservation.json"),
            {**identity, "reservation_base_sha": case.original},
        )
        assert (transition is not None) is (outcome == "transition_readback")
        if transition is not None:
            assert transition["head_sha"] == record.resulting_workspace.revision
        _assert_reserved_failure_evidence(case, result, record)
        del result
        _assert_reserved_quota_restart(case, monkeypatch, tmp_path)


def _reserved_postpublication_restart(
    case: Any,
    record: Any,
    start_path: Path,
    identity: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reconcile actual remote B or use verified completed-start evidence without replay."""
    from unittest.mock import Mock

    from hephaestus.automation import git_utils

    case.pool.shutdown()
    del case.pool
    case.manager = SourceWorkspaceManager(
        case.job.capability_target.repository_root, repository="repository"
    )
    completed = record.phase == "complete"
    pool, checks, reads = _restart_publication_checks(case, "remote_b", monkeypatch, tmp_path)
    publish = Mock()
    monkeypatch.setattr(git_utils, "push_head_to_branch", publish)
    try:
        job = _reserved_restart_job(case, "initial", pool, completed=completed)
        result = pool._run_git(job)
        started = pool._initial_record_matches(start_path, identity)
    finally:
        pool.shutdown()
    assert result.ok, result
    assert reads
    assert checks == ([] if completed else ["quota", "structural", "semantic", "metadata"])
    publish.assert_not_called()
    assert case.starts == [True]
    assert result.value["implementation_started"] is True
    assert result.value["published"] is False
    assert result.value["source_workspace"] == record.resulting_workspace.to_dict()
    if completed:
        assert result.value["rebased"] is False
        assert "capability_receipt" not in result.value
        assert "structural_execution_receipt" not in result.value
    else:
        _assert_reserved_failure_evidence(case, result, record)
    assert "review_audit" not in result.value
    assert started is not None and started["head_sha"] == record.resulting_workspace.revision
    current = _store(case.manager.common_dir).read(1, record.request.request_id)
    assert current == replace(record, phase="complete")
    assert current.request == record.request and current.remote_head_sha == case.original


@pytest.mark.parametrize(
    "outcome", ["start_write", "start_readback", "complete_write", "complete_readback"]
)
def test_reserved_postpublication_persistence_failure_recovers_without_repush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Preserve physical state after successful publication and a failed persistence boundary."""
    from hephaestus.automation import git_utils

    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=False, reserved=True) as case:
        checks = _publication_validation_seams(case, "initial", monkeypatch)
        start_path, identity = case.pool._initial_start_identity(case.job)
        operation_id = case.job.capability_target.request_id
        faults: list[str] = []
        pushes: list[str] = []

        def publish(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
            record = _store(case.manager.common_dir).read(1, operation_id)
            assert record.phase == "publication_intent"
            assert record.remote_head_sha == expected == case.original
            assert branch == record.branch == case.reservation["branch"]
            assert cwd == record.resulting_workspace.cwd
            assert kwargs["source_sha"] == record.resulting_workspace.revision
            assert not start_path.exists()
            assert case.remote_state["head"] == case.original
            pushes.append(kwargs["source_sha"])
            case.remote_state["head"] = kwargs["source_sha"]

        with monkeypatch.context() as patch:
            patch.setattr(git_utils, "push_head_to_branch", publish)
            _reserved_persistence_faults(case, outcome, patch, start_path, faults)
            result = case.pool._run_git(case.job)
        assert faults == [outcome]
        assert not result.ok, result
        assert result.value["failure_kind"] == "validation_runner"
        assert checks[:2] == ["quota", "structural"]
        record = _store(case.manager.common_dir).read(1, operation_id)
        assert record.phase == (
            "complete" if outcome == "complete_readback" else "publication_intent"
        )
        assert record.request == case.job.capability_target
        assert record.remote_head_sha == case.original
        assert pushes == [record.resulting_workspace.revision]
        assert case.remote_state["head"] == record.resulting_workspace.revision
        started = case.pool._initial_record_matches(start_path, identity)
        assert (started is not None) is (outcome != "start_write")
        if started is not None:
            assert started["head_sha"] == record.resulting_workspace.revision
        transition = case.pool._initial_record_matches(
            start_path.with_suffix(".reservation.json"),
            {**identity, "reservation_base_sha": case.original},
        )
        assert transition is not None
        assert transition["head_sha"] == record.resulting_workspace.revision
        _assert_reserved_failure_evidence(case, result, record)
        del result
        _reserved_postpublication_restart(case, record, start_path, identity, monkeypatch, tmp_path)
        assert pushes == [record.resulting_workspace.revision]


def _assert_reserved_restart_outcome(
    case: Any,
    first: Any,
    outcome: str,
    result: Any,
    checks: list[str],
    reads: list[str],
    pushes: list[str],
) -> None:
    """Keep remote reconciliation separate from a second publication or review proof."""
    accepted = outcome in {"remote_a", "remote_b"}
    assert result.ok is accepted, result
    assert reads
    assert checks == (["quota", "structural", "semantic", "metadata"] if accepted else [])
    assert pushes == ([first.record.resulting_workspace.revision] if outcome == "remote_a" else [])
    current = _store(case.manager.common_dir).read(1, first.record.request.request_id)
    assert current == (replace(first.record, phase="complete") if accepted else first.record)
    assert current.remote_head_sha == case.original
    assert case.starts == [True]
    assert result.value["source_workspace"] == first.record.resulting_workspace.to_dict()
    assert "review_audit" not in result.value
    if accepted:
        assert result.value["implementation_started"] is True
        assert (
            result.value["capability_receipt"].target.source_head_sha
            == first.record.resulting_workspace.revision
        )
        assert (
            result.value["structural_execution_receipt"].value["head_sha"]
            == first.record.resulting_workspace.revision
        )
    else:
        assert result.value["failure_kind"] == "validation_runner"


def _resume_local_start(
    case: Any,
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fresh_entry: bool = False,
) -> None:
    """Recover B after a failed start save with new process owners and checks."""
    from hephaestus.automation.pipeline.stages import JobRequest

    case.pool.shutdown()
    del case.pool
    stage, item, ctx = _restart_publication_stage(case, operation, case.original)
    pool, checks, remote_reads = _restart_publication_checks(
        case, "remote_a", monkeypatch, tmp_path
    )
    operation_id = case.job.capability_target.request_id
    retained = _store(case.manager.common_dir).read(1, operation_id)
    try:
        if fresh_entry:
            _discover_local_restart_from_enter(stage, item, ctx, pool)
        discovery = stage.step(item, ctx)
        assert isinstance(discovery, JobRequest), discovery
        assert isinstance(discovery.job, git_jobs.GitJob)
        assert discovery.job.op == "discover_pending_rebase"
        discovered = pool._run_git(discovery.job)
        assert discovered.ok, discovered
        assert discovered.value["rebase_recovery_candidate"] == operation_id
        stage.on_job_done(item, discovered, ctx)
        submitted = stage.step(item, ctx)
        assert isinstance(submitted, JobRequest), submitted
        assert isinstance(submitted.job, git_jobs.GitJob)
        assert submitted.job.rebase_recovery_candidate == operation_id
        assert submitted.job.workspace == retained.resulting_workspace
        assert submitted.job.kwargs["rebase_reason"] == (
            "implementation_start" if fresh_entry else case.job.kwargs["rebase_reason"]
        )
        result = pool._run_git(submitted.job)
        start_path, identity = pool._initial_start_identity(submitted.job)
        started = pool._initial_record_matches(start_path, identity)
    finally:
        pool.shutdown()
    assert result.ok, result
    assert checks == ["quota", "structural", "semantic", "metadata"]
    assert remote_reads == []
    assert started is not None
    assert started["head_sha"] == retained.resulting_workspace.revision
    complete = _store(case.manager.common_dir).read(1, operation_id)
    assert complete == replace(retained, phase="complete")
    assert case.starts == [True]
    assert "review_audit" not in result.value


def _discover_local_restart_from_enter(stage: Any, item: Any, ctx: Any, pool: Any) -> None:
    """Find retained local work without a restored stage or source payload."""
    from hephaestus.automation.pipeline.stages import Continue, JobRequest
    from hephaestus.automation.state_labels import STATE_PLAN_GO
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    item.state = "ENTER"
    item.payload.clear()
    item.branch = None
    item.worktree = None
    ctx = replace(ctx, github=FakeStageGitHub(labels=[STATE_PLAN_GO]))
    assert stage.on_enter(item, ctx) is None
    for _ in range(8):
        if item.state in {"REBASE_WAIT", "REBASE_CONTINUE_WAIT"}:
            return
        result = stage.step(item, ctx)
        if isinstance(result, Continue):
            item.state = result.next_state
            continue
        assert isinstance(result, JobRequest), result
        assert isinstance(result.job, git_jobs.GitJob), result
        assert result.job.op in {"discover_first_publication", "discover_pending_rebase"}
        completed = pool._run_git(result.job)
        assert completed.ok, completed
        item.state = result.on_done_state
        stage.on_job_done(item, completed, ctx)
    pytest.fail("Fresh entry did not reach the retained rebase owner.")


@pytest.mark.parametrize("reason", ["manual", "implementation_start"])
@pytest.mark.parametrize("operation", ["initial", "continued"])
@pytest.mark.parametrize(
    "outcome",
    ["success", "start_write", "complete_write", "complete_readback", "restart", "fresh_restart"],
)
def test_local_rebase_completion_follows_the_initial_start_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, outcome: str, reason: str
) -> None:
    """Complete local B only after its existing start owner saves success."""
    from hephaestus.automation.pipeline import worker_pool
    from hephaestus.io.utils import write_secure

    fixture = (
        _automatic_local_worker_case(tmp_path, monkeypatch, conflict=operation == "continued")
        if reason == "implementation_start"
        else _abort_worker_case(
            tmp_path,
            monkeypatch,
            fallback=False,
            fault="clean",
            conflict=operation == "continued",
            structural=True,
            pr_number=None,
        )
    )
    with fixture as case:
        assert case.job.kwargs.get("direct_scope_reservation") is None
        checks = _publication_validation_seams(case, operation, monkeypatch)
        job = _normal_publication_job(case, operation)
        assert job.kwargs["rebase_reason"] == reason
        assert job.kwargs["pr_number"] is None
        assert not job.kwargs["publish_rebased_head"]
        assert job.kwargs.get("direct_scope_reservation") is None
        operation_id = case.job.capability_target.request_id
        start_path, identity = case.pool._initial_start_identity(job)
        module = import_module("hephaestus.automation.rebase_recovery")
        save, write, read = write_secure, module._write_receipt, module._read_record
        events: list[str] = []

        def save_start(path: Path, contents: str, **kwargs: Any) -> Any:
            if path != start_path:
                return save(path, contents, **kwargs)
            pending = _store(case.manager.common_dir).read(1, operation_id)
            assert pending.phase == "pending_validation"
            assert pending.publication_mode == "none" and pending.remote_head_sha is None
            assert json.loads(contents)["head_sha"] == pending.resulting_workspace.revision
            events.append("start")
            if outcome in {"start_write", "restart", "fresh_restart"}:
                raise OSError("Controlled initial-start save failure.")
            return save(path, contents, **kwargs)

        def complete_write(*args: Any, **kwargs: Any) -> Any:
            value = json.loads(args[-1])
            if value["phase"] == "complete":
                started = case.pool._initial_record_matches(start_path, identity)
                assert started is not None
                assert started["head_sha"] == value["resulting_workspace"]["revision"]
                assert events == ["start"]
                events.append("complete")
                if outcome == "complete_write":
                    raise OSError("Controlled local completion write failure.")
            return write(*args, **kwargs)

        def complete_read(*args: Any, **kwargs: Any) -> Any:
            value = read(*args, **kwargs)
            if (
                outcome == "complete_readback"
                and value is not None
                and value[0].phase == "complete"
            ):
                events.append("readback")
                raise ValueError("Controlled local completion readback failure.")
            return value

        with monkeypatch.context() as patch:
            patch.setattr(worker_pool, "write_secure", save_start)
            patch.setattr(module, "_write_receipt", complete_write)
            patch.setattr(module, "_read_record", complete_read)
            result = case.pool._run_git(job)
        retained = _store(case.manager.common_dir).read(1, operation_id)
        assert retained.request == case.job.capability_target
        assert retained.resulting_workspace.revision != case.original
        assert checks[:2] == ["quota", "structural"]
        assert case.starts == [True]
        case.pushes.assert_not_called()
        if outcome in {"restart", "fresh_restart"}:
            assert not result.ok, result
            assert retained.phase == "pending_validation"
            assert events == ["start"]
            del result, job
            _resume_local_start(
                case, operation, monkeypatch, tmp_path, fresh_entry=outcome == "fresh_restart"
            )
            case.pushes.assert_not_called()
        else:
            _assert_local_start_result(case, outcome, result, retained, events)


def _assert_local_start_result(
    case: Any, outcome: str, result: Any, retained: Any, events: list[str]
) -> None:
    """Retain source and execution evidence on each local persistence failure."""
    assert result.ok is (outcome == "success"), result
    assert retained.publication_mode == "none" and retained.remote_head_sha is None
    expected_phase = (
        "complete" if outcome in {"success", "complete_readback"} else "pending_validation"
    )
    assert retained.phase == expected_phase
    assert (
        events
        == {
            "success": ["start", "complete"],
            "start_write": ["start"],
            "complete_write": ["start", "complete"],
            "complete_readback": ["start", "complete", "readback"],
        }[outcome]
    )
    assert result.value["head_sha"] == retained.resulting_workspace.revision
    assert result.value["source_workspace"] == retained.resulting_workspace.to_dict()
    assert (
        result.value["source_receipt"]
        == case.manager._require_receipt(1, SourceLane.IMPLEMENTATION).to_dict()
    )
    assert (
        result.value["capability_receipt"].target.source_head_sha
        == retained.resulting_workspace.revision
    )
    execution = result.value["structural_execution_receipt"]
    assert execution.ok and execution.value["immutable_source"] is True
    assert execution.value["head_sha"] == retained.resulting_workspace.revision
    if not result.ok:
        assert result.value["failure_kind"] == "validation_runner"
    assert "review_audit" not in result.value


def _normal_publication_job(case: Any, operation: str) -> Any:
    """Obtain continuation authority through the existing real conflict owners."""
    from types import SimpleNamespace

    from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.stages import ImplementationStage, StageContext
    from hephaestus.automation.pipeline.stages.repo import DIRECT_SCOPE_RESERVATION_KEY
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    if operation == "initial":
        return case.job
    initial = replace(case.job, kwargs={**case.job.kwargs, "resolve_conflicts": True})
    paused = case.pool._run_git(initial)
    assert paused.error == "mechanical rebase hit conflicts; resolution required", paused
    stage = ImplementationStage()
    item = WorkItem(
        "repository",
        ItemKind.ISSUE,
        issue=1,
        pr=case.job.capability_target.pr_number,
        stage=StageName.IMPLEMENTATION,
        state="REBASE_WAIT",
        branch=case.job.kwargs["branch"],
        worktree=str(case.binding.cwd),
        payload={
            "rebase_reason": case.job.kwargs["rebase_reason"],
            "_impl_source_workspace": case.binding.to_dict(),
            "_impl_source_revision": case.original,
            "_impl_source_receipt": case.receipt,
        },
    )
    if case.job.kwargs.get("direct_scope_reservation") is not None:
        item.payload[DIRECT_SCOPE_RESERVATION_KEY] = dict(
            case.job.kwargs["direct_scope_reservation"]
        )
    ctx = StageContext(
        PipelineConfig(org="acme", repos=["repository"]),
        "acme",
        False,
        FakeStageGitHub(
            pr_state={
                "state": "OPEN",
                "headRefOid": case.original,
                "autoMergeRequest": None,
                "baseRefName": "main",
            }
        ),
        SimpleNamespace(
            repo_root=case.job.capability_target.repository_root, worktree=case.binding.cwd
        ),
    )
    stage.on_job_done(item, paused, ctx)
    return _resolve_real_conflict(stage, item, ctx, case.pool, case.manager)


def _publication_validation_seams(
    case: Any, operation: str, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Control external execution while retaining real source and receipt checks."""
    import subprocess

    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline.host_capabilities import (
        QUOTA_AVAILABLE_TOKEN,
        HostCapabilityReceipt,
    )
    from hephaestus.automation.pipeline.jobs import JobResult
    from tests.unit.automation.test_source_worktree import _git

    seen: list[str] = []

    def available(target: Any, *, deadline: Any) -> Any:
        assert deadline.remaining() > 0
        assert target.source_head_sha == _git(case.binding.cwd, "rev-parse", "HEAD")
        assert target.source_head_sha != case.original
        pending = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert pending.phase == "pending_validation"
        assert pending.resulting_workspace.revision == target.source_head_sha
        seen.append("quota")
        return HostCapabilityReceipt(
            True,
            QUOTA_AVAILABLE_TOKEN,
            None,
            "scratch",
            "e" * 32,
            target=target,
            cleanup_state="complete",
        )

    def execute(job: Any) -> Any:
        assert job.immutable_source is True
        assert job.expected_head_sha == _git(case.binding.cwd, "rev-parse", "HEAD")
        seen.append("structural")
        return JobResult(
            ok=True,
            value={"head_sha": job.expected_head_sha, "immutable_source": True},
        )

    def rebase(**kwargs: Any) -> bool:
        assert kwargs["preserve_conflicts"] is True
        intent = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert intent.phase == "intent"
        case.starts.append(True)
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "rebase", case.base],
            cwd=case.binding.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result
        return True

    run = git_utils.run

    def metadata(argv: list[str], **kwargs: Any) -> Any:
        result = run(argv, **kwargs)
        if argv[:3] == ["git", "cat-file", "-p"]:
            assert argv[3] == _git(case.binding.cwd, "rev-parse", "HEAD")
            seen.append("metadata")
            return subprocess.CompletedProcess(
                argv, 0, result.stdout + "\ngpgsig controlled-test-signature\n", result.stderr
            )
        return result

    case.backend.preflight.side_effect = available
    monkeypatch.setattr(case.pool, "_run_immutable_build_test", execute)
    monkeypatch.setattr(git_utils, "run", metadata)
    if operation == "initial":
        monkeypatch.setattr(git_utils, "rebase_worktree_onto", rebase)
    return seen


@pytest.mark.parametrize("operation", ["initial", "continued"])
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "intent_write",
        "intent_readback",
        "push_failed",
        "push_unknown",
        "complete_write",
        "complete_readback",
    ],
)
def test_normal_rebase_publication_is_durable_before_the_external_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, outcome: str
) -> None:
    """Normal publication must read back intent before push and complete only after success."""
    from hephaestus.automation import git_utils
    from tests.unit.automation.test_source_worktree import _git

    with _abort_worker_case(
        tmp_path,
        monkeypatch,
        fallback=False,
        fault="clean",
        conflict=operation == "continued",
        structural=True,
    ) as case:
        seen = _publication_validation_seams(case, operation, monkeypatch)
        job = _normal_publication_job(case, operation)
        operation_id = case.job.capability_target.request_id
        module = import_module("hephaestus.automation.rebase_recovery")
        read, write = module._read_record, module._write_receipt
        faults: list[str] = []
        pushes: list[tuple[str, str]] = []
        writes: list[str] = []

        def intent_write(*args: Any, **kwargs: Any) -> Any:
            phase = json.loads(args[-1])["phase"]
            writes.append(phase)
            failed_phase = {
                "intent_write": "publication_intent",
                "complete_write": "complete",
            }.get(outcome)
            if phase == failed_phase:
                faults.append(outcome)
                raise OSError(f"Controlled {phase} write failure.")
            return write(*args, **kwargs)

        def intent_read(*args: Any, **kwargs: Any) -> Any:
            value = read(*args, **kwargs)
            failed_phase = {
                "intent_readback": "publication_intent",
                "complete_readback": "complete",
            }.get(outcome)
            if value is not None and value[0].phase == failed_phase:
                faults.append(outcome)
                raise ValueError(f"Controlled {failed_phase} readback failure.")
            return value

        def push(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
            assert branch == "1-repair" and expected == case.original and cwd == case.binding.cwd
            retained = _store(case.manager.common_dir).read(1, operation_id)
            head = _git(cwd, "rev-parse", "HEAD")
            pushes.append((retained.phase, head))
            assert retained.phase == "publication_intent"
            assert retained.remote_head_sha == case.original
            assert retained.resulting_workspace.revision == head == kwargs["source_sha"]
            if outcome == "push_failed":
                raise git_utils.BranchPublicationRemoteHeadUnchangedError(failure_kind="transport")
            if outcome == "push_unknown":
                raise git_utils.BranchPublicationRemoteProbeError(failure_kind="timeout")

        with monkeypatch.context() as patch:
            patch.setattr(module, "_write_receipt", intent_write)
            patch.setattr(module, "_read_record", intent_read)
            patch.setattr(git_utils, "push_head_to_branch", push)
            result = case.pool._run_git(job)
        retained = _store(case.manager.common_dir).read(1, operation_id)
        resulting = _git(case.binding.cwd, "rev-parse", "HEAD")
        assert resulting != case.original
        assert retained.request == case.job.capability_target
        assert retained.resulting_workspace.revision == resulting
        assert case.manager._require_receipt(1, SourceLane.IMPLEMENTATION).revision == resulting
        assert seen[:2] == ["quota", "structural"]
        assert case.starts == [True]
        if outcome.startswith("intent_"):
            assert faults == [outcome]
            assert pushes == []
            assert not result.ok
            assert retained.phase == (
                "pending_validation" if outcome == "intent_write" else "publication_intent"
            )
        elif outcome.startswith("complete_"):
            assert faults == [outcome]
            assert pushes == [("publication_intent", resulting)]
            assert writes.count("publication_intent") == 1
            assert writes.count("complete") == 1
            assert not result.ok
            assert result.value["failure_kind"] == "validation_runner"
            assert retained.phase == (
                "publication_intent" if outcome == "complete_write" else "complete"
            )
            assert retained.remote_head_sha == case.original
            assert retained.resulting_tree_sha == _git(case.binding.cwd, "rev-parse", "HEAD^{tree}")
            assert result.value["head_sha"] == resulting
            assert result.value["source_workspace"] == retained.resulting_workspace.to_dict()
            assert (
                result.value["source_receipt"]
                == case.manager._require_receipt(1, SourceLane.IMPLEMENTATION).to_dict()
            )
            capability = result.value["capability_receipt"]
            assert capability.target.request == job.capability_target
            assert capability.target.source_head_sha == resulting
            execution = result.value["structural_execution_receipt"]
            assert execution.ok
            assert execution.value["head_sha"] == resulting
            assert execution.value["immutable_source"] is True
        else:
            assert pushes == [("publication_intent", resulting)]
            assert result.ok is (outcome == "success")
            assert retained.phase == ("complete" if outcome == "success" else "publication_intent")
        assert "review_audit" not in (result.value or {})


def _record(root: Path, *, phase: str = "intent", operation: str = "rebase") -> Any:
    """Build complete input identity without using filesystem discovery."""
    workspace = WorkspaceBinding.source(
        cwd=root / "writer",
        reusable_root=root,
        repository="repository",
        ownership_key="repository:owned:1:impl",
        item_number=1,
        lane=SourceLane.IMPLEMENTATION,
        revision="a" * 40 if operation == "rebase" else "e" * 40,
        generation=3,
        detached=False,
    )
    request = CapabilityRequestTarget(
        "acme/repository",
        1,
        None,
        root,
        workspace.cwd,
        "a" * 40 if operation == "rebase" else "e" * 40,
        "rebase",
        "scratch",
        "f" * 32,
        workspace=workspace,
        generation=1,
    )
    return git_jobs.PendingRebaseRecord(
        request=request,
        operation=operation,
        scheduler_repository="repository",
        branch="1-capability-repair",
        destination="https://github.com/acme/repository.git",
        publication_mode="existing",
        remote_head_sha="a" * 40,
        target_base_sha="b" * 40,
        policy_name="hephaestus-adr-v1",
        phase=phase,
        resulting_workspace=(
            None if phase == "intent" else replace(workspace, revision="c" * 40, generation=4)
        ),
        resulting_tree_sha=None if phase == "intent" else "d" * 40,
    )


def _store(common_dir: Path, *, deadline: Any = None) -> Any:
    """Use the real protected owner with a controlled operation budget."""
    module = import_module("hephaestus.automation.rebase_recovery")
    return module.PendingRebaseStore(
        common_dir,
        deadline=deadline or _PreparationDeadline(10.0, lambda: 0.0, threading.Event()),
    )


def _competing_store_write(
    common_dir: Path, record: Any, expected: Any, barrier: Any, output: Any
) -> None:
    """Run one real store operation in an independent process."""
    try:
        store = _store(
            common_dir,
            deadline=_PreparationDeadline(
                time.monotonic() + 30.0, time.monotonic, threading.Event()
            ),
        )
        if expected is not None:
            assert (
                store.read(expected.request.issue_number, expected.request.request_id) == expected
            )
        barrier.wait(timeout=30.0)
        try:
            retained = store.write(record, expected=expected)
        except ValueError as error:
            output.send(("rejected", os.getpid(), str(error)))
        else:
            output.send(("written", os.getpid(), retained.to_dict()))
    finally:
        output.close()


def _stop_store_processes(processes: list[Any]) -> None:
    """Stop only the child processes that this test created."""
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=5.0)
        assert not process.is_alive()
        process.close()


def _race_store_writes(common_dir: Path, records: list[Any], expected: Any) -> list[Any]:
    """Release two prepared processes together and bound all waits."""
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    processes: list[Any] = []
    readers: list[Any] = []
    try:
        for record in records:
            reader, writer = context.Pipe(duplex=False)
            readers.append(reader)
            process = context.Process(
                target=_competing_store_write,
                args=(common_dir, record, expected, barrier, writer),
            )
            try:
                process.start()
            finally:
                writer.close()
            processes.append(process)
        barrier.wait(timeout=30.0)
        results = []
        for reader in readers:
            assert reader.poll(30.0), "The store process did not return within its budget."
            results.append(reader.recv())
        for process in processes:
            process.join(timeout=5.0)
            assert process.exitcode == 0
        return results
    finally:
        try:
            _stop_store_processes(processes)
        finally:
            for reader in readers:
                reader.close()


@pytest.mark.parametrize("operation", ["create", "advance"])
def test_pending_store_serializes_competing_processes(tmp_path: Path, operation: str) -> None:
    """Two real processes cannot create two owners or replace the same prior phase."""
    common_dir = tmp_path / "common"
    common_dir.mkdir(mode=0o700)
    intent = _record(tmp_path)
    store = _store(common_dir)
    if operation == "create":
        expected = None
        records = [
            replace(intent, request=replace(intent.request, request_id=token * 32))
            for token in ("1", "2")
        ]
    else:
        expected = store.write(intent, expected=None)
        pending = _record(tmp_path, phase="pending_validation")
        records = [
            replace(
                pending,
                resulting_workspace=replace(pending.resulting_workspace, revision=token * 40),
                resulting_tree_sha=tree * 40,
            )
            for token, tree in (("c", "d"), ("e", "f"))
        ]
    results = _race_store_writes(common_dir, records, expected)
    assert sorted(result[0] for result in results) == ["rejected", "written"]
    assert len({result[1] for result in results}) == 2
    assert all(result[1] != os.getpid() for result in results)
    winner = next(
        record for record, result in zip(records, results, strict=True) if result[0] == "written"
    )
    loser = next(
        record for record, result in zip(records, results, strict=True) if result[0] == "rejected"
    )
    written = next(result[2] for result in results if result[0] == "written")
    assert written == winner.to_dict()
    assert store.read(1, winner.request.request_id) == winner
    directory = common_dir / "hephaestus-source-workspaces" / "pending-rebases"
    paths = list(directory.glob("*.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text()) == winner.to_dict()
    assert winner.remote_head_sha == intent.remote_head_sha
    assert winner.target_base_sha == intent.target_base_sha
    assert winner.request.workspace == intent.request.workspace
    assert (
        replace(
            winner,
            request=intent.request,
            phase="intent",
            resulting_workspace=None,
            resulting_tree_sha=None,
        )
        == intent
    )
    if operation == "create":
        assert store.read(1, loser.request.request_id) is None
        assert winner.phase == "intent"
    else:
        assert winner.request == intent.request
        assert winner.phase == "pending_validation"
        assert store.candidate(1) == winner


def _aborted_record(intent: Any) -> Any:
    """Build terminal restoration data without claiming a resulting rebase head."""
    return replace(
        intent,
        schema_version=2,
        phase="aborted",
        restored_workspace=intent.request.workspace,
        restored_tree_sha="e" * 40,
    )


@pytest.mark.parametrize(
    "phase", ["intent", "pending_validation", "publication_intent", "complete"]
)
def test_abort_schema_preserves_exact_legacy_record_meaning(tmp_path: Path, phase: str) -> None:
    """A schema-1 record retains its original phase without implied abort proof."""
    original = _record(tmp_path, phase=phase)
    payload = original.to_dict()
    payload["schema_version"] = 1
    payload.pop("restored_workspace", None)
    payload.pop("restored_tree_sha", None)
    parsed = type(original).from_dict(payload)
    assert parsed.phase == phase
    assert parsed.schema_version == 1
    assert parsed.to_dict() == payload
    assert parsed.restored_workspace is None
    assert parsed.restored_tree_sha is None


def test_abort_record_separates_restoration_from_validation_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminal abort data is immutable, pure, and separate from completed-B data."""
    intent = _record(tmp_path)
    monkeypatch.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("filesystem read"))
    aborted = _aborted_record(intent)
    assert aborted.request == intent.request
    assert aborted.operation == intent.operation
    assert aborted.restored_workspace == intent.request.workspace
    assert aborted.resulting_workspace is None
    assert aborted.resulting_tree_sha is None
    assert type(aborted).from_dict(aborted.to_dict()) == aborted
    with pytest.raises(FrozenInstanceError):
        aborted.phase = "complete"


@pytest.mark.parametrize(
    "fault",
    ["workspace_absent", "tree_absent", "head", "generation", "owner", "path", "result", "phase"],
)
def test_abort_record_requires_exact_separate_restoration(tmp_path: Path, fault: str) -> None:
    """An abort cannot use missing, foreign, or completed-result source data."""
    aborted = _aborted_record(_record(tmp_path))
    changes: dict[str, Any] = {
        "workspace_absent": {"restored_workspace": None},
        "tree_absent": {"restored_tree_sha": None},
        "head": {"restored_workspace": replace(aborted.restored_workspace, revision="f" * 40)},
        "generation": {"restored_workspace": replace(aborted.restored_workspace, generation=9)},
        "owner": {"restored_workspace": replace(aborted.restored_workspace, ownership_key="other")},
        "path": {"restored_workspace": replace(aborted.restored_workspace, cwd=tmp_path / "other")},
        "result": {
            "resulting_workspace": replace(aborted.request.workspace, revision="c" * 40),
            "resulting_tree_sha": "d" * 40,
        },
        "phase": {"phase": "intent"},
    }
    with pytest.raises(ValueError):
        replace(aborted, **changes[fault])


@pytest.mark.parametrize(
    "fault",
    [
        "legacy_extra",
        "legacy_abort",
        "new_missing",
        "new_legacy_shape",
        "abort_without_restoration",
        "bool_version",
        "future",
    ],
)
def test_abort_schema_rejects_mixed_or_unsupported_shapes(tmp_path: Path, fault: str) -> None:
    """A version marker cannot grant authority to an incomplete or mixed schema."""
    record = _record(tmp_path)
    payload = record.to_dict()
    payload.update(schema_version=1)
    payload.pop("restored_workspace", None)
    payload.pop("restored_tree_sha", None)
    if fault == "legacy_extra":
        payload.update(restored_workspace=None, restored_tree_sha=None)
    elif fault == "legacy_abort":
        payload["phase"] = "aborted"
    elif fault == "new_missing":
        payload.update(schema_version=2, restored_workspace=record.request.workspace.to_dict())
    elif fault == "new_legacy_shape":
        payload["schema_version"] = 2
    elif fault == "abort_without_restoration":
        payload.update(
            schema_version=2, phase="aborted", restored_workspace=None, restored_tree_sha=None
        )
    elif fault == "bool_version":
        payload["schema_version"] = True
    else:
        payload["schema_version"] = 3
    with pytest.raises(ValueError):
        type(record).from_dict(payload)


@pytest.mark.parametrize(
    "field,value",
    [("schema_version", True), ("generation", "3"), ("generation", 3.5), ("detached", 0)],
)
def test_abort_schema_rejects_coerced_restoration_identity(
    tmp_path: Path, field: str, value: Any
) -> None:
    """Restoration fields must retain exact JSON primitive types."""
    record = _aborted_record(_record(tmp_path))
    payload = record.to_dict()
    payload["restored_workspace"][field] = value
    with pytest.raises(ValueError):
        type(record).from_dict(payload)


@pytest.mark.parametrize("mode", ["none", "absent", "existing"])
def test_abort_store_retains_terminal_evidence_without_a_resume_candidate(
    tmp_path: Path, mode: str
) -> None:
    """A read-back abort ends one operation but does not select resumed execution."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    intent = replace(
        _record(tmp_path),
        publication_mode=mode,
        remote_head_sha="a" * 40 if mode == "existing" else None,
    )
    store = _store(common)
    written = store.write(intent, expected=None)
    assert written == intent
    aborted = _aborted_record(intent)
    written = store.write(aborted, expected=intent)
    assert written == aborted
    del store
    restarted = _store(common)
    assert restarted.read(1, intent.request.request_id) == aborted
    assert restarted.candidate(1) is None
    next_intent = replace(intent, request=replace(intent.request, request_id="0" * 32))
    written = restarted.write(next_intent, expected=None)
    assert written == next_intent
    assert restarted.read(1, intent.request.request_id) == aborted
    with pytest.raises(ValueError):
        restarted.candidate(1)


@pytest.mark.parametrize("prior", ["none", "pending_validation", "publication_intent", "complete"])
def test_abort_store_accepts_only_the_exact_intent_transition(tmp_path: Path, prior: str) -> None:
    """An abort cannot retire validation, publication, or a missing operation."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    intent = _record(tmp_path)
    store = _store(common)
    expected = None
    if prior != "none":
        expected = store.write(intent, expected=None)
        for phase in ("pending_validation", "publication_intent", "complete"):
            expected = store.write(_record(tmp_path, phase=phase), expected=expected)
            if phase == prior:
                break
    with pytest.raises(ValueError):
        store.write(_aborted_record(intent), expected=expected)
    assert store.read(1, intent.request.request_id) == expected


@pytest.mark.parametrize("field", ["remote_head_sha", "target_base_sha", "branch"])
def test_abort_store_cas_preserves_original_operation_identity(tmp_path: Path, field: str) -> None:
    """Exact restoration data cannot replace the original remote or base authority."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    intent = _record(tmp_path)
    store = _store(common)
    store.write(intent, expected=None)
    changed = replace(
        _aborted_record(intent), **{field: "other" if field == "branch" else "9" * 40}
    )
    with pytest.raises(ValueError):
        store.write(changed, expected=intent)
    assert store.read(1, intent.request.request_id) == intent


@pytest.mark.parametrize(
    "phase", ["intent", "pending_validation", "publication_intent", "complete"]
)
def test_abort_store_cannot_reactivate_or_complete_a_terminal_abort(
    tmp_path: Path, phase: str
) -> None:
    """A retained abort cannot become a fresh mutation or completed-B result."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    intent = _record(tmp_path)
    store = _store(common)
    store.write(intent, expected=None)
    aborted = _aborted_record(intent)
    store.write(aborted, expected=intent)
    with pytest.raises(ValueError):
        store.write(_record(tmp_path, phase=phase), expected=aborted)
    assert store.read(1, intent.request.request_id) == aborted


@pytest.mark.parametrize("after_write", [False, True], ids=["write_failed", "readback_failed"])
def test_abort_store_failure_reports_no_verified_terminal_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_write: bool
) -> None:
    """Failed persistence does not imply that the physical record stayed unchanged."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    intent = _record(tmp_path)
    aborted = _aborted_record(intent)
    store = _store(common)
    store.write(intent, expected=None)
    module = import_module("hephaestus.automation.rebase_recovery")
    write = module._write_receipt
    read = module._read_record
    calls: list[bool] = []
    readback_failures: list[bool] = []

    def failed_write(*args: Any, **kwargs: Any) -> None:
        calls.append(after_write)
        if after_write:
            write(*args, **kwargs)
            return
        raise OSError("Controlled write failure.")

    def failed_readback(*args: Any, **kwargs: Any) -> Any:
        if calls:
            readback_failures.append(True)
            raise ValueError("Controlled readback failure.")
        return read(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_write_receipt", failed_write)
        patch.setattr(module, "_read_record", failed_readback)
        with pytest.raises((OSError, ValueError)):
            store.write(aborted, expected=intent)
    assert calls == [after_write]
    assert readback_failures == ([True] if after_write else [])
    assert _store(common).read(1, intent.request.request_id) == (aborted if after_write else intent)


@pytest.mark.parametrize("operation", ["rebase", "continue_rebase"])
def test_pending_record_keeps_original_lease_separate_from_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """The pure record binds both input and result without resolving paths."""
    monkeypatch.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("filesystem read"))
    record = _record(tmp_path, phase="pending_validation", operation=operation)
    assert record.remote_head_sha == "a" * 40
    assert record.request.expected_head_sha == ("a" * 40 if operation == "rebase" else "e" * 40)
    assert record.resulting_workspace.revision == "c" * 40
    assert record.resulting_tree_sha == "d" * 40
    assert type(record).from_dict(record.to_dict()) == record
    with pytest.raises(FrozenInstanceError):
        record.phase = "complete"


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"operation": "commit_push"},
        {"scheduler_repository": "foreign"},
        {"branch": "../unsafe"},
        {"destination": "https://github.com/foreign/repository.git"},
        {"destination": "https://credential@github.com/acme/repository.git"},
        {"publication_mode": "unknown"},
        {"publication_mode": "absent"},
        {"publication_mode": "none"},
        {"remote_head_sha": None},
        {"target_base_sha": "b" * 39},
        {"policy_name": "execute-arbitrary-command"},
        {"phase": "review_approved"},
        {"phase": "pending_validation"},
        {"resulting_tree_sha": "d" * 40},
    ],
)
def test_pending_record_rejects_incomplete_or_foreign_identity(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    """Malformed records cannot become recovery authority."""
    record = _record(tmp_path)
    with pytest.raises(ValueError):
        replace(record, **change)


@pytest.mark.parametrize("publication_mode", ["none", "absent", "existing"])
def test_pending_record_distinguishes_publication_modes(
    tmp_path: Path, publication_mode: str
) -> None:
    """No publication and absent-branch creation are not an existing-head lease."""
    record = _record(tmp_path)
    changed = replace(
        record,
        publication_mode=publication_mode,
        remote_head_sha="a" * 40 if publication_mode == "existing" else None,
    )
    assert type(changed).from_dict(changed.to_dict()) == changed
    malformed = changed.to_dict()
    malformed["extra_authority"] = True
    with pytest.raises(ValueError):
        type(changed).from_dict(malformed)


def test_pending_store_retains_durable_phases_and_rejects_stale_advancement(tmp_path: Path) -> None:
    """Only the exact prior record can advance one pending operation."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    intent = _record(tmp_path)
    store = _store(common)
    assert store.read(1, intent.request.request_id) is None
    written = store.write(intent, expected=None)
    assert written == intent
    pending = _record(tmp_path, phase="pending_validation")
    written = store.write(pending, expected=intent)
    assert written == pending
    del store
    restarted = _store(common)
    assert restarted.read(1, intent.request.request_id) == pending
    assert restarted.candidate(1) == pending
    publishing = replace(pending, phase="publication_intent")
    with pytest.raises(ValueError):
        restarted.write(publishing, expected=intent)
    assert restarted.read(1, intent.request.request_id) == pending
    written = restarted.write(publishing, expected=pending)
    assert written == publishing
    complete = replace(publishing, phase="complete")
    written = restarted.write(complete, expected=publishing)
    assert written == complete
    assert restarted.candidate(1) is None
    assert restarted.read(1, intent.request.request_id) == complete
    assert "review" not in complete.to_dict()


@pytest.mark.parametrize("creation_umask", [0o022, 0o002])
def test_pending_store_uses_owned_source_parent_and_private_namespace(
    tmp_path: Path, creation_umask: int
) -> None:
    """Normal source-manager state needs no permission change for private recovery."""
    prior_umask = os.umask(creation_umask)
    try:
        root, _, revision = _repository(tmp_path, origin_repository="repository")
        manager = SourceWorkspaceManager(root, repository="repository")
        manager._write_receipt(
            SourceWorkspaceReceipt(
                repository=manager.repository,
                repository_identity=manager.repository_identity,
                ownership_key=manager.ownership_key(1, SourceLane.IMPLEMENTATION),
                item_number=1,
                lane=SourceLane.IMPLEMENTATION,
                path=manager.path_for(1, SourceLane.IMPLEMENTATION),
                revision=revision,
                generation=1,
                detached=False,
                branch="1-capability-repair",
            )
        )
    finally:
        os.umask(prior_umask)
    original_mode = stat.S_IMODE(manager.state_dir.stat().st_mode)
    assert original_mode == 0o777 & ~creation_umask
    store = _store(manager.common_dir)
    record = _record(root)
    if creation_umask == 0o002:
        with pytest.raises(ValueError):
            store.write(record, expected=None)
        assert stat.S_IMODE(manager.state_dir.stat().st_mode) == original_mode
        return
    written = store.write(record, expected=None)
    assert written == record
    assert stat.S_IMODE(manager.state_dir.stat().st_mode) == original_mode
    namespace = manager.state_dir / "pending-rebases"
    assert stat.S_IMODE(namespace.stat().st_mode) == 0o700
    records = list(namespace.glob("*.json"))
    assert len(records) == 1
    assert stat.S_IMODE(records[0].stat().st_mode) == 0o600


@pytest.mark.parametrize("fault", ["parent_symlink", "namespace_symlink", "public_namespace"])
def test_pending_store_rejects_unsafe_namespace(tmp_path: Path, fault: str) -> None:
    """Record access cannot traverse a symlink or use public recovery state."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    parent = common / "hephaestus-source-workspaces"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if fault == "parent_symlink":
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.mkdir(mode=0o700)
        namespace = parent / "pending-rebases"
        if fault == "namespace_symlink":
            namespace.symlink_to(outside, target_is_directory=True)
        else:
            namespace.mkdir(mode=0o755)
            namespace.chmod(0o755)
    record = _record(tmp_path)
    with pytest.raises((OSError, ValueError)):
        _store(common).write(record, expected=None)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("fault", ["symlink", "public", "oversized", "malformed", "foreign"])
def test_pending_store_rejects_changed_record_without_overwriting_it(
    tmp_path: Path, fault: str
) -> None:
    """A damaged or foreign record stays available for operator inspection."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    store = _store(common)
    store.write(record, expected=None)
    namespace = common / "hephaestus-source-workspaces" / "pending-rebases"
    path = next(namespace.glob("*.json"))
    if fault == "symlink":
        original = path.read_bytes()
        path.unlink()
        external = tmp_path / "external.json"
        external.write_bytes(original)
        path.symlink_to(external)
    elif fault == "public":
        path.chmod(0o644)
    elif fault == "oversized":
        path.write_bytes(b" " * (128 * 1024))
    elif fault == "malformed":
        path.write_text("{invalid", encoding="utf-8")
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["destination"] = "https://github.com/foreign/repository.git"
        path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises((OSError, ValueError)):
        store.read(1, record.request.request_id)
    with pytest.raises((OSError, ValueError)):
        store.write(_record(tmp_path, phase="pending_validation"), expected=record)
    assert path.read_bytes() == before


def test_pending_store_readback_failure_cannot_report_advancement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable write without exact readback leaves evidence but grants no success."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    store = _store(common)
    actual_read = os.read

    def altered_read(descriptor: int, length: int) -> bytes:
        value = actual_read(descriptor, length)
        return b"!" + value[1:] if value else value

    monkeypatch.setattr(os, "read", altered_read)
    with pytest.raises(ValueError):
        store.write(record, expected=None)
    monkeypatch.setattr(os, "read", actual_read)
    assert store.read(1, record.request.request_id) == record


def test_pending_store_expired_budget_does_not_create_an_intent(tmp_path: Path) -> None:
    """Expired work cannot start a durable operation or wait for its lock."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    deadline = _PreparationDeadline(1.0, lambda: 2.0, threading.Event())
    record = _record(tmp_path)
    with pytest.raises(SourceWorkspacePreparationError) as stopped:
        _store(common, deadline=deadline).write(record, expected=None)
    assert stopped.value.cause is SourceWorkspacePreparationCause.GIT_TIMEOUT
    assert list(common.rglob("*.json")) == []


@pytest.mark.parametrize("mode", [0o755, 0o775, 0o777])
def test_pending_store_accepts_only_nonwritable_owned_parents(tmp_path: Path, mode: int) -> None:
    """Public read permission is distinct from permission to replace private state."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    parent = common / "hephaestus-source-workspaces"
    parent.mkdir(mode=mode)
    parent.chmod(mode)
    record = _record(tmp_path)
    if mode == 0o755:
        written = _store(common).write(record, expected=None)
        assert written == record
    else:
        with pytest.raises(ValueError):
            _store(common).write(record, expected=None)
    assert stat.S_IMODE(parent.stat().st_mode) == mode


def test_pending_store_rejects_conflicting_intents_and_skipped_phases(tmp_path: Path) -> None:
    """A second operation cannot replace an unfinished mutation or skip its checks."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    store = _store(common)
    store.write(record, expected=None)
    foreign = replace(record, request=replace(record.request, request_id="0" * 32))
    with pytest.raises(ValueError):
        store.write(foreign, expected=None)
    with pytest.raises(ValueError):
        store.write(_record(tmp_path, phase="complete"), expected=record)
    with pytest.raises(ValueError):
        store.candidate(1)
    assert store.read(1, record.request.request_id) == record


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "cancelled"])
def test_pending_store_bounds_lock_wait_without_advancement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    """A contended record lock obeys the same deadline and cancellation signal."""
    import fcntl

    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    shutdown = threading.Event()
    clock = [0.0]
    attempts: list[int] = []

    def held_lock(descriptor: int, operation: int) -> None:
        del descriptor
        attempts.append(operation)
        assert operation == fcntl.LOCK_EX | fcntl.LOCK_NB
        if cancel:
            shutdown.set()
        else:
            clock[0] = 2.0
        raise BlockingIOError("record lock held")

    monkeypatch.setattr(fcntl, "flock", held_lock)
    record = _record(tmp_path)
    deadline = _PreparationDeadline(1.0, lambda: clock[0], shutdown)
    error = InterruptedError if cancel else SourceWorkspacePreparationError
    with pytest.raises(error):
        _store(common, deadline=deadline).write(record, expected=None)
    assert attempts == [fcntl.LOCK_EX | fcntl.LOCK_NB]
    assert list(common.rglob("*.json")) == []


@pytest.mark.parametrize("location", ["request", "result"])
@pytest.mark.parametrize(
    "key,value",
    [("schema_version", True), ("generation", "4"), ("generation", 4.5), ("detached", 0)],
)
def test_pending_record_rejects_coerced_workspace_scalars(
    tmp_path: Path, location: str, key: str, value: Any
) -> None:
    """Durable ownership scalars must retain exact types at both boundaries."""
    record = _record(tmp_path, phase="pending_validation")
    payload = record.to_dict()
    workspace = (
        payload["request"]["workspace"] if location == "request" else payload["resulting_workspace"]
    )
    workspace[key] = value
    with pytest.raises(ValueError):
        type(record).from_dict(payload)


@pytest.mark.parametrize(
    "field", ["remote", "base", "branch", "policy", "repository", "owner", "path", "generation"]
)
def test_pending_store_cas_rejects_valid_but_changed_operation_identity(
    tmp_path: Path, field: str
) -> None:
    """A correct previous record does not permit new authority in the next phase."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    intent = _record(tmp_path)
    pending = _record(tmp_path, phase="pending_validation")
    if field in {"remote", "base", "branch", "policy"}:
        key, value = {
            "remote": ("remote_head_sha", "9" * 40),
            "base": ("target_base_sha", "8" * 40),
            "branch": ("branch", "another-branch"),
            "policy": ("policy_name", None),
        }[field]
        pending = replace(pending, **{key: value})
    elif field == "repository":
        pending = replace(
            pending,
            request=replace(pending.request, repository="foreign/repository"),
            destination="https://github.com/foreign/repository.git",
        )
    else:
        workspace_key, workspace_value = {
            "owner": ("ownership_key", "foreign-owner"),
            "path": ("cwd", tmp_path / "another-writer"),
            "generation": ("generation", 6),
        }[field]
        original = replace(pending.request.workspace, **{workspace_key: workspace_value})
        resulting = replace(pending.resulting_workspace, **{workspace_key: workspace_value})
        request = replace(pending.request, workspace=original, checkout_path=original.cwd)
        pending = replace(pending, request=request, resulting_workspace=resulting)
    store = _store(common)
    store.write(intent, expected=None)
    with pytest.raises(ValueError):
        store.write(pending, expected=intent)
    assert store.read(1, intent.request.request_id) == intent


@pytest.mark.parametrize(
    "field,value",
    [("ownership_key", "foreign"), ("cwd", Path("/foreign/writer")), ("lane", SourceLane.REVIEW)],
)
def test_pending_record_rejects_foreign_result_binding(
    tmp_path: Path, field: str, value: Any
) -> None:
    """The resulting source must retain the admitted workspace owner and path."""
    record = _record(tmp_path, phase="pending_validation")
    with pytest.raises(ValueError):
        replace(record, resulting_workspace=replace(record.resulting_workspace, **{field: value}))


@pytest.mark.parametrize("mode", ["none", "absent", "existing"])
def test_pending_store_requires_the_publication_phase_only_when_requested(
    tmp_path: Path, mode: str
) -> None:
    """No-publication completion is distinct from conditional branch publication."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    intent = replace(
        _record(tmp_path),
        publication_mode=mode,
        remote_head_sha="a" * 40 if mode == "existing" else None,
    )
    pending = replace(
        intent,
        phase="pending_validation",
        resulting_workspace=replace(intent.request.workspace, revision="c" * 40, generation=4),
        resulting_tree_sha="d" * 40,
    )
    store = _store(common)
    store.write(intent, expected=None)
    store.write(pending, expected=intent)
    complete = replace(pending, phase="complete")
    if mode == "none":
        written = store.write(complete, expected=pending)
        assert written == complete
    else:
        with pytest.raises(ValueError):
            store.write(complete, expected=pending)
        publishing = replace(pending, phase="publication_intent")
        store.write(publishing, expected=pending)
        written = store.write(complete, expected=publishing)
        assert written == complete


@pytest.mark.parametrize(
    "target,fault",
    [("lock", "symlink"), ("lock", "public"), ("lock", "hardlink"), ("record", "hardlink")],
)
def test_pending_store_rejects_unsafe_lock_and_record_entries(
    tmp_path: Path, target: str, fault: str
) -> None:
    """Neither a lock nor a record can redirect or share private recovery state."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    store = _store(common)
    store.write(record, expected=None)
    namespace = common / "hephaestus-source-workspaces" / "pending-rebases"
    path = next(namespace.glob("*.lock" if target == "lock" else "*.json"))
    external = tmp_path / "external"
    if fault == "symlink":
        path.unlink()
        external.write_bytes(b"private")
        external.chmod(0o600)
        path.symlink_to(external)
    elif fault == "public":
        path.chmod(0o644)
    else:
        os.link(path, external)
    with pytest.raises((OSError, ValueError)):
        store.read(1, record.request.request_id)


def test_pending_store_rejects_writable_common_directory(tmp_path: Path) -> None:
    """A writable common directory cannot anchor a private child."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    common.chmod(0o775)
    with pytest.raises(ValueError):
        _store(common).write(_record(tmp_path), expected=None)
    assert list(common.iterdir()) == []


def test_pending_store_rejects_live_namespace_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readback through an old descriptor cannot authorize a replaced namespace."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    store = _store(common)
    namespace = common / "hephaestus-source-workspaces" / "pending-rebases"
    retained = namespace.with_name("retained-pending-rebases")
    actual_replace = os.replace
    replaced = []

    def replace_then_move(*args: Any, **kwargs: Any) -> None:
        actual_replace(*args, **kwargs)
        namespace.rename(retained)
        namespace.mkdir(mode=0o700)
        replaced.append(True)

    monkeypatch.setattr(os, "replace", replace_then_move)
    with pytest.raises(ValueError):
        store.write(record, expected=None)
    assert replaced == [True]
    assert len(list(retained.glob("*.json"))) == 1
    assert list(namespace.iterdir()) == []


def test_pending_store_rejects_replaced_live_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock on a detached inode cannot protect the current record pathname."""
    import fcntl

    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    namespace = common / "hephaestus-source-workspaces" / "pending-rebases"
    actual_flock = fcntl.flock
    replacements: list[Path] = []

    def replace_acquired_lock(descriptor: int, operation: int) -> None:
        actual_flock(descriptor, operation)
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
            path = next(namespace.glob("*.lock"))
            retained = path.with_suffix(".retained")
            path.rename(retained)
            path.touch(mode=0o600)
            replacements.append(retained)

    monkeypatch.setattr(fcntl, "flock", replace_acquired_lock)
    with pytest.raises(ValueError):
        _store(common).write(record, expected=None)
    assert len(replacements) == 1
    assert replacements[0].exists()
    assert list(namespace.glob("*.json")) == []


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "cancelled"])
@pytest.mark.parametrize("entry_kind", ["records", "unrelated"])
def test_pending_store_stops_discovery_before_reading_after_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, entry_kind: str
) -> None:
    """Discovery must stop between records, not only after a complete scan."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path, phase="complete")
    namespace = common / "hephaestus-source-workspaces" / "pending-rebases"
    namespace.parent.mkdir(mode=0o700)
    namespace.mkdir(mode=0o700)
    for candidate in ("e" * 32, "f" * 32, "0" * 32):
        current = replace(record, request=replace(record.request, request_id=candidate))
        suffix = "json" if entry_kind == "records" else "unrelated"
        path = namespace / f"1-{candidate}.{suffix}"
        path.write_text(json.dumps(current.to_dict()), encoding="utf-8")
        path.chmod(0o600)
    clock = [0.0]
    shutdown = threading.Event()
    actual_scan = os.scandir
    actual_open = os.open
    reads: list[str] = []
    visited: list[str] = []

    @contextmanager
    def expiring_scan(descriptor: Any) -> Any:
        with actual_scan(descriptor) as entries:

            def timed_entries() -> Any:
                count = 0
                for entry in entries:
                    if entry.name.endswith(".lock"):
                        continue
                    count += 1
                    if count == 2:
                        if cancel:
                            shutdown.set()
                        else:
                            clock[0] = 2.0
                    visited.append(entry.name)
                    yield entry

            yield timed_entries()

    def observe_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith(".json"):
            reads.append(str(path))
        return actual_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", expiring_scan)
    monkeypatch.setattr(os, "open", observe_open)
    deadline = _PreparationDeadline(1.0, lambda: clock[0], shutdown)
    with pytest.raises(InterruptedError if cancel else SourceWorkspacePreparationError):
        _store(common, deadline=deadline).candidate(1)
    assert len(reads) == (1 if entry_kind == "records" else 0)
    assert len(visited) == 2


def test_pending_store_checks_budget_between_record_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial record read cannot continue after its shared budget expires."""
    common = tmp_path / "git-common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    _store(common).write(record, expected=None)
    clock = [0.0]
    actual_read = os.read
    reads: list[int] = []

    def expire_after_read(descriptor: int, length: int) -> bytes:
        value = actual_read(descriptor, length)
        reads.append(length)
        clock[0] = 2.0
        return value

    monkeypatch.setattr(os, "read", expire_after_read)
    deadline = _PreparationDeadline(1.0, lambda: clock[0], threading.Event())
    with pytest.raises(SourceWorkspacePreparationError):
        _store(common, deadline=deadline).read(1, record.request.request_id)
    assert len(reads) == 1
