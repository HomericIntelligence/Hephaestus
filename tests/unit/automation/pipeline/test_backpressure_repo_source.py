"""Repository-discovery source-pull regression coverage for bounded queues (#2399)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import loop_repo_manager
from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.stages.base import Continue
from hephaestus.automation.pipeline.stages.repo import RepoStage
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _ImmediatePassStage:
    """Finish planning synchronously so source admission order is observable."""

    def __init__(self, events: list[tuple[str, int]]) -> None:
        self._events = events

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx

    def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
        del ctx
        assert item.issue is not None
        self._events.append(("complete", item.issue))
        return StageOutcome(Disposition.FINISH_PASS, f"completed #{item.issue}")

    def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
        del item, result, ctx
        raise AssertionError("source-pull test must not submit a worker job")


def _facts(issue: int) -> IssueFacts:
    """Return one eligible, planning-entry issue classification."""
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


def test_repo_discovery_never_materializes_an_unbounded_products_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo source must retain a cursor, not all C+1 classified products.

    The bounded pipeline's only work-holding queues are coordinator-owned.
    Discovery metadata may be read one page at a time, but the repo WorkItem
    cannot turn every eligible issue into a ``payload["products"]`` spill list
    before the planning queue has made capacity.
    """
    repo_item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO, state="DISCOVER")
    ctx = type(
        "Context",
        (),
        {
            "org": "org",
            "github": FakeStageGitHub(labels=["state:needs-plan"]),
            "config": type("Config", (), {"drive_green_all": False})(),
        },
    )()
    metadata = [
        {"number": 101, "labels": ["state:needs-plan"], "title": "first"},
        {"number": 102, "labels": ["state:needs-plan"], "title": "second"},
    ]
    monkeypatch.setattr(loop_repo_manager, "_list_open_issue_meta", lambda _org, _repo: metadata)
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", lambda issue, _github: _facts(issue))

    result = RepoStage().step(repo_item, ctx)

    assert isinstance(result, Continue)
    assert "products" not in repo_item.payload


def test_repo_issue_source_is_lossless_and_ordered_at_capacity_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C+1 discovered issues classify only as C=1 capacity becomes available.

    The first issue may enter and finish before the second is classified.  No
    internal saturation is a shutdown/fatal error, every issue finishes once,
    and source order remains stable across the admission boundary.
    """
    events: list[tuple[str, int]] = []
    metadata = [
        {"number": 101, "labels": ["state:needs-plan"], "title": "first"},
        {"number": 102, "labels": ["state:needs-plan"], "title": "second"},
    ]

    def classify(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        return _facts(issue)

    monkeypatch.setattr(loop_repo_manager, "_list_open_issue_meta", lambda _org, _repo: metadata)
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)

    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            parallel_repos=1,
            max_workers=1,
            dry_run=True,
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
    discovered = [item for item in coordinator.items if item.kind is ItemKind.ISSUE]
    assert [item.issue for item in discovered] == [101, 102]
    assert all(item.result is not None and item.result.passed for item in discovered)
    assert coordinator.shutdown.is_set() is False
    assert coordinator._fatal is False
    assert coordinator._all_idle()
