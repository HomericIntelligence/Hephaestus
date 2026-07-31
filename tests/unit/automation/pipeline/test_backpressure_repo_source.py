"""Repository-discovery source-pull regression coverage for bounded queues."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import loop_repo_manager, pr_discovery
from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import (
    Coordinator,
    PipelineConfig,
    _ActiveRepoIssueSource,
)
from hephaestus.automation.pipeline.routing import (
    PIPELINE_ORDER,
    Disposition,
    StageName,
    StageOutcome,
)
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.stages.repo import RepoIssueSource, RepoStage
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _ImmediatePassStage:
    """Finish planning synchronously so admission order is observable."""

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


class _ImmediatePrPassStage:
    """Finish PR review synchronously so bounded PR admission is observable."""

    def __init__(self, events: list[int]) -> None:
        self._events = events

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx

    def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
        del ctx
        assert item.pr is not None
        self._events.append(item.pr)
        return StageOutcome(Disposition.FINISH_PASS, f"reviewed PR #{item.pr}")

    def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
        del item, result, ctx
        raise AssertionError("source-pull test must not submit a worker job")


def _facts(issue: int) -> IssueFacts:
    """Return one eligible planning-entry issue classification."""
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


def test_large_repo_discovery_retains_only_page_cursor_and_one_pending_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten thousand issues never become a products list or resident WorkItems."""
    produced = 0

    def metadata_source(_org: str, _repo: str):
        nonlocal produced
        for issue in range(1, 10_001):
            produced += 1
            yield {"number": issue, "labels": ["state:needs-plan"], "title": f"Issue {issue}"}

    monkeypatch.setattr(loop_repo_manager, "_iter_open_issue_meta", metadata_source)
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            parallel_repos=1,
            max_workers=1,
            stage_queue_capacity=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    repo_item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO, state="DISCOVER")
    ctx = coordinator._ctx_for_repo("repo-a")

    result = RepoStage().step(repo_item, ctx)
    repo_item.state = result.next_state  # type: ignore[union-attr]
    source = repo_item.payload["_repo_issue_source"]
    assert coordinator._externalize_repo_issue_source(repo_item, source)

    # Fill every possible downstream stage and the bounded spool so the
    # cursor can retain only one not-yet-classified metadata row.
    for stage in PIPELINE_ORDER:
        if stage is StageName.REPO:
            continue
        blocker = WorkItem(repo="block", kind=ItemKind.REPO, stage=stage)
        assert coordinator.queues[stage].offer(blocker)
    for _index in range(coordinator._admission_spool_capacity):
        coordinator._pending_admissions.append(
            (
                WorkItem(repo="pending", kind=ItemKind.REPO),
                StageName.PLANNING,
                True,
            )
        )

    coordinator._drain_repo_issue_sources()

    assert produced == 1
    assert len(coordinator._repo_issue_sources) == 1
    active = coordinator._repo_issue_sources[0]
    assert active.source.pending is not None
    assert active.source.pending["number"] == 1
    assert "products" not in repo_item.payload
    assert not [item for item in coordinator.items if item.kind is ItemKind.ISSUE]


def test_repo_source_drain_stops_after_admission_saturation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Later cursors retain metadata when an earlier cursor saturates admission."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a", "repo-b"],
            loops=1,
            parallel_repos=1,
            max_workers=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    first = _ActiveRepoIssueSource("repo-a", RepoIssueSource(iter(()), pending={"number": 1}))
    second_metadata = {"number": 2, "labels": ["state:needs-plan"], "title": "pending"}
    second = _ActiveRepoIssueSource("repo-b", RepoIssueSource(iter(()), pending=second_metadata))
    coordinator._repo_issue_sources.extend((first, second))
    drained: list[str] = []

    def drain(active: _ActiveRepoIssueSource) -> bool:
        drained.append(active.repo)
        coordinator._admission_saturated = True
        coordinator._begin_graceful_shutdown("test admission saturation")
        return True

    monkeypatch.setattr(coordinator, "_drain_repo_issue_source", drain)

    coordinator._drain_repo_issue_sources()

    assert drained == ["repo-a"]
    assert list(coordinator._repo_issue_sources) == [second, first]
    assert second.source.pending is second_metadata


def test_repo_source_is_lossless_and_ordered_at_capacity_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C+1 discovered issues classify only as capacity becomes available."""
    events: list[tuple[str, int]] = []
    metadata = [
        {"number": 1, "labels": ["state:needs-plan"], "title": "first"},
        {"number": 2, "labels": ["state:needs-plan"], "title": "second"},
        {"number": 1, "labels": ["state:needs-plan"], "title": "duplicate"},
    ]

    monkeypatch.setattr(
        loop_repo_manager, "_iter_open_issue_meta", lambda _org, _repo: iter(metadata)
    )

    def classify(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            parallel_repos=1,
            max_workers=1,
            stage_queue_capacity=1,
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
        ("classify", 1),
        ("complete", 1),
        ("classify", 2),
        ("complete", 2),
    ]
    assert Counter(issue for event, issue in events if event == "classify") == Counter({1: 1, 2: 1})
    assert Counter(issue for event, issue in events if event == "complete") == Counter({1: 1, 2: 1})
    issues = [item for item in coordinator._effective_items() if item.kind is ItemKind.ISSUE]
    assert [item.issue for item in issues] == [1, 2]
    assert all(item.result is not None and item.result.passed for item in issues)


def test_active_cursor_reserves_progress_before_next_repo_setup_at_capacity_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A C=1 cursor cannot be deadlocked by the next repo setup permit."""
    events: list[tuple[str, int]] = []
    metadata = {
        "repo-a": iter(
            [
                {"number": 1, "labels": ["epic"], "title": "Epic"},
                {"number": 2, "labels": ["state:needs-plan"], "title": "actionable"},
            ]
        ),
        "repo-b": iter(()),
    }
    monkeypatch.setattr(
        loop_repo_manager,
        "_iter_open_issue_meta",
        lambda _org, repo: metadata[repo],
    )
    monkeypatch.setattr(
        seeding_mod,
        "seed_issue_from_github",
        lambda issue, github: _facts(issue),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a", "repo-b"],
            loops=1,
            parallel_repos=1,
            max_workers=1,
            stage_queue_capacity=1,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage(events)

    assert coordinator._seed_pass() == 1
    coordinator._drain_queues()
    assert [active.repo for active in coordinator._repo_issue_sources] == ["repo-a"]
    assert coordinator.repo_source_owner_count == 1
    assert coordinator.live_work_count == 0

    # The first row is an excluded epic, so A has not consumed a work permit.
    # B must nevertheless remain in the lazy seed iterator: admitting it here
    # would recreate B-permit -> A-cursor -> B-cursor at C=1.
    coordinator._drain_repo_issue_sources()
    assert coordinator.live_work_count == 0
    assert coordinator._drain_seed_entries() == 0
    assert coordinator.repo_source_owner_count == 1
    assert not coordinator.queues[StageName.REPO].snapshot()

    # A can now classify its next row and acquire the still-free permit.
    coordinator._drain_repo_issue_sources()
    planning = coordinator.queues[StageName.PLANNING].snapshot()
    assert [item.issue for item in planning] == [2]
    assert coordinator.live_work_count == 1

    # Once A's work completes and its source exhausts, the same source slot
    # transfers to B; reserving progress does not starve later repositories.
    coordinator._drain_queues()
    coordinator._drain_queues()
    assert events == [("complete", 2)]
    assert coordinator.live_work_count == 0
    coordinator._drain_repo_issue_sources()
    assert not coordinator._repo_issue_sources
    assert coordinator._drain_seed_entries() == 1
    repo_items = coordinator.queues[StageName.REPO].snapshot()
    assert [item.repo for item in repo_items] == ["repo-b"]
    assert coordinator.repo_source_owner_count == 1


def test_repo_source_deduplicates_metadata_after_completed_work_at_capacity_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated source row cannot re-run after its first item finishes."""
    events: list[tuple[str, int]] = []
    metadata = [
        {"number": 1, "labels": ["state:needs-plan"], "title": "first"},
        {"number": 2, "labels": ["state:needs-plan"], "title": "second"},
        {"number": 1, "labels": ["state:needs-plan"], "title": "duplicate"},
    ]
    monkeypatch.setattr(
        loop_repo_manager, "_iter_open_issue_meta", lambda _org, _repo: iter(metadata)
    )

    def classify(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            parallel_repos=1,
            max_workers=1,
            stage_queue_capacity=1,
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
        ("classify", 1),
        ("complete", 1),
        ("classify", 2),
        ("complete", 2),
    ]
    assert Counter(issue for event, issue in events if event == "classify") == Counter({1: 1, 2: 1})
    assert Counter(issue for event, issue in events if event == "complete") == Counter({1: 1, 2: 1})
    issues = [item for item in coordinator._effective_items() if item.kind is ItemKind.ISSUE]
    assert [item.issue for item in issues] == [1, 2]
    assert all(item.result is not None and item.result.passed for item in issues)


def test_drive_green_pr_source_is_filtered_lossless_and_capacity_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eligible linked PRs stream after issues and drain through a C=1 window."""
    produced: list[int] = []
    reviewed: list[int] = []
    pulls: list[dict[str, Any]] = [
        {
            "number": 31,
            "state": "OPEN",
            "isDraft": False,
            "user": {"login": "alice", "type": "User"},
        },
        {
            "number": 32,
            "state": "OPEN",
            "isDraft": False,
            "user": {"login": "alice", "type": "User"},
        },
        {
            "number": 33,
            "state": "OPEN",
            "isDraft": False,
            "user": {"login": "bob", "type": "User"},
        },
        {
            "number": 34,
            "state": "OPEN",
            "isDraft": False,
            "user": {"login": "alice", "type": "Bot"},
        },
        {
            "number": 35,
            "state": "OPEN",
            "isDraft": True,
            "user": {"login": "alice", "type": "User"},
        },
    ]

    def pull_source(_org: str, _repo: str):
        for pull in pulls:
            produced.append(int(pull["number"]))
            yield pull

    monkeypatch.setattr(loop_repo_manager, "_iter_open_issue_meta", lambda _org, _repo: iter(()))
    monkeypatch.setattr(loop_repo_manager, "_iter_open_pr_meta", pull_source)
    monkeypatch.setattr(pr_discovery, "_resolve_viewer_login", lambda: "alice")
    monkeypatch.setattr(
        seeding_mod,
        "seed_issue_from_github",
        lambda issue, github: _facts(issue),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            parallel_repos=1,
            max_workers=1,
            stage_queue_capacity=1,
            drive_green_all=True,
            include_all_authors=False,
            include_bot_prs=False,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(pr_issue=901),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    coordinator.stages[StageName.PR_REVIEW] = _ImmediatePrPassStage(reviewed)

    assert coordinator.run() == 0

    assert produced == [31, 32, 33, 34, 35]
    assert reviewed == [31, 32]
    prs = [item for item in coordinator.item_summaries if item.kind is ItemKind.PR]
    assert [item.pr for item in prs] == [31, 32]
    assert all(item.issue == 901 for item in prs)
    assert all(item.result is not None and item.result.passed for item in prs)
    assert not coordinator._repo_issue_sources
