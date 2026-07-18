"""Timer-heap, RETRY-delay consumption, and step-watchdog tests (#1817).

The heap owns EVERY wait: stages never sleep (enforced by
``test_pipeline_architecture``); a ``RETRY`` outcome's backoff is recorded in
``item.payload["retry_delay_s"]`` (base.py contract, documented by #1816) and
the coordinator consumes it into the heapq timer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.automation.pipeline import coordinator as coordinator_mod, seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import (
    _STEP_WATCHDOG_S,
    Coordinator,
    PipelineConfig,
)
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class FakeClock:
    """Deterministic monotonic clock for the coordinator's timer logic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Coordinator, FakeClock]:
    """Coordinator whose time module is replaced with a fake clock."""
    clock = FakeClock()
    monkeypatch.setattr(
        coordinator_mod,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, time=lambda: clock.now),
    )
    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
    config = PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path)
    coordinator = Coordinator(
        config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
    )
    return coordinator, clock


def _item(issue: int, stage: StageName = StageName.PR_REVIEW) -> WorkItem:
    return WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=issue, stage=stage, state="POLL")


class TestTimerHeap:
    """Ordering, seq tie-break, and expiry-only waking."""

    def test_earliest_timer_wakes_first(self, clocked: tuple[Coordinator, FakeClock]) -> None:
        """Wake order follows wake_ts, not insertion order."""
        coordinator, clock = clocked
        coordinator._timer_park(_item(1), 50.0)
        coordinator._timer_park(_item(2), 10.0)
        coordinator._timer_park(_item(3), 30.0)

        clock.now += 60.0
        coordinator._wake_timers()

        pushes = [entry for entry in coordinator.event_log if entry[0] == "push"]
        assert [p[2] for p in pushes] == ["repo-a#2", "repo-a#3", "repo-a#1"]

    def test_wake_moves_only_expired_entries(self, clocked: tuple[Coordinator, FakeClock]) -> None:
        """Unexpired timers stay parked."""
        coordinator, clock = clocked
        coordinator._timer_park(_item(1), 10.0)
        coordinator._timer_park(_item(2), 100.0)

        clock.now += 20.0
        coordinator._wake_timers()

        assert len(coordinator.timers) == 1
        assert len(coordinator.queues[StageName.PR_REVIEW]) == 1

    def test_seq_tiebreak_prevents_workitem_comparison(
        self, clocked: tuple[Coordinator, FakeClock]
    ) -> None:
        """Two identical wake timestamps must not compare WorkItems (FIFO wins)."""
        coordinator, clock = clocked
        coordinator._timer_park(_item(1), 5.0)
        coordinator._timer_park(_item(2), 5.0)  # would raise without the seq tie-break

        clock.now += 6.0
        coordinator._wake_timers()

        pushes = [entry[2] for entry in coordinator.event_log if entry[0] == "push"]
        assert pushes == ["repo-a#1", "repo-a#2"]


class TestRetryDelayConsumption:
    """RETRY timer contract: payload["retry_delay_s"] -> heap park."""

    def test_retry_with_delay_parks_on_heap(self, clocked: tuple[Coordinator, FakeClock]) -> None:
        """The stage-recorded backoff is consumed into the timer heap."""
        coordinator, _clock = clocked
        item = _item(9)
        item.payload["retry_delay_s"] = 42.0

        coordinator._route(item, StageOutcome(Disposition.RETRY, "ci pending"))

        assert "retry_delay_s" not in item.payload  # consumed, not stale
        assert len(coordinator.timers) == 1
        wake_ts, _seq, parked = coordinator.timers[0]
        assert parked is item
        assert wake_ts == pytest.approx(1000.0 + 42.0)

    def test_retry_without_delay_requeues_for_next_tick(
        self, clocked: tuple[Coordinator, FakeClock]
    ) -> None:
        """A missing key means retry on the next drain tick (no timer)."""
        coordinator, _clock = clocked
        item = _item(10)

        coordinator._route(item, StageOutcome(Disposition.RETRY, "transient"))

        assert coordinator.timers == []
        assert len(coordinator.queues[StageName.PR_REVIEW]) == 1

    def test_retry_preserves_stage_and_state(self, clocked: tuple[Coordinator, FakeClock]) -> None:
        """RETRY re-steps the SAME stage state; it never re-runs on_enter."""
        coordinator, clock = clocked
        item = _item(11)
        item.payload["retry_delay_s"] = 1.0

        coordinator._route(item, StageOutcome(Disposition.RETRY, "poll"))
        clock.now += 2.0
        coordinator._wake_timers()

        assert item.stage is StageName.PR_REVIEW
        assert item.state == "POLL"
        assert item.payload.get("_enter_pending") is not True


class TestStepWatchdog:
    """WARN when a stage.step breaches the <~15s protocol contract."""

    def test_watchdog_warns_on_slow_step(
        self,
        clocked: tuple[Coordinator, FakeClock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A step exceeding _STEP_WATCHDOG_S logs the stall warning."""
        coordinator, clock = clocked

        class SlowStage:
            def on_enter(self, item: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, item: WorkItem, ctx: Any) -> Any:
                clock.now += _STEP_WATCHDOG_S + 3.0  # simulate a stalled step
                return StageOutcome(Disposition.SKIP, "slow")

            def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.PR_REVIEW] = SlowStage()
        item = _item(12)

        with caplog.at_level("WARNING"):
            coordinator._run_item(item)

        assert any("stage.step stalled" in record.message for record in caplog.records)

    def test_watchdog_silent_on_fast_step(
        self,
        clocked: tuple[Coordinator, FakeClock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A fast step logs no stall warning."""
        coordinator, _clock = clocked

        class FastStage:
            def on_enter(self, item: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, item: WorkItem, ctx: Any) -> Any:
                return StageOutcome(Disposition.SKIP, "fast")

            def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.PR_REVIEW] = FastStage()

        with caplog.at_level("WARNING"):
            coordinator._run_item(_item(13))

        assert not any("stalled" in record.message for record in caplog.records)
