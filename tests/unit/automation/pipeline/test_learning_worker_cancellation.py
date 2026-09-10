"""Keep learning retries safe on each side of host execution."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import source_worktree
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillExecutor,
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.github_jobs import (
    GitHubJob,
    RateBudgetRead,
    ReadRateBudgetRequest,
)
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stage_results import Continue, JobRequest
from hephaestus.automation.pipeline.stages.learning import CLAIM
from hephaestus.automation.pipeline.work_item import ItemKind, LearningIntent, WorkItem
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from tests.unit.automation.pipeline.conftest import FakeWorkerPool, claim_test_item
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
from tests.unit.automation.test_source_worktree import _repository


class _Host:
    def __init__(self) -> None:
        self.calls: list[AthenaSkillRequest] = []

    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        """Return a confirmed delivery from the controlled host boundary."""
        self.calls.append(request)
        return AthenaSkillResult(
            kind="learn",
            delivery_receipt={
                "pr_url": "https://github.com/HomericIntelligence/Mnemosyne/pull/1",
                "pr_number": 1,
                "commit_sha": "a" * 40,
                "readback_head_sha": "a" * 40,
            },
        )

    def cancel(self) -> None:
        """Leave this host idle because it has no external process."""


def _coordinator(repo_root: Path, host: AthenaSkillExecutor) -> Coordinator:
    """Use production learning execution with a controlled host."""
    return Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=repo_root.parent,
            repo_roots={"repo": repo_root},
            max_workers=1,
            learning_workers=1,
            learning_queue_capacity=1,
            rate_guard_enabled=False,
        ),
        github=FakeStageGitHub(),
        pool_factory=FakeWorkerPool().factory,
        auxiliary_pool_factory=partial(AuxiliaryWorkerPool, athena_skill_executor=host),
        install_signals=False,
    )


def _prepare_request(coordinator: Coordinator, item: WorkItem, revision: str) -> JobRequest:
    """Create a real source binding and durable claim before submission."""
    item.state = CLAIM
    item.payload["_synced_default_branch_sha"] = revision
    claim_test_item(coordinator, item)
    stage = coordinator.stages[StageName.LEARNING]
    ctx = coordinator._ctx_for(item)
    assert stage.on_enter(item, ctx) is None
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, AthenaSkillJob)
    assert request.job.request.workspace is not None
    return request


def _restore_item(coordinator: Coordinator) -> WorkItem:
    """Recover the intent from the shared journal in a new coordinator."""
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.FINISHED)
    coordinator._restore_learning_intents(item, StageName.FINISHED, "already merged")
    assert item.stage is StageName.LEARNING
    assert len(item.learning_intents) == 1
    return item


@pytest.mark.parametrize(
    "stop_error", [InterruptedError, KeyboardInterrupt, SystemExit, GeneratorExit]
)
def test_cancelled_learning_lease_returns_claim_to_pending_and_retries_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_error: type[BaseException]
) -> None:
    """A cancelled lease has no host effects and permits a later delivery."""
    repo_root, revision, _second = _repository(tmp_path)
    host = _Host()
    coordinator = _coordinator(repo_root, host)
    intent = LearningIntent.post_merge(repo="repo", issue=1, pr=2)
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.LEARNING)
    item.learning_intents.append(intent)
    request = _prepare_request(coordinator, item, revision)
    journal = coordinator._ctx_for(item).learning_journal
    manager = SourceWorkspaceManager(repo_root, repository="repo")
    lock_path = manager._lane_lock_path(1, SourceLane.IMPLEMENTATION)
    waiting = threading.Event()
    cancel_ready = threading.Event()

    @contextmanager
    def observe_lock(path: Path, **kwargs: Any) -> Iterator[None]:
        try:
            with file_lock(path, **kwargs):
                yield
        except LockUnavailableError:
            if path == lock_path:
                waiting.set()
                assert cancel_ready.wait(timeout=5)
                if stop_error is not InterruptedError:
                    raise stop_error("lease cancelled") from None
            raise

    monkeypatch.setattr(source_worktree, "file_lock", observe_lock)
    try:
        with file_lock(lock_path, require_exclusive=True):
            coordinator._submit(item, request)
            assert waiting.wait(timeout=5)
            coordinator.force_shutdown_event.set()
            cancel_ready.set()
            handle, result = coordinator.auxiliary_completion_q.get(timeout=5)
            assert result.interrupted and not result.ok
            assert result.error == "interrupted_before_start"
            assert result.stderr_tail.startswith(f"{stop_error.__name__}:")
            coordinator._handle_completion(handle, result, auxiliary=True)

        assert host.calls == []
        record = journal.load(intent.key)
        assert record is not None and record["status"] == "pending"
        assert record["error"] == "interrupted_before_start"
        assert not journal.claim_is_active(intent.key)
        assert coordinator.learning_work_count == 0
        assert coordinator.auxiliary_completion_q.empty()

        restarted = _coordinator(repo_root, host)
        try:
            recovered = _restore_item(restarted)
            retry = _prepare_request(restarted, recovered, revision)
            restarted._submit(recovered, retry)
            handle, result = restarted.auxiliary_completion_q.get(timeout=5)
            restarted.shutdown.set()
            restarted._handle_completion(handle, result, auxiliary=True)

            final = restarted._ctx_for(recovered).learning_journal.load(intent.key)
            assert result.ok and not result.interrupted
            assert final is not None and final["status"] == "succeeded"
            assert final["attempts"] == 2
            assert len(host.calls) == 1
            assert restarted.auxiliary_completion_q.empty()
        finally:
            restarted.auxiliary_pool.shutdown(mark_interrupted=False)
    finally:
        cancel_ready.set()
        coordinator.auxiliary_pool.shutdown()
        journal._release_claim_lock(intent.key)


def test_cancelled_learning_after_host_entry_preserves_unknown_effect_on_restart(
    tmp_path: Path,
) -> None:
    """After host entry, restart must not repeat an unconfirmed delivery."""
    repo_root, revision, _second = _repository(tmp_path)

    class InterruptedHost(_Host):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            self.calls.append(request)
            self.started.set()
            assert self.release.wait(timeout=5)
            raise InterruptedError("host request cancelled")

        def cancel(self) -> None:
            self.release.set()

    host = InterruptedHost()
    coordinator = _coordinator(repo_root, host)
    intent = LearningIntent.post_merge(repo="repo", issue=1, pr=2)
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.LEARNING)
    item.learning_intents.append(intent)
    request = _prepare_request(coordinator, item, revision)
    journal = coordinator._ctx_for(item).learning_journal
    try:
        coordinator._submit(item, request)
        assert host.started.wait(timeout=5)
        coordinator.force_shutdown_event.set()
        host.release.set()
        handle, result = coordinator.auxiliary_completion_q.get(timeout=5)
        coordinator._handle_completion(handle, result, auxiliary=True)

        assert result.interrupted and not result.ok
        assert result.error == "InterruptedError: host request cancelled"
        record = journal.load(intent.key)
        assert record is not None and record["status"] == "claimed"
        assert journal.claim_is_active(intent.key)
        assert coordinator.learning_work_count == 0
        assert len(host.calls) == 1

        # Process exit releases the lock but does not change the durable record.
        journal._release_claim_lock(intent.key)
        restarted = _coordinator(repo_root, host)
        try:
            recovered = _restore_item(restarted)
            recovered.state = CLAIM
            stage = restarted.stages[StageName.LEARNING]
            ctx = restarted._ctx_for(recovered)
            assert stage.on_enter(recovered, ctx) is None
            assert isinstance(stage.step(recovered, ctx), Continue)
            final = ctx.learning_journal.load(intent.key)
            assert final is not None and final["status"] == "failed"
            assert final["error"] == "outcome_unknown"
            assert final["attempts"] == 1
            assert len(host.calls) == 1
            assert restarted.auxiliary_completion_q.empty()
        finally:
            restarted.auxiliary_pool.shutdown(mark_interrupted=False)
    finally:
        host.release.set()
        coordinator.auxiliary_pool.shutdown()
        journal._release_claim_lock(intent.key)


@pytest.mark.parametrize("host_error", [None, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_fatal_shared_host_cancellation_preserves_unknown_learning_claim(
    tmp_path: Path,
    host_error: type[BaseException] | None,
) -> None:
    """Fatal shutdown must retain an uncertain claim before either pool waits."""
    repo_root, revision, _second = _repository(tmp_path)
    main_started = threading.Event()
    main_release = threading.Event()

    class CancelledHost(_Host):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.cancelled = threading.Event()

        def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
            self.calls.append(request)
            self.started.set()
            assert self.cancelled.wait(timeout=10)
            if host_error is not None:
                raise host_error("unconfirmed delivery after cancellation")
            return AthenaSkillResult(kind="learn", error="unconfirmed delivery after cancellation")

        def cancel(self) -> None:
            self.cancelled.set()

    class MainRunner:
        gh_timeout = 30

        def run(
            self,
            job: GitHubJob,
            *,
            shutdown: threading.Event | None = None,
            deadline_s: float | None = None,
        ) -> RateBudgetRead:
            assert isinstance(job.request, ReadRateBudgetRequest)
            main_started.set()
            assert main_release.wait(timeout=10)
            return RateBudgetRead(job.request, remaining=None, reset_epoch=None)

    host = CancelledHost()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=repo_root.parent,
            repo_roots={"repo": repo_root},
            max_workers=1,
            learning_workers=1,
            learning_queue_capacity=1,
            rate_guard_enabled=False,
            budget_overrides={"learn": 3},
        ),
        github=FakeStageGitHub(),
        pool_factory=partial(
            WorkerPool,
            lock_dir=tmp_path / "worker-locks",
            github_job_runner=MainRunner(),
            athena_skill_executor=host,
        ),
        auxiliary_pool_factory=partial(AuxiliaryWorkerPool, athena_skill_executor=host),
        install_signals=False,
    )
    intent = LearningIntent.post_merge(repo="repo", issue=1, pr=2)
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.LEARNING)
    item.learning_intents.append(intent)
    request = _prepare_request(coordinator, item, revision)
    journal = coordinator._ctx_for(item).learning_journal
    main = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=3, stage=StageName.PLANNING)
    claim_test_item(coordinator, main)
    errors: list[BaseException] = []

    def teardown() -> None:
        try:
            coordinator._shutdown_pool()
        except (Exception, KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            errors.append(error)

    thread = threading.Thread(target=teardown)
    try:
        coordinator._submit(
            main,
            JobRequest(
                GitHubJob(
                    "repo",
                    repo_root,
                    ReadRateBudgetRequest(time.monotonic() + 30),
                    "controlled_main_operation",
                ),
                "RATE_BUDGET",
            ),
        )
        assert main_started.wait(timeout=5)
        coordinator._submit(item, request)
        assert host.started.wait(timeout=5)
        coordinator._fatal = True
        thread.start()
        completions = coordinator.auxiliary_completion_q
        with completions.not_empty:
            assert completions.not_empty.wait_for(lambda: bool(completions.queue), timeout=5)
            _handle, result = completions.queue[0]
        assert thread.is_alive(), "The main job must hold the fatal teardown interval open"
        main_release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors

        record = journal.load(intent.key)
        assert record is not None and record["status"] == "claimed"
        assert journal.claim_is_active(intent.key)
        assert result.interrupted and not result.ok
        assert len(host.calls) == 1
        assert coordinator.learning_work_count == 0
        assert completions.empty()
        assert not coordinator.in_flight and not coordinator.auxiliary_in_flight
        assert not coordinator.shutdown.is_set(), "Fatal failure must retain exit status 1"

        # Process exit releases the lock without authorizing a new delivery.
        journal._release_claim_lock(intent.key)
        restarted = _coordinator(repo_root, host)
        try:
            recovered = _restore_item(restarted)
            recovered.state = CLAIM
            stage = restarted.stages[StageName.LEARNING]
            ctx = restarted._ctx_for(recovered)
            assert stage.on_enter(recovered, ctx) is None
            assert isinstance(stage.step(recovered, ctx), Continue)
            final = ctx.learning_journal.load(intent.key)
            assert final is not None and final["status"] == "failed"
            assert final["error"] == "outcome_unknown"
            assert final["attempts"] == 1
            assert len(host.calls) == 1
        finally:
            restarted._shutdown_pool()
    finally:
        host.cancel()
        main_release.set()
        if thread.ident is not None:
            thread.join(timeout=10)
        coordinator._shutdown_pool()
        journal._release_claim_lock(intent.key)
