"""Direct-scope source-pull regression coverage for bounded queues (#2399)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.queues import StageQueue
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.work_item import WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _ImmediatePassStage:
    """A no-I/O planning stage that makes admission progress observable."""

    def __init__(self, events: list[tuple[str, int]]) -> None:
        """Record stage progress alongside the seed-classification trace."""
        self._events = events

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        """Leave the item ready for its deterministic one-step completion."""
        del item, ctx

    def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
        """Complete one seeded issue without submitting a worker job."""
        del ctx
        assert item.issue is not None
        self._events.append(("complete", item.issue))
        return StageOutcome(Disposition.FINISH_PASS, f"completed #{item.issue}")

    def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
        """Reject a worker path; this test exercises only queue admission."""
        del item, result, ctx
        raise AssertionError("the deterministic stage must not submit a job")


def test_direct_issue_seeds_are_source_pulled_and_lossless_at_capacity_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C+1 direct issues wait at a bounded source cursor until each prior item drains.

    ``--issues`` is an operator-provided source, not permission to build an
    unbounded admission buffer.  With C=1, the second classification must
    happen only after the first item has made pipeline progress; both issues
    must then finish exactly once without turning internal saturation into a
    shutdown or fatal run.
    """
    events: list[tuple[str, int]] = []
    observed_occupancies: list[int] = []
    original_offer = StageQueue.offer

    def record_offer(self: StageQueue, item: WorkItem) -> bool:
        accepted = original_offer(self, item)
        observed_occupancies.append(self.occupancy)
        return accepted

    def classify_direct_issue(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        return IssueFacts(
            number=issue,
            title=f"Issue {issue}",
            body="",
            is_epic=False,
            labels={"state:needs-plan"},
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )

    monkeypatch.setattr(StageQueue, "offer", record_offer)
    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda *_args: [])
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify_direct_issue)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: issues,
    )

    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101, 102],
            loops=1,
            parallel_repos=1,
            max_workers=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage(events)

    assert coordinator.run() == 0

    assert events == [
        ("classify", 101),
        ("complete", 101),
        ("classify", 102),
        ("complete", 102),
    ]
    assert [item.issue for item in coordinator.items] == [101, 102]
    assert all(item.result is not None and item.result.passed for item in coordinator.items)
    assert coordinator.shutdown.is_set() is False
    assert coordinator._fatal is False
    assert coordinator._all_idle()
    assert observed_occupancies
    assert max(observed_occupancies) <= 1
