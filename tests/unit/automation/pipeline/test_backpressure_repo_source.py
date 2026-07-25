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
    monkeypatch.setattr(
        loop_repo_manager, "_iter_open_issue_meta", lambda _org, _repo: iter(metadata)
    )
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

    monkeypatch.setattr(
        loop_repo_manager, "_iter_open_issue_meta", lambda _org, _repo: iter(metadata)
    )
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


def test_repo_entries_are_source_pulled_in_order_at_capacity_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C+1 repositories wait at one FIFO cursor instead of being dropped.

    A repository source holds the sole REPO-stage lease through its bounded
    issue cursor.  The next configured repository is therefore admitted only
    after that source has drained, preserving source order without creating an
    unbounded list of repo ``WorkItem`` instances.
    """
    events: list[tuple[str, int]] = []
    metadata = {
        "repo-a": [{"number": 101, "labels": ["state:needs-plan"], "title": "first"}],
        "repo-b": [{"number": 201, "labels": ["state:needs-plan"], "title": "second"}],
    }

    def classify(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        return _facts(issue)

    monkeypatch.setattr(
        loop_repo_manager,
        "_iter_open_issue_meta",
        lambda _org, repo: iter(metadata[repo]),
    )
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)

    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a", "repo-b"],
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
        ("classify", 201),
        ("complete", 201),
    ]
    assert [item.issue for item in coordinator.items if item.kind is ItemKind.ISSUE] == [101, 201]
    assert all(item.kind is not ItemKind.REPO for item in coordinator.items)
    assert coordinator.live_work_count == 0
    assert coordinator._all_idle()


def test_repo_sources_round_robin_across_repositories_at_capacity_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C=2 admits one source item from A and B before either gets a second turn."""
    events: list[tuple[str, int]] = []
    metadata = {
        "repo-a": [
            {"number": 101, "labels": ["state:needs-plan"], "title": "first A"},
            {"number": 102, "labels": ["state:needs-plan"], "title": "second A"},
        ],
        "repo-b": [
            {"number": 201, "labels": ["state:needs-plan"], "title": "first B"},
            {"number": 202, "labels": ["state:needs-plan"], "title": "second B"},
        ],
    }

    def classify(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        return _facts(issue)

    monkeypatch.setattr(
        loop_repo_manager,
        "_iter_open_issue_meta",
        lambda _org, repo: iter(metadata[repo]),
    )
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a", "repo-b"],
            loops=1,
            parallel_repos=1,
            max_workers=2,
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
        ("classify", 201),
        ("complete", 101),
        ("complete", 201),
        ("classify", 102),
        ("classify", 202),
        ("complete", 102),
        ("complete", 202),
    ]
    assert coordinator._all_idle()
    assert coordinator.live_work_count == 0


def test_repo_source_reseed_drains_second_pass_before_zero_work_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh repo cursor keeps loop two alive until it classifies its issue.

    The first pass fails and the second succeeds for the same discovered
    issue.  A repository source begins with no classified work, so returning
    from reseed based only on ``_pass_work_count`` would exit immediately and
    strand the second cursor.
    """
    events: list[tuple[str, int]] = []

    class _FailThenPassStage:
        def on_enter(self, item: WorkItem, ctx: Any) -> None:
            del item, ctx

        def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
            del ctx
            assert item.issue == 101
            if not any(kind == "fail" for kind, _number in events):
                events.append(("fail", item.issue))
                return StageOutcome(Disposition.FINISH_FAIL, "first pass failed")
            events.append(("pass", item.issue))
            return StageOutcome(Disposition.FINISH_PASS, "replacement passed")

        def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
            del item, result, ctx
            raise AssertionError("the deterministic stage must not submit a job")

    def classify(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        return _facts(issue)

    monkeypatch.setattr(
        loop_repo_manager,
        "_iter_open_issue_meta",
        lambda _org, _repo: iter(
            [{"number": 101, "labels": ["state:needs-plan"], "title": "retry"}]
        ),
    )
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=2,
            parallel_repos=1,
            max_workers=1,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _FailThenPassStage()

    assert coordinator.run() == 0

    assert events == [
        ("classify", 101),
        ("fail", 101),
        ("classify", 101),
        ("pass", 101),
    ]
    assert coordinator._loops_run == 2
    assert coordinator._terminal_summary.dispositions == {"pass": 1}


def test_empty_repo_source_still_converges_after_one_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drained source with no actionable issue does not cause a reseed loop."""
    monkeypatch.setattr(loop_repo_manager, "_iter_open_issue_meta", lambda _org, _repo: iter(()))
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=3,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )

    assert coordinator.run() == 0
    assert coordinator._loops_run == 1
    assert coordinator._all_idle()


def test_repo_source_tags_epic_before_exclusion_and_next_issue_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An epic is durably excluded before the cursor advances to live work."""
    events: list[tuple[str, int]] = []
    metadata = [
        {"number": 5, "labels": ["epic"], "title": "Epic: umbrella"},
        {"number": 6, "labels": ["state:needs-plan"], "title": "implementation"},
    ]

    class _RecordingGitHub(FakeStageGitHub):
        def skip_epics(self, epics_labels: dict[int, list[str]]) -> None:
            events.extend(("tag", number) for number in epics_labels)
            super().skip_epics(epics_labels)

    def classify(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        return _facts(issue)

    monkeypatch.setattr(
        loop_repo_manager, "_iter_open_issue_meta", lambda _org, _repo: iter(metadata)
    )
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    github = _RecordingGitHub(labels=["state:needs-plan"])
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
        github=github,
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage(events)

    assert coordinator.run() == 0
    assert events == [("tag", 5), ("classify", 6), ("complete", 6)]
    assert "state:skip" in github.labels[5]
