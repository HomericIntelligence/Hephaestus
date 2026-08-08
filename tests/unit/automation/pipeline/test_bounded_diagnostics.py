"""Bounded in-process diagnostics and terminal summary retention (#2399)."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.seeding import SeedEntry
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _coordinator(tmp_path: Path) -> Coordinator:
    """Create a C=1 coordinator with deliberately small diagnostic bounds."""
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        projects_dir=tmp_path,
        metrics_port=9123,
        event_log_capacity=3,
        terminal_detail_capacity=2,
    )
    return Coordinator(
        config,
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )


def _finished_item(issue: int, *, passed: bool) -> WorkItem:
    """Build one terminal item for the coordinator-owned finished sink."""
    return WorkItem(
        repo="repo-a",
        kind=ItemKind.ISSUE,
        issue=issue,
        stage=StageName.FINISHED,
        result=ItemResult(
            passed=passed,
            reason="ok" if passed else "failed",
            final_stage=StageName.FINISHED,
        ),
    )


def test_coordinator_bounds_diagnostics_and_keeps_full_terminal_aggregates(
    tmp_path: Path,
) -> None:
    """Metric ticks, repo contexts, and old terminal detail cannot grow with an org."""
    coordinator = _coordinator(tmp_path)

    for _ in range(5):
        coordinator._emit_observability_tick()
    assert len(coordinator.event_log) == 3
    assert all(event[0] == "metrics_snapshot" for event in coordinator.event_log)

    for repo in ("repo-a", "repo-b", "repo-c"):
        coordinator._ctx_for_repo(repo)
    assert list(coordinator._ctx_cache) == ["repo-c"]

    for issue in range(1, 5):
        item = _finished_item(issue, passed=issue != 2)
        assert coordinator._push_item(item, StageName.FINISHED, enter=True)
        coordinator._drain_queues()

    # The diagnostic rings retain only recent data, including generated metric
    # events, while the terminal aggregate still reflects all four outcomes.
    assert len(coordinator.event_log) == 3
    assert len(coordinator._ctx_cache) == 1
    assert [item.issue for item in coordinator.items] == [3, 4]
    assert [result.reason for result in coordinator.ledger] == ["ok", "ok"]
    assert coordinator._terminal_summary.total == 4
    assert coordinator._terminal_summary.dispositions == {"fail": 1, "pass": 3}
    assert coordinator._exit_code() == 1


class _TerminalStage:
    """Return one terminal outcome per re-seeded planning item."""

    def __init__(self, *outcomes: StageOutcome) -> None:
        self._outcomes = deque(outcomes)

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx
        return None

    def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
        del item, ctx
        return self._outcomes.popleft()

    def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
        del item, result, ctx


def test_terminal_summary_uses_only_the_latest_reseed_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful replacement must supersede an earlier failed loop outcome."""
    passes = deque(
        [
            [SeedEntry(kind="issue", identifier=7, stage=StageName.PLANNING, reason="first")],
            [SeedEntry(kind="issue", identifier=7, stage=StageName.PLANNING, reason="second")],
        ]
    )
    monkeypatch.setattr(
        seeding_mod,
        "seed_from_cli",
        lambda _repos, _issues, _prs: list(passes.popleft()) if passes else [],
    )
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo-a"], loops=2, projects_dir=tmp_path),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _TerminalStage(
        StageOutcome(Disposition.FINISH_FAIL, "first failed"),
        StageOutcome(Disposition.FINISH_PASS, "replacement passed"),
    )

    assert coordinator.run() == 0
    assert coordinator._terminal_summary.total == 1
    assert coordinator._terminal_summary.dispositions == {"pass": 1}
