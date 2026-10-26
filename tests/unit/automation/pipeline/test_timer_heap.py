"""Timer-heap, RETRY-delay consumption, and step-watchdog tests (#1817).

The heap owns EVERY wait: stages never sleep (enforced by
``test_pipeline_architecture``); a ``RETRY`` outcome's backoff is recorded in
``item.payload["retry_delay_s"]`` (base.py contract, documented by #1816) and
the coordinator consumes it into the heapq timer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages.base import Stage
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
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
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        parallel_repos=3,
        projects_dir=tmp_path,
        rate_guard_enabled=False,
    )
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        **fake_worker_factories(FakeWorkerPool(), None),
        install_signals=False,
        monotonic=clock.monotonic,
        wall_time=lambda: clock.now,
    )
    return coordinator, clock


def _item(issue: int, stage: StageName = StageName.PR_REVIEW) -> WorkItem:
    return WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=issue, stage=stage, state="POLL")


class TestTimerHeap:
    """Ordering, seq tie-break, and expiry-only waking."""

    def test_expired_timer_keeps_ownership_until_full_stage_accepts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C=1 does not drop an expired timer when its source stage is full."""
        clock = FakeClock()
        coordinator = Coordinator(
            PipelineConfig(
                org="org", repos=["repo-a"], projects_dir=tmp_path, rate_guard_enabled=False
            ),
            github=FakeStageGitHub(),
            **fake_worker_factories(FakeWorkerPool(), None),
            install_signals=False,
            monotonic=clock.monotonic,
            wall_time=lambda: clock.now,
        )
        waiting = _item(1)
        # Park a genuinely admitted item so it owns the sole global permit.
        # Inject the blocker directly to model a stale/corrupt full stage:
        # normal permit accounting cannot create a second live item at C=1,
        # but timer re-entry must still retain ownership instead of losing it.
        coordinator._push_item(waiting, StageName.PR_REVIEW, enter=True)
        assert coordinator._claim_item(StageName.PR_REVIEW) is waiting
        coordinator._timer_park(claim_test_item(coordinator, waiting), 0.0)
        blocker = _item(2)
        coordinator.queues[StageName.PR_REVIEW].push(blocker)

        coordinator._wake_timers()

        # The timer remains its owner; no overflow, loss, or duplicate occurs.
        assert [entry[2] for entry in coordinator.timers] == [waiting]
        assert coordinator.queues[StageName.PR_REVIEW].snapshot() == [blocker]

        coordinator.queues[StageName.PR_REVIEW].pop()
        coordinator._wake_timers()

        assert coordinator.timers == []
        assert coordinator.queues[StageName.PR_REVIEW].snapshot() == [waiting]

    def test_earliest_timer_wakes_first(self, clocked: tuple[Coordinator, FakeClock]) -> None:
        """Wake order follows wake_ts, not insertion order."""
        coordinator, clock = clocked
        ctx_now = coordinator._ctx_for_repo("repo-a").now_fn
        assert ctx_now is not None
        assert ctx_now() == clock.now
        clock.now += 1.0
        assert ctx_now() == clock.now
        coordinator._timer_park(claim_test_item(coordinator, _item(1)), 50.0)
        coordinator._timer_park(claim_test_item(coordinator, _item(2)), 10.0)
        coordinator._timer_park(claim_test_item(coordinator, _item(3)), 30.0)

        coordinator.event_log.clear()
        clock.now += 60.0
        coordinator._wake_timers()

        pushes = [entry for entry in coordinator.event_log if entry[0] == "push"]
        assert [p[2] for p in pushes] == ["repo-a#2", "repo-a#3", "repo-a#1"]

    def test_wake_moves_only_expired_entries(self, clocked: tuple[Coordinator, FakeClock]) -> None:
        """Unexpired timers stay parked."""
        coordinator, clock = clocked
        coordinator._timer_park(claim_test_item(coordinator, _item(1)), 10.0)
        coordinator._timer_park(claim_test_item(coordinator, _item(2)), 100.0)

        clock.now += 20.0
        coordinator._wake_timers()

        assert len(coordinator.timers) == 1
        assert len(coordinator.queues[StageName.PR_REVIEW]) == 1

    def test_seq_tiebreak_prevents_workitem_comparison(
        self, clocked: tuple[Coordinator, FakeClock]
    ) -> None:
        """Two identical wake timestamps must not compare WorkItems (FIFO wins)."""
        coordinator, clock = clocked
        coordinator._timer_park(claim_test_item(coordinator, _item(1)), 5.0)
        coordinator._timer_park(
            claim_test_item(coordinator, _item(2)), 5.0
        )  # would raise without the seq tie-break

        coordinator.event_log.clear()
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

        coordinator._route(
            claim_test_item(coordinator, item), StageOutcome(Disposition.RETRY, "ci pending")
        )

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

        coordinator._route(
            claim_test_item(coordinator, item), StageOutcome(Disposition.RETRY, "transient")
        )

        assert coordinator.timers == []
        assert len(coordinator.queues[StageName.PR_REVIEW]) == 1

    def test_retry_preserves_stage_and_state(self, clocked: tuple[Coordinator, FakeClock]) -> None:
        """RETRY re-steps the SAME stage state; it never re-runs on_enter."""
        coordinator, clock = clocked
        item = _item(11)
        item.payload["retry_delay_s"] = 1.0

        coordinator._route(
            claim_test_item(coordinator, item), StageOutcome(Disposition.RETRY, "poll")
        )
        clock.now += 2.0
        coordinator._wake_timers()

        assert item.stage is StageName.PR_REVIEW
        assert item.state == "POLL"
        assert item.payload.get("_enter_pending") is not True


class TestStepWatchdog:
    """WARN when a stage.step breaches the <~60s protocol contract."""

    def test_watchdog_allows_step_at_sixty_seconds(
        self,
        clocked: tuple[Coordinator, FakeClock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A 60-second step remains within the coordinator contract."""
        coordinator, clock = clocked

        class WithinContractStage(Stage):
            def on_enter(self, item: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, item: WorkItem, ctx: Any) -> Any:
                clock.now += 60.0
                return StageOutcome(Disposition.SKIP, "within_contract")

            def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.PR_REVIEW] = WithinContractStage()

        with caplog.at_level("WARNING"):
            coordinator._run_item(claim_test_item(coordinator, _item(11)))

        assert not any("stage.step stalled" in record.message for record in caplog.records)

    def test_watchdog_warns_on_slow_step(
        self,
        clocked: tuple[Coordinator, FakeClock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A step exceeding _STEP_WATCHDOG_S logs the stall warning."""
        coordinator, clock = clocked

        class SlowStage(Stage):
            def on_enter(self, item: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, item: WorkItem, ctx: Any) -> Any:
                clock.now += 60.1
                return StageOutcome(Disposition.SKIP, "slow")

            def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.PR_REVIEW] = SlowStage()
        item = _item(12)

        with caplog.at_level("WARNING"):
            coordinator._run_item(claim_test_item(coordinator, item))

        assert any("stage.step stalled" in record.message for record in caplog.records)

    def test_watchdog_silent_on_fast_step(
        self,
        clocked: tuple[Coordinator, FakeClock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A fast step logs no stall warning."""
        coordinator, _clock = clocked

        class FastStage(Stage):
            def on_enter(self, item: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, item: WorkItem, ctx: Any) -> Any:
                return StageOutcome(Disposition.SKIP, "fast")

            def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.PR_REVIEW] = FastStage()

        with caplog.at_level("WARNING"):
            coordinator._run_item(claim_test_item(coordinator, _item(13)))

        assert not any("stalled" in record.message for record in caplog.records)

    def test_bounded_source_preparation_retries_before_watchdog_and_submission(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A held source lane timer-parks planning without an agent job."""
        repo = tmp_path / "repo-a"
        repo.mkdir()

        def run_git(*args: str) -> str:
            return subprocess.run(
                ["git", *args],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        run_git("init", "-b", "main")
        run_git("config", "user.email", "test@example.invalid")
        run_git("config", "user.name", "Test User")
        (repo / "tracked.txt").write_text("source\n", encoding="utf-8")
        run_git("add", "tracked.txt")
        run_git("commit", "-m", "initial")
        revision = run_git("rev-parse", "HEAD")

        config = PipelineConfig(
            org="org", repos=["repo-a"], projects_dir=tmp_path, rate_guard_enabled=False
        )
        pool = FakeWorkerPool()
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            **fake_worker_factories(pool, None),
            install_signals=False,
        )
        manager = SourceWorkspaceManager(repo, repository="org/repo-a")
        item = WorkItem(
            repo="repo-a",
            kind=ItemKind.ISSUE,
            issue=2983,
            stage=StageName.PLANNING,
            state="ADVISE_WAIT",
            payload={"_synced_default_branch_sha": revision},
        )

        with file_lock(
            manager._lane_lock_path(2983, SourceLane.REVIEW),
            require_exclusive=True,
        ):
            with caplog.at_level("WARNING"):
                coordinator._run_item(claim_test_item(coordinator, item))

        assert pool.submitted == []
        assert item.attempts["source_workspace"] == 1
        assert len(coordinator.timers) == 1
        assert any("lane_lock_unavailable" in record.message for record in caplog.records)
        assert not any("stage.step stalled" in record.message for record in caplog.records)
        assert any(event[0] == "timer_park" for event in coordinator.event_log)
