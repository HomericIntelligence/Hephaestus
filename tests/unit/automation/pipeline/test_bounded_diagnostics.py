"""Bounded in-process diagnostics and terminal summary retention (#2399)."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.seeding import SeedEntry
from hephaestus.automation.pipeline.stages.base import Stage
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    fake_worker_factories,
    script_source_passes,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _coordinator(tmp_path: Path) -> Coordinator:
    """Create a C=1 coordinator with deliberately small diagnostic bounds."""
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        projects_dir=tmp_path,
        metrics_port=9123,
        event_log_path=tmp_path / "events.jsonl",
        event_log_capacity=3,
        terminal_detail_capacity=2,
        rate_guard_enabled=False,
    )
    return Coordinator(
        config,
        github=FakeStageGitHub(),
        **fake_worker_factories(FakeWorkerPool(), None),
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


def _finding_record(issue: int) -> dict[str, object]:
    """Build one complete review-finding record with a private body."""
    return {
        "finding_id": f"{issue:064x}",
        "source_head": "a" * 40,
        "severity": "major",
        "body": f"private body {issue}",
        "evidence": f"private evidence {issue}",
        "original_anchor": {"path": f"file-{issue}.py", "line": issue, "side": "RIGHT"},
        "final_anchor": {"path": f"file-{issue}.py", "line": issue, "side": "RIGHT"},
        "status": "published",
        "surface": "inline",
        "reason": None,
    }


def test_coordinator_metric_policy_rejects_unknown_stage(tmp_path: Path) -> None:
    """The coordinator's queue-depth family rejects undeclared stages."""
    coordinator = _coordinator(tmp_path)
    coordinator._emit_observability_tick()
    registry = coordinator._metrics_registry
    assert registry is not None

    with pytest.raises(ValueError, match="not allowed"):
        registry.gauge("hephaestus_pipeline_queue_depth").set(
            1,
            labels={"stage": "adversarial"},
        )


class _TerminalStage(Stage):
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
    coordinator = Coordinator(
        PipelineConfig(
            org="org", repos=["repo-a"], loops=2, projects_dir=tmp_path, rate_guard_enabled=False
        ),
        github=FakeStageGitHub(),
        **fake_worker_factories(FakeWorkerPool(), None),
        install_signals=False,
    )
    script_source_passes(coordinator, monkeypatch, list(passes))
    coordinator.stages[StageName.PLANNING] = _TerminalStage(
        StageOutcome(Disposition.FINISH_FAIL, "first failed"),
        StageOutcome(Disposition.FINISH_PASS, "replacement passed"),
    )

    assert coordinator.run() == 0
    assert coordinator._terminal_summary.total == 1
    assert coordinator._terminal_summary.dispositions == {"pass": 1}
