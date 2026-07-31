"""Acceptance tests for bounded pipeline queues and completion recovery."""

from __future__ import annotations

import gc
import json
import time
import weakref
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _job(issue: int) -> AgentJob:
    """Create a lightweight job handle payload for queue tests."""
    return AgentJob(
        repo="repo-a",
        issue=issue,
        agent="claude",
        model="test-model",
        prompt_builder=lambda: "prompt",
        cwd=Path("/tmp"),
        timeout_s=1,
    )


class _JobRequestingStage:
    """Stage that submits exactly one job for an end-to-end overflow test."""

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        """Perform no enter-time transition."""

    def step(self, item: WorkItem, ctx: Any) -> JobRequest | StageOutcome:
        """Request one agent job."""
        return JobRequest(_job(item.issue or 0), on_done_state="VERIFY")

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        """Keep the callback empty because the overflow path parks the item."""


class _RecoveringStage(_JobRequestingStage):
    """Complete normally when the same seed is submitted to a fresh run."""

    def step(self, item: WorkItem, ctx: Any) -> JobRequest | StageOutcome:
        """Submit once, then finish after the recovered completion."""
        if item.state == "ENTER":
            return JobRequest(_job(item.issue or 0), on_done_state="VERIFY")
        return StageOutcome(Disposition.FINISH_PASS, "recovered")


def _coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pool: Any | None = None,
    event_log_path: Path | None = None,
    max_workers: int = 1,
    parallel_repos: int = 1,
    stage_queue_capacity: int | None = None,
) -> Coordinator:
    """Build a coordinator with an explicitly matched bounded worker channel."""
    capacity = max(1, max_workers * parallel_repos)
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        max_workers=max_workers,
        parallel_repos=parallel_repos,
        projects_dir=tmp_path,
        event_log_path=event_log_path,
    )
    if stage_queue_capacity is not None:
        config = replace(config, stage_queue_capacity=stage_queue_capacity)
    if pool is None:
        pool = FakeWorkerPool(size=capacity, completion_q=CompletionQueue(capacity=capacity))
    monkeypatch.setattr(
        seeding_mod,
        "seed_from_cli",
        lambda repos, issues, prs: [
            seeding_mod.SeedEntry(
                kind="issue", identifier=1, stage=StageName.PLANNING, reason="test seed"
            )
        ],
    )
    coordinator = Coordinator(config, github=FakeStageGitHub(), pool=pool, install_signals=False)
    coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]
    return coordinator


def test_work_window_bounds_all_pipeline_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completion capacity follows workers while stage capacity stays independent."""
    coordinator = _coordinator(tmp_path, monkeypatch, max_workers=2, parallel_repos=3)

    assert coordinator._work_window == 6
    assert coordinator.completion_q.capacity == 6
    assert {queue.capacity for queue in coordinator.queues.values()} == {
        coordinator.config.stage_queue_capacity
    }
    assert coordinator.config.stage_queue_capacity != coordinator._work_window

    coordinator.in_flight.update(
        {
            JobHandle(job=_job(issue), on_done_state="VERIFY"): WorkItem(
                repo="repo-a", kind=ItemKind.REPO
            )
            for issue in range(6)
        }
    )
    assert coordinator._admit(WorkItem(repo="repo-a", kind=ItemKind.REPO)) is False


def test_stage_burst_is_deferred_until_queue_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full stage queue defers new work without stopping the coordinator."""
    event_log = tmp_path / "events.jsonl"
    coordinator = _coordinator(
        tmp_path,
        monkeypatch,
        event_log_path=event_log,
        max_workers=2,
        stage_queue_capacity=1,
    )
    first = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    second = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)

    assert coordinator._push_item(first, StageName.REPO, enter=True)
    assert coordinator._push_item(second, StageName.REPO, enter=True)

    assert coordinator.queues[StageName.REPO].snapshot() == [first]
    assert second in coordinator.items
    assert second.result is None
    assert coordinator._pending_admissions
    assert not coordinator.shutdown.is_set()
    assert coordinator._exit_code() == 0
    assert any(event[0] == "queue_deferred" for event in coordinator.event_log)
    assert not any(event[0] == "queue_saturated" for event in coordinator.event_log)
    records = [json.loads(line) for line in event_log.read_text().splitlines()]
    assert any(record["event"] == "queue_deferred" for record in records)


def test_large_production_seed_source_allocates_only_with_available_permits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 10,000-entry production source retains an iterator, not a seed list."""
    event_log = tmp_path / "bounded-admission-events.jsonl"
    burst_size = 10_000
    repos = [f"repo-{index}" for index in range(1, burst_size + 1)]
    created_entries = 0
    seed_entry_type = seeding_mod.SeedEntry

    def counted_seed_entry(*args: Any, **kwargs: Any) -> seeding_mod.SeedEntry:
        """Count production ``SeedEntry`` allocation without replacing its behavior."""
        nonlocal created_entries
        created_entries += 1
        return seed_entry_type(*args, **kwargs)

    monkeypatch.setattr(seeding_mod, "SeedEntry", counted_seed_entry)
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=repos,
            max_workers=1,
            parallel_repos=1,
            stage_queue_capacity=1,
            metrics_port=9124,
            projects_dir=tmp_path,
            event_log_path=event_log,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(size=1, completion_q=CompletionQueue(capacity=1)),
        install_signals=False,
    )

    pushed = coordinator._seed_pass()

    assert coordinator._admission_spool_capacity == len(StageName)
    assert pushed == 1
    # The global permit check happens before advancing the real generator, so
    # neither the coordinator nor seed_from_cli materializes the remaining
    # 9,999 SeedEntry instances.
    assert created_entries == 1
    assert created_entries < burst_size
    assert len(coordinator.queues[StageName.REPO]) == 1
    assert len(coordinator._pending_admissions) == 0
    resident_depth = sum(len(queue) for queue in coordinator.queues.values()) + len(
        coordinator._pending_admissions
    )
    resident_capacity = sum(queue.capacity for queue in coordinator.queues.values()) + (
        coordinator._admission_spool_capacity
    )
    assert resident_depth == 1
    assert resident_depth <= resident_capacity
    assert len(coordinator.items) == pushed
    assert len(coordinator.items) < burst_size
    assert coordinator.items[-1].result is None
    assert coordinator.live_work_count == coordinator._work_window == 1
    remaining_entries = coordinator._seed_entries
    assert remaining_entries is not None
    assert iter(remaining_entries) is remaining_entries
    second_entry = next(remaining_entries)
    assert second_entry.identifier == "repo-2"
    assert created_entries == 2
    assert not coordinator.shutdown.is_set()
    assert coordinator._fatal is False

    snapshot = coordinator._observability_snapshot()
    assert snapshot["admission_depth"] == 0
    assert snapshot["admission_capacity"] == coordinator._admission_spool_capacity
    coordinator._emit_observability_tick()
    assert coordinator._metrics_registry is not None
    rendered = coordinator._metrics_registry.render_prometheus()
    assert "hephaestus_pipeline_admission_depth 0" in rendered
    assert (
        f"hephaestus_pipeline_admission_capacity {coordinator._admission_spool_capacity}"
        in rendered
    )
    records = [json.loads(line) for line in event_log.read_text().splitlines()]
    assert not any(record["event"] == "queue_saturated" for record in records)


def test_push_fallback_overflow_preserves_spool_and_journals_exact_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A far-over-bound burst retains only the spool and exact recovery boundary."""
    event_log = tmp_path / "push-fallback-overflow.jsonl"
    burst_size = 10_000
    coordinator = _coordinator(
        tmp_path,
        monkeypatch,
        event_log_path=event_log,
        max_workers=2 + len(StageName),
        stage_queue_capacity=1,
    )
    queued = WorkItem(
        repo="repo-a",
        kind=ItemKind.ISSUE,
        issue=1,
        stage=StageName.PLANNING,
    )
    assert coordinator._push_item(queued, StageName.PLANNING, enter=True)
    for issue in range(2, 2 + coordinator._admission_spool_capacity):
        assert coordinator._push_item(
            WorkItem(
                repo="repo-a",
                kind=ItemKind.ISSUE,
                issue=issue,
                stage=StageName.PLANNING,
            ),
            StageName.PLANNING,
            enter=True,
        )
    pending_before = list(coordinator._pending_admissions)
    boundary_issue = 2 + coordinator._admission_spool_capacity
    boundary = WorkItem(
        repo="repo-a",
        kind=ItemKind.ISSUE,
        issue=boundary_issue,
        stage=StageName.PLANNING,
    )

    assert coordinator._push_item(boundary, StageName.PLANNING, enter=True) is False

    # Once the exact saturation boundary is parked, a burst far beyond C+1 is
    # refused before acquiring a permit or entering coordinator accounting.
    # This proves the bounded spool cannot become an alternate unbounded queue.
    for issue in range(boundary_issue + 1, burst_size + 1):
        assert (
            coordinator._push_item(
                WorkItem(
                    repo="repo-a",
                    kind=ItemKind.ISSUE,
                    issue=issue,
                    stage=StageName.PLANNING,
                ),
                StageName.PLANNING,
                enter=True,
            )
            is False
        )

    assert list(coordinator._pending_admissions) == pending_before
    assert boundary.result is not None
    assert "admission spool saturated" in boundary.result.reason
    assert len(coordinator.items) == coordinator._admission_spool_capacity + 2
    assert coordinator.live_work_count == coordinator._admission_spool_capacity + 1
    resident_depth = sum(queue.occupancy for queue in coordinator.queues.values()) + len(
        coordinator._pending_admissions
    )
    resident_capacity = sum(queue.capacity for queue in coordinator.queues.values()) + (
        coordinator._admission_spool_capacity
    )
    assert resident_depth == coordinator._admission_spool_capacity + 1
    assert resident_depth <= resident_capacity
    snapshot = coordinator._observability_snapshot()
    assert snapshot["admission_depth"] == coordinator._admission_spool_capacity
    assert snapshot["admission_capacity"] == coordinator._admission_spool_capacity
    receipts = [
        record
        for record in (json.loads(line) for line in event_log.read_text().splitlines())
        if record["event"] == "queue_saturated"
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["fields"][0]["item"] == f"repo-a#{boundary_issue}"
    assert receipt["fields"][0]["details"] == {
        "admission_depth": coordinator._admission_spool_capacity,
        "blocked_stage": "planning",
        "stage_capacity": 1,
        "stage_depth": 1,
    }


def test_product_burst_respects_global_live_work_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery cannot retain products beyond the stage plus admission bounds."""
    event_log = tmp_path / "bounded-product-events.jsonl"
    burst_size = 10_000
    coordinator = _coordinator(
        tmp_path,
        monkeypatch,
        event_log_path=event_log,
        stage_queue_capacity=1,
    )
    repo_item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    repo_item.payload["products"] = [
        {"kind": "issue", "number": issue, "stage": StageName.PLANNING}
        for issue in range(1, burst_size + 1)
    ]

    with pytest.raises(RuntimeError, match="RepoIssueSource"):
        coordinator._seed_products(repo_item)

    assert len(coordinator.queues[StageName.PLANNING]) == 0
    assert len(coordinator._pending_admissions) == 0
    assert len(coordinator.items) == 0
    assert len(coordinator.items) < burst_size
    assert "products" not in repo_item.payload
    assert not coordinator.shutdown.is_set()
    assert coordinator._fatal is False

    snapshot = coordinator._observability_snapshot()
    assert snapshot["admission_depth"] == 0
    assert not event_log.exists()


def test_c_plus_one_seed_drains_and_recovery_reuses_the_same_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capacity-one run admits C+1 work and a fresh run accepts that seed again."""
    event_log = tmp_path / "c-plus-one-events.jsonl"
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        max_workers=1,
        parallel_repos=1,
        stage_queue_capacity=1,
        projects_dir=tmp_path,
        event_log_path=event_log,
    )
    monkeypatch.setattr(
        seeding_mod,
        "seed_from_cli",
        lambda repos, issues, prs: [
            seeding_mod.SeedEntry(
                kind="issue", identifier=issue, stage=StageName.PLANNING, reason="seed"
            )
            for issue in (1, 2)
        ],
    )
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(size=1, completion_q=CompletionQueue(capacity=1)),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _RecoveringStage()

    assert coordinator.run() == 0
    summaries = coordinator._effective_items()
    assert [item.issue for item in summaries] == [1, 2]
    assert coordinator.items == []
    assert len(coordinator.items) <= coordinator._work_window
    assert len(coordinator.item_summaries) == 2
    assert len(coordinator.ledger) == 2
    assert not coordinator._pending_admissions
    assert all(item.result is not None and item.result.passed for item in summaries)
    assert all(item.entry_stage == StageName.PLANNING.value for item in coordinator.item_summaries)
    assert all(not hasattr(item, "payload") for item in coordinator.item_summaries)
    records = [json.loads(line) for line in event_log.read_text().splitlines()]
    deferred = [record for record in records if record["event"] == "queue_deferred"]
    assert deferred == []
    assert not any(record["event"] == "queue_saturated" for record in records)

    retry = Coordinator(
        config,
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(size=1, completion_q=CompletionQueue(capacity=1)),
        install_signals=False,
    )
    retry.stages[StageName.PLANNING] = _RecoveringStage()

    assert retry.run() == 0
    retry_summaries = retry._effective_items()
    assert [item.issue for item in retry_summaries] == [1, 2]
    assert retry.items == []
    assert len(retry.items) <= retry._work_window
    assert len(retry.item_summaries) == 2
    assert len(retry.ledger) == 2
    assert not retry._pending_admissions
    assert all(item.result is not None and item.result.passed for item in retry_summaries)


def test_completed_issue_payload_retention_stays_bounded_by_work_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completing more than C issues retains summaries, not full WorkItems."""
    issue_count = 5
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        max_workers=1,
        parallel_repos=1,
        stage_queue_capacity=1,
        projects_dir=tmp_path,
    )
    monkeypatch.setattr(
        seeding_mod,
        "seed_from_cli",
        lambda repos, issues, prs: (
            seeding_mod.SeedEntry(
                kind="issue",
                identifier=issue,
                stage=StageName.PLANNING,
                reason="seed",
            )
            for issue in range(1, issue_count + 1)
        ),
    )
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(size=1, completion_q=CompletionQueue(capacity=1)),
        install_signals=False,
    )
    observed_live_counts: list[int] = []

    class _IssuePayload:
        """Weak-referenceable stand-in for a large GitHub issue payload."""

        def __init__(self) -> None:
            self.body = "payload" * 10_000

    payload_refs: list[weakref.ReferenceType[_IssuePayload]] = []

    class PayloadStage:
        """Attach a representative large payload, then complete immediately."""

        def on_enter(self, item: WorkItem, ctx: Any) -> None:
            payload = _IssuePayload()
            payload_refs.append(weakref.ref(payload))
            item.payload["issue_body"] = payload

        def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
            observed_live_counts.append(len(coordinator.items))
            return StageOutcome(Disposition.FINISH_PASS, "done")

        def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
            """No jobs are submitted by this stage."""

    coordinator.stages[StageName.PLANNING] = PayloadStage()

    assert coordinator.run() == 0
    assert issue_count > coordinator._work_window
    assert max(observed_live_counts) <= coordinator._work_window
    assert coordinator.items == []
    assert len(coordinator.item_summaries) == issue_count
    assert len(coordinator.ledger) == issue_count
    assert [item.issue for item in coordinator._effective_items()] == list(
        range(1, issue_count + 1)
    )
    assert all(not hasattr(item, "payload") for item in coordinator.item_summaries)
    gc.collect()
    assert all(payload_ref() is None for payload_ref in payload_refs)


def test_c_plus_one_products_stop_at_global_permit_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repo discovery products use the same resumable admission spool as seeds."""
    coordinator = _coordinator(tmp_path, monkeypatch, stage_queue_capacity=1)
    repo_item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    repo_item.payload["products"] = [
        {"kind": "issue", "number": 1, "stage": StageName.PLANNING},
        {"kind": "issue", "number": 2, "stage": StageName.PLANNING},
    ]

    with pytest.raises(RuntimeError, match="RepoIssueSource"):
        coordinator._seed_products(repo_item)

    assert not coordinator.queues[StageName.PLANNING].snapshot()
    assert not coordinator._pending_admissions
    assert coordinator.live_work_count == 0
    assert not coordinator.shutdown.is_set()


def test_repeated_wakes_do_not_consume_completion_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C completion results and any number of wake requests coexist safely."""
    coordinator = _coordinator(tmp_path, monkeypatch, max_workers=2, parallel_repos=2)
    for issue in range(4):
        assert coordinator.completion_q.offer((object(), JobResult(ok=True, value=issue)))

    for _ in range(100):
        coordinator._wake_completion_wait()

    assert coordinator.completion_q.qsize() == coordinator._work_window
    assert coordinator._completion_wake.is_set()


def test_snapshot_exports_capacity_depth_and_rejection_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime observability exposes bounded depth and durable rejection counters."""
    base = _coordinator(tmp_path, monkeypatch, stage_queue_capacity=1)
    config = replace(base.config, metrics_port=9123, max_workers=2, stage_queue_capacity=1)
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(size=2, completion_q=CompletionQueue(capacity=2)),
        install_signals=False,
    )
    item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    rejected = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    assert coordinator._push_item(item, StageName.REPO, enter=True)
    assert coordinator._push_item(rejected, StageName.REPO, enter=True)
    coordinator._record_queue_saturation(
        queue=StageName.REPO.value,
        capacity=1,
        item=rejected,
        source="test_rejection",
    )

    snapshot = coordinator._observability_snapshot()
    assert snapshot["queue_capacities"][StageName.REPO.value] == 1
    assert snapshot["queue_depths"][StageName.REPO.value] == 1
    assert snapshot["admission_depth"] == 1
    assert snapshot["admission_capacity"] == len(StageName)
    assert snapshot["queue_rejection_totals"][StageName.REPO.value] == 1
    assert snapshot["saturated_queues"] == [StageName.REPO.value]

    coordinator._emit_observability_tick()
    assert coordinator._metrics_registry is not None
    rendered = coordinator._metrics_registry.render_prometheus()
    assert 'hephaestus_pipeline_queue_capacity{stage="repo"} 1' in rendered
    assert f"hephaestus_pipeline_admission_capacity {len(StageName)}" in rendered
    assert "hephaestus_pipeline_admission_depth 1" in rendered
    assert "hephaestus_pipeline_queue_rejections_total" in rendered


def test_saturation_journal_append_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saturation event cannot continue when its configured journal is unwritable."""
    event_log = tmp_path / "events.jsonl"
    event_log.mkdir()
    coordinator = _coordinator(tmp_path, monkeypatch, event_log_path=event_log)

    coordinator._record_queue_saturation(
        queue="completion",
        capacity=1,
        source="completion_publish_rejected",
    )

    assert coordinator._event_log_disabled is True
    assert coordinator._journal_failure is True
    assert coordinator._fatal is True
    assert coordinator.shutdown.is_set()
    assert coordinator._exit_code() == 1


class _OverflowingPool(FakeWorkerPool):
    """Fill the result channel before rejecting the submitted completion."""

    def submit(self, job: Any, on_done_state: Any, **kwargs: Any) -> JobHandle:
        """Return a handle whose result goes to the bounded rejection mailbox."""
        handle = JobHandle(job=job, on_done_state=on_done_state)
        self.submitted.append(handle)
        self.submitted_claims.append((kwargs.get("claim_key", ""), kwargs.get("claim_stage", "")))
        blocker = JobHandle(job=_job(999), on_done_state="VERIFY")
        assert self.completion_q.offer((blocker, JobResult(ok=True)))
        assert self.completion_q.offer((handle, JobResult(ok=True))) is False
        self.shutdown_event.set()
        return handle


def test_completion_rejection_is_durable_and_returns_non_signal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected worker result parks its exact item and returns 1, never 130."""
    event_log = tmp_path / "events.jsonl"
    pool = _OverflowingPool(
        size=1,
        completion_q=CompletionQueue(capacity=1),
    )
    coordinator = _coordinator(tmp_path, monkeypatch, pool=pool, event_log_path=event_log)
    coordinator.stages[StageName.PLANNING] = _JobRequestingStage()

    started = time.monotonic()
    exit_code = coordinator.run()

    assert time.monotonic() - started < 1.0
    assert coordinator._signal_received is False
    assert exit_code == 1
    assert exit_code != 130
    assert coordinator.in_flight == {}
    assert coordinator.items[0].result is not None
    assert coordinator.items[0].result.reason.startswith("resumable at planning")
    records = [json.loads(line) for line in event_log.read_text().splitlines()]
    assert any(record["event"] == "queue_saturated" for record in records)


def test_completion_rejection_recovers_from_the_same_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh coordinator can process the unchanged seed after saturation."""
    first_pool = _OverflowingPool(size=1, completion_q=CompletionQueue(capacity=1))
    first = _coordinator(tmp_path, monkeypatch, pool=first_pool)
    first.stages[StageName.PLANNING] = _JobRequestingStage()

    assert first.run() == 1
    assert first._signal_received is False
    assert first.items[0].result is not None
    assert first.items[0].result.reason.startswith("resumable at planning")

    second = _coordinator(
        tmp_path,
        monkeypatch,
        pool=FakeWorkerPool(size=1, completion_q=CompletionQueue(capacity=1)),
    )
    second.stages[StageName.PLANNING] = _RecoveringStage()

    assert second.run() == 0
    assert second.items == []
    assert second.item_summaries[0].result.passed is True


def test_completion_saturation_blocks_follow_on_submission_from_accepted_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After saturation, accepted completions are parked but cannot admit new work."""
    completion_q = CompletionQueue(capacity=1)
    pool = FakeWorkerPool(size=1, completion_q=completion_q)
    coordinator = _coordinator(tmp_path, monkeypatch, pool=pool)

    class SubmittingOnDoneStage(_JobRequestingStage):
        def __init__(self) -> None:
            self.job_done_calls = 0

        def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
            self.job_done_calls += 1
            item.payload["accepted_completion_processed"] = True
            coordinator._submit(item, JobRequest(_job(300), on_done_state="VERIFY"))

    stage = SubmittingOnDoneStage()
    coordinator.stages[StageName.PLANNING] = stage
    rejected_item = WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=10, stage=StageName.PLANNING)
    accepted_item = WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=20, stage=StageName.PLANNING)
    rejected_handle = JobHandle(job=_job(10), on_done_state="VERIFY")
    accepted_handle = JobHandle(job=_job(20), on_done_state="VERIFY")
    coordinator.in_flight[rejected_handle] = rejected_item
    coordinator.in_flight[accepted_handle] = accepted_item
    coordinator.inflight_per_repo["repo-a"] = 2

    assert completion_q.offer((accepted_handle, JobResult(ok=True)))
    assert completion_q.offer((rejected_handle, JobResult(ok=True))) is False

    coordinator._drain_completions()

    assert coordinator.shutdown.is_set()
    assert stage.job_done_calls == 1
    assert pool.submitted == []
    assert coordinator.in_flight == {}
    assert rejected_item.result is not None
    assert rejected_item.result.reason.startswith("resumable at planning")
    assert accepted_item.payload["accepted_completion_processed"] is True
    assert accepted_item.result is not None
    assert accepted_item.result.reason.startswith("resumable at planning")


def test_rejection_mailbox_overflow_parks_all_live_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mailbox overflow parks live work and reports failure, not a signal."""
    coordinator = _coordinator(tmp_path, monkeypatch)
    first = WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLANNING)
    second = WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=2, stage=StageName.PLANNING)
    handle1 = JobHandle(job=_job(1), on_done_state="VERIFY")
    handle2 = JobHandle(job=_job(2), on_done_state="VERIFY")
    coordinator.in_flight[handle1] = first
    coordinator.in_flight[handle2] = second
    coordinator.inflight_per_repo["repo-a"] = 2

    assert coordinator.completion_q.offer((object(), JobResult(ok=True)))
    assert coordinator.completion_q.offer((handle1, JobResult(ok=True))) is False
    assert coordinator.completion_q.offer((handle2, JobResult(ok=True))) is False

    assert coordinator._drain_completion_rejections()

    assert coordinator.in_flight == {}
    assert first.result is not None
    assert second.result is not None
    assert first.result.reason.startswith("resumable at planning")
    assert second.result.reason.startswith("resumable at planning")
    assert coordinator._pool_shut_down is True
    assert coordinator.shutdown.is_set()
    assert coordinator._signal_received is False
    assert coordinator._exit_code() == 1
