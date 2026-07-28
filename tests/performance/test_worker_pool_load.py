"""Bounded capacity and sustained-concurrency tests for ``WorkerPool``."""

from __future__ import annotations

import json
import os
import platform
import queue
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import BuildTestJob, JobHandle, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from tests.performance.conftest import LoadConfig

_CAPACITY_JOBS = 256
_DRAIN_GRACE_S = 10.0


@dataclass
class _Tracker:
    """Thread-safe timestamps and concurrency observations for one load run."""

    active: int = 0
    peak_active: int = 0

    def __post_init__(self) -> None:
        """Initialize the synchronization and observation containers."""
        self._lock = threading.Lock()
        self.start_times: dict[str, float] = {}
        self.worker_ids: set[str] = set()

    def started(self, job_id: str) -> None:
        """Record a synthetic handler entering service."""
        with self._lock:
            self.start_times[job_id] = time.monotonic()
            self.worker_ids.add(threading.current_thread().name)
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)

    def finished(self) -> None:
        """Record a synthetic handler leaving service."""
        with self._lock:
            self.active -= 1


@dataclass
class _Measurements:
    """Completion-channel measurements for one bounded load run."""

    started_at: float

    def __post_init__(self) -> None:
        """Initialize handle-indexed measurements at the run start time."""
        self.submitted_at: dict[JobHandle, float] = {}
        self.completed_handles: set[JobHandle] = set()
        self.queue_latency_ms: list[float] = []
        self.end_to_end_latency_ms: list[float] = []
        self.duplicates = 0
        self.errors = 0
        self.last_completion_at = self.started_at

    @property
    def outstanding(self) -> int:
        """Return the number of submitted handles awaiting completion."""
        return len(self.submitted_at) - len(self.completed_handles)

    def record_completion(
        self,
        handle: JobHandle,
        result: JobResult,
        completed_at: float,
        tracker: _Tracker,
    ) -> None:
        """Capture one completion and its queue/end-to-end latency samples."""
        submitted_at = self.submitted_at.get(handle)
        if submitted_at is None or handle in self.completed_handles:
            self.duplicates += 1
            self.errors += 1
            return

        start_at = tracker.start_times.get(handle.job.descr)
        if start_at is None:
            self.errors += 1
            return

        self.completed_handles.add(handle)
        self.last_completion_at = completed_at
        if not result.ok:
            self.errors += 1
        self.queue_latency_ms.append((start_at - submitted_at) * 1_000)
        self.end_to_end_latency_ms.append((completed_at - submitted_at) * 1_000)


@dataclass(frozen=True)
class _LatencySummary:
    """Percentile summary in milliseconds for one latency dimension."""

    p50: float
    p95: float
    p99: float
    maximum: float


@dataclass(frozen=True)
class _LoadReport:
    """Measurements and invariants captured by a bounded worker-pool run."""

    config: LoadConfig
    submitted: int
    completed: int
    duplicates: int
    errors: int
    active_workers: int
    peak_concurrency: int
    elapsed_s: float
    drain_s: float
    queue_latency_ms: _LatencySummary
    end_to_end_latency_ms: _LatencySummary

    @property
    def lost(self) -> int:
        """Return submitted jobs that did not produce one completion."""
        return self.submitted - self.completed

    @property
    def throughput_jobs_per_s(self) -> float:
        """Return completion throughput over the full run duration."""
        return self.completed / self.elapsed_s if self.elapsed_s else 0.0

    def as_json(self) -> dict[str, object]:
        """Return a structured runtime-evidence report without fabricated values."""
        return {
            "schema_version": 1,
            "profile": asdict(self.config),
            "environment": {
                "commit": os.environ.get("GITHUB_SHA", "unknown"),
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "invariants": {
                "submitted": self.submitted,
                "completed": self.completed,
                "lost": self.lost,
                "duplicates": self.duplicates,
                "errors": self.errors,
            },
            "concurrency": {
                "configured_workers": self.config.workers,
                "active_workers": self.active_workers,
                "peak": self.peak_concurrency,
            },
            "runtime": {
                "elapsed_s": self.elapsed_s,
                "drain_s": self.drain_s,
                "throughput_jobs_per_s": self.throughput_jobs_per_s,
            },
            "latency_ms": {
                "queue": asdict(self.queue_latency_ms),
                "end_to_end": asdict(self.end_to_end_latency_ms),
            },
        }


class _SyntheticRecoveryStage:
    """Run one real worker-pool job, then finish after its completion."""

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        """Perform no enter-time transition."""
        del item, ctx

    def step(self, item: WorkItem, ctx: Any) -> JobRequest | StageOutcome:
        """Submit once and finish after the recovered completion."""
        del ctx
        if item.payload.get("completed"):
            return StageOutcome(Disposition.FINISH_PASS, "synthetic complete")
        job = BuildTestJob(
            repo=item.repo,
            cwd=Path.cwd(),
            argv=("synthetic", f"recovery-{item.issue}"),
            timeout_s=2,
            descr=f"recovery-{item.issue}",
        )
        return JobRequest(job, on_done_state=StageName.PLANNING)

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        """Record successful completion for the next stage step."""
        del ctx
        if result.ok:
            item.payload["completed"] = True


class _ObservedCompletionQueue(CompletionQueue):
    """Completion queue with a synchronization-only publication counter."""

    def __init__(self, *, capacity: int) -> None:
        super().__init__(capacity=capacity)
        self._offer_condition = threading.Condition()
        self.offer_count = 0
        self.accepted_count = 0
        self.rejected_count = 0
        self.result_high_water = 0
        self.rejection_high_water = 0
        self.overflow_observed = False
        self.publication_outcomes: list[tuple[str, bool]] = []

    def offer(self, value: tuple[Any, Any]) -> bool:
        """Publish normally and capture exact paused-consumer high-water state."""
        accepted = super().offer(value)
        with self._offer_condition:
            self.offer_count += 1
            if accepted:
                self.accepted_count += 1
            else:
                self.rejected_count += 1
            self.result_high_water = max(self.result_high_water, self.qsize())
            self.rejection_high_water = max(
                self.rejection_high_water,
                self._rejections.qsize(),
            )
            self.overflow_observed = self.overflow_observed or self._rejection_overflowed
            handle = value[0]
            description = str(getattr(getattr(handle, "job", None), "descr", ""))
            self.publication_outcomes.append((description, accepted))
            self._offer_condition.notify_all()
        return accepted

    def wait_for_offers(self, expected: int, *, timeout: float) -> None:
        """Wait until exactly observed worker callbacks reach *expected*."""
        deadline = time.monotonic() + timeout
        with self._offer_condition:
            while self.offer_count < expected:
                remaining = deadline - time.monotonic()
                assert remaining > 0, (
                    f"observed {self.offer_count}/{expected} completion publications"
                )
                self._offer_condition.wait(timeout=remaining)


class _StalledConsumerHarness:
    """Synchronize two complete worker waves without relying on sleeps."""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.legitimate_started = 0
        self.overflow_started = 0
        self.state_lock = threading.Lock()
        self.legitimate_ready = threading.Event()
        self.overflow_ready = threading.Event()
        self.release_legitimate = threading.Event()
        self.release_overflow = threading.Event()

    def run_job(self, _pool: WorkerPool, job: BuildTestJob) -> JobResult:
        """Hold two worker waves while the coordinator cannot consume."""
        is_overflow = job.descr.startswith("overflow-")
        with self.state_lock:
            if is_overflow:
                self.overflow_started += 1
                if self.overflow_started == self.workers:
                    self.overflow_ready.set()
            else:
                self.legitimate_started += 1
                if self.legitimate_started == self.workers:
                    self.legitimate_ready.set()
        release = self.release_overflow if is_overflow else self.release_legitimate
        assert release.wait(timeout=5.0)
        return JobResult(ok=True, value=job.descr)


def _make_recovery_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
    *,
    observed: bool = False,
) -> Coordinator:
    """Build the same C-sized coordinator/pool topology used in production."""
    config = PipelineConfig(
        org="org",
        repos=["performance/worker-pool"],
        loops=1,
        max_workers=workers,
        parallel_repos=1,
        stage_queue_capacity=workers,
        projects_dir=Path.cwd(),
    )
    work_window = max(1, config.parallel_repos * config.max_workers)
    completion_q: CompletionQueue
    if observed:
        completion_q = _ObservedCompletionQueue(capacity=work_window)
    else:
        completion_q = CompletionQueue(capacity=work_window)
    shared_shutdown = threading.Event()
    pool = WorkerPool(size=work_window, shutdown=shared_shutdown, completion_q=completion_q)
    coordinator = Coordinator(
        config,
        github=cast(Any, object()),
        pool=pool,
        install_signals=False,
    )
    # Injected pools expose their queue but not their private shutdown event.
    # Use the same event on both sides, as production Coordinator does.
    coordinator.shutdown = shared_shutdown
    coordinator.stages[StageName.PLANNING] = _SyntheticRecoveryStage()
    monkeypatch.setattr(coordinator, "_seed_pass", lambda: 0)
    for issue in range(workers):
        item = WorkItem(
            repo="performance/worker-pool",
            kind=ItemKind.ISSUE,
            issue=issue,
            stage=StageName.PLANNING,
        )
        assert coordinator._push_item(item, StageName.PLANNING, enter=True)
    return coordinator


def _percentile(samples: list[float], percentile: float) -> float:
    """Return the linearly interpolated percentile (tail-inclusive) in ms.

    Uses interpolation between closest ranks (numpy's default ``linear``
    method): ``rank = percentile * (n - 1)``. Unlike nearest-rank, this
    straddles into the slow tail so a small fraction of very slow
    completions cannot hide below the p95 gate (issue #2229).
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = percentile * (len(ordered) - 1)
    low = int(rank)
    frac = rank - low
    if low + 1 < len(ordered):
        return ordered[low] + frac * (ordered[low + 1] - ordered[low])
    return ordered[low]


def _latency_summary(samples: list[float]) -> _LatencySummary:
    """Summarize a non-empty latency sample list in milliseconds."""
    return _LatencySummary(
        p50=_percentile(samples, 0.50),
        p95=_percentile(samples, 0.95),
        p99=_percentile(samples, 0.99),
        maximum=max(samples, default=0.0),
    )


@pytest.mark.performance
@pytest.mark.parametrize(
    ("percentile", "expected"),
    [(0.50, 5.5), (0.95, 9.55), (0.99, 9.91)],
)
def test_percentile_interpolates_between_closest_ranks(percentile: float, expected: float) -> None:
    """Percentiles interpolate between ranks instead of rounding down a tail."""
    samples = [float(value) for value in range(1, 11)]
    assert _percentile(samples, percentile) == pytest.approx(expected)


@pytest.mark.performance
def test_p95_includes_slow_tail_completion() -> None:
    """A single slow tenth completion pulls p95 into the tail."""
    end_to_end_samples = [10.0] * 9 + [1_000.0]
    assert _latency_summary(end_to_end_samples).p95 == pytest.approx(554.5)


def _write_json_report(report_path: Path, payload: dict[str, object]) -> None:
    """Write measured runtime evidence before threshold assertions run."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_capacity_report(control: _LoadReport, scaled: _LoadReport) -> None:
    """Record both capacity controls and their measured throughput ratio."""
    ratio = scaled.throughput_jobs_per_s / control.throughput_jobs_per_s
    _write_json_report(
        Path(scaled.config.report_path),
        {
            "schema_version": 1,
            "capacity": {
                "control": control.as_json(),
                "scaled": scaled.as_json(),
                "throughput_ratio": ratio,
            },
        },
    )


def _write_report(report: _LoadReport) -> None:
    """Write sustained-load evidence while retaining any capacity comparison."""
    report_path = Path(report.config.report_path)
    payload = report.as_json()
    if report_path.is_file():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        capacity = previous.get("capacity")
        if isinstance(capacity, dict):
            payload["capacity"] = capacity
    _write_json_report(report_path, payload)


def _submit_until_bounded(
    pool: WorkerPool,
    measurements: _Measurements,
    config: LoadConfig,
    *,
    next_job: int,
    total_jobs: int,
    deadline: float | None,
) -> int:
    """Submit work until the configured in-flight or time bound is reached."""
    while (
        next_job < total_jobs
        and measurements.outstanding < config.max_in_flight
        and (deadline is None or time.monotonic() < deadline)
    ):
        job_id = f"load-{next_job}"
        job = BuildTestJob(
            repo="performance/worker-pool",
            cwd=Path.cwd(),
            argv=("synthetic", job_id),
            timeout_s=1,
            descr=job_id,
        )
        submitted_at = time.monotonic()
        measurements.submitted_at[pool.submit(job, StageName.MERGE_WAIT)] = submitted_at
        next_job += 1
    return next_job


def _drain_completion(
    completion_q: CompletionQueue,
    measurements: _Measurements,
    tracker: _Tracker,
    *,
    timeout_s: float,
) -> bool:
    """Record one queued completion, returning false when its wait expires."""
    try:
        handle, result = completion_q.get(timeout=timeout_s)
    except queue.Empty:
        return False
    measurements.record_completion(handle, result, time.monotonic(), tracker)
    return True


def _run_load(
    config: LoadConfig,
    *,
    total_jobs: int,
    duration_s: float | None,
) -> _LoadReport:
    """Exercise the real worker-pool completion path with bounded synthetic work."""
    completion_q = CompletionQueue(capacity=config.max_in_flight)
    shutdown = threading.Event()
    tracker = _Tracker()
    pool = WorkerPool(size=config.workers, shutdown=shutdown, completion_q=completion_q)
    started_at = time.monotonic()
    measurements = _Measurements(started_at)
    submit_deadline = started_at + duration_s if duration_s is not None else None

    def synthetic_handler(_pool: WorkerPool, job: BuildTestJob) -> JobResult:
        """Model a fixed-duration build job without any external process."""
        tracker.started(job.descr)
        try:
            time.sleep(config.service_delay_s)
            return JobResult(ok=True)
        finally:
            tracker.finished()

    with patch.object(WorkerPool, "_run_build_test", synthetic_handler):
        try:
            submitted = 0
            while submitted < total_jobs and (
                submit_deadline is None or time.monotonic() < submit_deadline
            ):
                submitted = _submit_until_bounded(
                    pool,
                    measurements,
                    config,
                    next_job=submitted,
                    total_jobs=total_jobs,
                    deadline=submit_deadline,
                )
                if measurements.outstanding == 0:
                    continue
                timeout_s = (
                    _DRAIN_GRACE_S
                    if submit_deadline is None
                    else max(0.0, submit_deadline - time.monotonic())
                )
                if timeout_s == 0.0 or not _drain_completion(
                    completion_q,
                    measurements,
                    tracker,
                    timeout_s=timeout_s,
                ):
                    break

            drain_started_at = time.monotonic()
            drain_deadline = drain_started_at + _DRAIN_GRACE_S
            while measurements.outstanding:
                timeout_s = drain_deadline - time.monotonic()
                if timeout_s <= 0.0 or not _drain_completion(
                    completion_q,
                    measurements,
                    tracker,
                    timeout_s=timeout_s,
                ):
                    break
        finally:
            pool.shutdown()

    return _LoadReport(
        config=config,
        submitted=len(measurements.submitted_at),
        completed=len(measurements.completed_handles),
        duplicates=measurements.duplicates,
        errors=measurements.errors,
        active_workers=len(tracker.worker_ids),
        peak_concurrency=tracker.peak_active,
        elapsed_s=max(measurements.last_completion_at - started_at, 0.0),
        drain_s=max(measurements.last_completion_at - drain_started_at, 0.0),
        queue_latency_ms=_latency_summary(measurements.queue_latency_ms),
        end_to_end_latency_ms=_latency_summary(measurements.end_to_end_latency_ms),
    )


@pytest.mark.performance
def test_worker_pool_stalled_consumer_preserves_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled coordinator bounds concurrent publication and recovers its seeds."""
    workers = 3
    stalled = _make_recovery_coordinator(monkeypatch, workers, observed=True)
    assert isinstance(stalled.completion_q, _ObservedCompletionQueue)
    assert stalled._work_window == stalled.config.parallel_repos * stalled.config.max_workers
    assert stalled.completion_q.capacity == stalled._work_window == workers
    harness = _StalledConsumerHarness(workers)
    consumer_paused = threading.Event()
    resume_consumer = threading.Event()

    original_wait = stalled._wait_for_completion

    def paused_wait(timeout: float) -> None:
        """Stop the only completion-consumer path once all C seeds are live."""
        if len(stalled.in_flight) == workers and not resume_consumer.is_set():
            consumer_paused.set()
            assert resume_consumer.wait(timeout=5.0)
        original_wait(timeout)

    monkeypatch.setattr(stalled, "_wait_for_completion", paused_wait)
    run_codes: list[int] = []
    run_thread = threading.Thread(target=lambda: run_codes.append(stalled.run()), daemon=True)

    with patch.object(
        WorkerPool,
        "_run_build_test",
        lambda pool, job: harness.run_job(pool, job),
    ):
        run_thread.start()
        assert harness.legitimate_ready.wait(timeout=5.0)
        assert consumer_paused.wait(timeout=5.0)

        # The coordinator owns C legitimate handles. Queue a separate C+1
        # publication burst through the same real pool while all workers are
        # occupied, then release each wave under the stalled consumer.
        for index in range(workers + 1):
            stalled.pool.submit(
                BuildTestJob(
                    repo="performance/worker-pool",
                    cwd=Path.cwd(),
                    argv=("synthetic", f"overflow-{index}"),
                    timeout_s=2,
                    descr=f"overflow-{index}",
                ),
                StageName.PLANNING,
            )

        harness.release_legitimate.set()
        stalled.completion_q.wait_for_offers(workers, timeout=5.0)
        assert harness.overflow_ready.wait(timeout=5.0)
        assert stalled.completion_q.qsize() == workers

        harness.release_overflow.set()
        stalled.completion_q.wait_for_offers((2 * workers) + 1, timeout=5.0)

        # C results occupy the channel, C rejected results occupy its bounded
        # mailbox, and the final publication records overflow. Nothing grows
        # while consumption is paused, and the worker callback requests stop.
        assert run_thread.is_alive()
        assert not resume_consumer.is_set()
        assert harness.legitimate_started == workers
        assert stalled.completion_q.offer_count == (2 * workers) + 1
        assert stalled.completion_q.accepted_count == workers
        assert stalled.completion_q.rejected_count == workers + 1
        assert set(stalled.completion_q.publication_outcomes) == {
            *((f"recovery-{issue}", True) for issue in range(workers)),
            *((f"overflow-{issue}", False) for issue in range(workers + 1)),
        }
        assert stalled.completion_q.result_high_water == workers
        assert stalled.completion_q.rejection_high_water == workers
        assert stalled.completion_q.overflow_observed is True
        assert stalled.completion_q.qsize() == stalled.completion_q.capacity == workers
        assert stalled.shutdown.is_set()

        resume_consumer.set()
        run_thread.join(timeout=5.0)

    assert not run_thread.is_alive()
    assert run_codes and run_codes[0] != 0
    assert len(stalled.items) == workers
    expected_issues = set(range(workers))
    assert {item.issue for item in stalled.items} == expected_issues
    assert all(
        item.result is not None and item.result.reason.startswith("resumable at")
        for item in stalled.items
    )

    recovered = _make_recovery_coordinator(monkeypatch, workers)
    assert recovered.completion_q.capacity == recovered._work_window
    with patch.object(
        WorkerPool,
        "_run_build_test",
        lambda _pool, job: JobResult(ok=True, value=job.descr),
    ):
        assert recovered.run() == 0

    assert len(recovered.items) == workers
    assert {item.issue for item in recovered.items} == expected_issues
    assert all(item.result is not None and item.result.passed for item in recovered.items)


@pytest.mark.performance
def test_worker_pool_capacity_scales(load_config: LoadConfig) -> None:
    """Four workers complete fixed-delay jobs at least twice as fast as one."""
    job_count = min(_CAPACITY_JOBS, load_config.max_jobs)
    control = _run_load(
        replace(load_config, workers=1),
        total_jobs=job_count,
        duration_s=None,
    )
    scaled = _run_load(
        replace(
            load_config,
            workers=4,
            max_in_flight=max(4, load_config.max_in_flight),
        ),
        total_jobs=job_count,
        duration_s=None,
    )
    _write_capacity_report(control, scaled)
    artifact = json.loads(Path(load_config.report_path).read_text(encoding="utf-8"))

    assert control.lost == scaled.lost == 0
    assert control.duplicates == scaled.duplicates == 0
    assert control.errors == scaled.errors == 0
    assert control.active_workers == 1
    assert scaled.active_workers == 4
    assert scaled.throughput_jobs_per_s >= control.throughput_jobs_per_s * 2
    assert artifact["capacity"]["throughput_ratio"] == (
        scaled.throughput_jobs_per_s / control.throughput_jobs_per_s
    )


@pytest.mark.performance
def test_worker_pool_sustains_load_with_bounded_latency(load_config: LoadConfig) -> None:
    """The configured profile drains exactly once with bounded p95 latency."""
    report = _run_load(
        load_config,
        total_jobs=load_config.max_jobs,
        duration_s=load_config.duration_s,
    )
    _write_report(report)
    artifact = json.loads(Path(load_config.report_path).read_text(encoding="utf-8"))

    assert report.submitted > 0
    assert report.lost == 0
    assert report.duplicates == 0
    assert report.errors == 0
    assert report.active_workers == load_config.workers
    assert report.peak_concurrency <= load_config.workers
    assert report.drain_s <= _DRAIN_GRACE_S
    assert report.end_to_end_latency_ms.p95 <= load_config.p95_budget_ms
    assert artifact["invariants"]["lost"] == 0
    assert artifact["concurrency"]["active_workers"] == load_config.workers
