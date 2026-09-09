"""Tests for queue completion and terminal capacity release."""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillJob, AthenaSkillRequest
from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.work_item import ItemKind, LearningIntent, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _learning_job(tmp_path: Path) -> AthenaSkillJob:
    """Return one host learning request without external work."""
    return AthenaSkillJob(
        request=AthenaSkillRequest(
            kind="learn",
            repo="repo",
            issue=1,
            agent="codex",
            model="default",
            cwd=tmp_path,
            timeout_s=1,
        )
    )


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("saturated", [False, True])
def test_auxiliary_completion_reports_base_exception(
    tmp_path: Path, error_type: type[BaseException], saturated: bool
) -> None:
    """A worker control exception releases bookkeeping and reports its result."""
    completions: queue.Queue[tuple[JobHandle, JobResult]] = queue.Queue(maxsize=1)
    wakeup = threading.Event()
    saturation = threading.Event()
    handle = JobHandle(job=_learning_job(tmp_path), on_done_state="RESULT")
    occupied = (handle, JobResult(ok=True, value="occupied"))
    if saturated:
        completions.put_nowait(occupied)
    pool = AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=None,
    )
    pool.set_completion_notifiers(wakeup=wakeup, saturation=saturation)
    future: Future[JobResult] = Future()
    future.set_exception(error_type("worker stopped"))
    pool._futures.add(future)
    escaped: BaseException | None = None
    try:
        try:
            pool._publish(handle, future)
        except BaseException as exc:
            escaped = exc
        assert escaped is None
        assert not pool._futures
        assert wakeup.is_set()
        assert saturation.is_set() is saturated
        returned_handle, result = completions.get_nowait()
        assert returned_handle is handle
        if saturated:
            assert result is occupied[1]
        else:
            assert not result.ok
            assert result.error == f"worker_crash: {error_type.__name__}: worker stopped"
        assert completions.empty()
    finally:
        pool.shutdown(mark_interrupted=False)


def test_learning_journal_error_releases_capacity_and_preserves_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked result frees its lane while its durable learning claim remains."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            max_workers=1,
            learning_queue_capacity=1,
            rate_guard_enabled=False,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, pr=7, stage=StageName.LEARNING)
    worktree = tmp_path / "preserved-worktree"
    worktree.mkdir()
    marker = worktree / "uncommitted.txt"
    marker.write_text("retain this work", encoding="utf-8")
    item.worktree = str(worktree)
    intent = LearningIntent.post_merge(repo="repo", issue=1, pr=7)
    item.learning_intents.append(intent)
    journal = coordinator._ctx_for(item).learning_journal
    assert journal is not None
    journal.ensure_pending(intent.key, kind=intent.kind.value, identity=intent.journal_identity())
    assert journal.claim(intent.key)
    before = journal.path(intent.key).read_bytes()
    item.payload["_learning_claimed_intent_key"] = intent.key

    def fail_retry(key: str, *, error: str) -> None:
        del key, error
        raise OSError("journal unavailable")

    monkeypatch.setattr(journal, "retry", fail_retry)
    assert coordinator._push_item(item, StageName.LEARNING, enter=True)
    assert coordinator._claim_item(StageName.LEARNING) is item
    handle = JobHandle(job=_learning_job(tmp_path), on_done_state="RESULT")
    coordinator.auxiliary_in_flight[handle] = item

    coordinator._handle_completion(handle, JobResult(ok=False, error="host failed"), auxiliary=True)

    assert item.result is not None and item.result.reason == "resumable at learning"
    assert journal.path(intent.key).read_bytes() == before
    assert journal.claim_is_active(intent.key)
    assert marker.read_text(encoding="utf-8") == "retain this work"
    assert coordinator.learning_work_count == 0
    next_item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.LEARNING)
    assert coordinator._push_item(next_item, StageName.LEARNING, enter=True)
    coordinator._park_resumable(item)
    assert coordinator.learning_work_count == 1
    assert coordinator._terminal_summary.dispositions["resumable"] == 1
    assert journal.path(intent.key).read_bytes() == before
    assert journal.claim_is_active(intent.key)
