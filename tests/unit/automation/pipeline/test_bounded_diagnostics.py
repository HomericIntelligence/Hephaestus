"""Bounded in-process diagnostics and terminal summary retention (#2399)."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.github_api.diff import normalize_review_finding_records
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.seeding import SeedEntry
from hephaestus.automation.pipeline.stages.base import Stage
from hephaestus.automation.pipeline.stages.pr_review_audit import PrReviewAudit
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


def test_terminal_finding_events_stream_before_item_detail_eviction(tmp_path: Path) -> None:
    """JSONL keeps bounded finding identities after terminal details roll over."""
    coordinator = _coordinator(tmp_path)
    event_log_path = tmp_path / "events.jsonl"

    for issue in range(1, 5):
        item = _finished_item(issue, passed=True)
        item.payload["review_finding_records"] = [_finding_record(issue)]
        coordinator.items.append(item)
        coordinator._record_terminal_result(item)

    assert [item.issue for item in coordinator.items] == [3, 4]
    records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
    finding_events = [record for record in records if record["event"] == "review_finding_outcome"]
    assert [event["fields"][0]["finding_id"] for event in finding_events] == [
        f"{issue:064x}" for issue in range(1, 5)
    ]
    assert [event["fields"][0]["original_anchor"]["path"] for event in finding_events] == [
        f"file-{issue}.py" for issue in range(1, 5)
    ]
    assert all("body" not in event["fields"][0] for event in finding_events)
    assert all("evidence" not in event["fields"][0] for event in finding_events)


def test_terminal_finding_events_include_compacted_identities(tmp_path: Path) -> None:
    """JSONL keeps the bounded identity and outcome after full-record compaction."""
    coordinator = _coordinator(tmp_path)
    event_log_path = tmp_path / "events.jsonl"
    item = _finished_item(1, passed=True)
    item.pr = 1001
    item.payload["review_finding_records"] = []
    item.payload["review_finding_compacted_outcomes"] = {
        "counts": {"corrected": 0, "not_publishable": 1, "published": 0},
        "identities": [["f" * 64, "9" * 40, "n", "b"]],
    }

    coordinator._record_terminal_result(item)

    records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
    compacted = [
        record for record in records if record["event"] == "review_finding_compacted_outcome"
    ]
    assert len(compacted) == 1
    assert compacted[0]["fields"] == [
        {
            "blocking": True,
            "finding_id": "f" * 64,
            "issue": 1,
            "outcome": "not_publishable",
            "pr": 1001,
            "repo": "repo-a",
            "source_head": "9" * 40,
        }
    ]


def test_restarted_go_history_reaches_terminal_summary_and_diagnostics(tmp_path: Path) -> None:
    """Restored pending GO history supplies terminal counters and bounded events."""
    coordinator = _coordinator(tmp_path)
    item = _finished_item(1, passed=True)
    item.pr = 1001
    item.payload["pending_implementation_go_audit_findings"] = [_finding_record(1)]
    item.payload["pending_implementation_go_audit_compacted_outcomes"] = {
        "counts": {"corrected": 1, "not_publishable": 0, "published": 0},
        "identities": [["f" * 64, "9" * 40, "c", "a"]],
    }

    PrReviewAudit._restore_pending_go_finding_history(item)
    coordinator._record_terminal_result(item)

    assert coordinator._terminal_summary.review_finding_outcomes == {
        "corrected": 1,
        "published": 1,
    }
    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert any(record["event"] == "review_finding_compacted_outcome" for record in records)


def test_legacy_boundary_terminal_history_emits_compacted_diagnostic(tmp_path: Path) -> None:
    """A valid old list emits an exact terminal identity after bounded conversion."""
    low = 1
    high = 16_384
    legacy_record: dict[str, object] | None = None
    while low <= high:
        size = (low + high) // 2
        candidate: dict[str, object] = {
            **_finding_record(1),
            "body": "<" * size,
            "evidence": "e" * 1_000,
        }
        try:
            normalize_review_finding_records([candidate])
        except ValueError:
            high = size - 1
        else:
            legacy_record = candidate
            low = size + 1
    assert legacy_record is not None
    coordinator = _coordinator(tmp_path)
    item = _finished_item(1, passed=True)
    item.pr = 1001
    item.payload["review_finding_records"] = [legacy_record]
    item.payload["review_finding_compacted_outcomes"] = {
        "counts": {"corrected": 0, "not_publishable": 0, "published": 0},
        "identities": [],
    }

    coordinator._record_terminal_result(item)

    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    compacted = [
        record for record in records if record["event"] == "review_finding_compacted_outcome"
    ]
    assert compacted[0]["fields"][0]["finding_id"] == legacy_record["finding_id"]


def test_legacy_boundary_pending_history_emits_full_pending_diagnostic(tmp_path: Path) -> None:
    """A maximal old pending list emits its identity without a terminal claim."""
    low = 1
    high = 16_384
    legacy_record: dict[str, object] | None = None
    while low <= high:
        size = (low + high) // 2
        candidate: dict[str, object] = {
            **_finding_record(1),
            "body": "<" * size,
            "evidence": "e" * 1_000,
            "final_anchor": {"path": "file-1.py", "line": 1, "side": "RIGHT"},
            "status": "pending",
            "surface": "inline",
            "reason": None,
        }
        try:
            normalize_review_finding_records([candidate])
        except ValueError:
            high = size - 1
        else:
            legacy_record = candidate
            low = size + 1
    assert legacy_record is not None
    coordinator = _coordinator(tmp_path)
    item = _finished_item(1, passed=False)
    item.pr = 1001
    item.payload["review_finding_records"] = [legacy_record]
    item.payload["review_finding_compacted_outcomes"] = {
        "counts": {"corrected": 0, "not_publishable": 0, "published": 0},
        "identities": [],
    }

    coordinator._record_terminal_result(item)

    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    pending = [record for record in records if record["event"] == "review_finding_outcome"]
    compacted = [
        record for record in records if record["event"] == "review_finding_compacted_outcome"
    ]
    assert pending[0]["fields"][0]["finding_id"] == legacy_record["finding_id"]
    assert pending[0]["fields"][0]["status"] == "pending"
    assert compacted == []


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
        record = _finding_record(issue)
        if issue == 4:
            record.update(
                {
                    "final_anchor": None,
                    "status": "not_publishable",
                    "surface": "not_publishable",
                    "reason": "line_not_in_diff",
                }
            )
        item.payload["review_finding_records"] = [record]
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
    assert coordinator._terminal_summary.review_finding_outcomes == {
        "not_publishable": 1,
        "published": 3,
    }
    assert coordinator._exit_code() == 1


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
