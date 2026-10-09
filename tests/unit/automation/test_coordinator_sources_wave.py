"""Regression coverage for lease-backed direct issue source reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import loop_repo_manager
from hephaestus.automation.issue_waves import (
    WAVE_NON_CODE_INTENT_PAYLOAD,
    IssueWaveStore,
)
from hephaestus.automation.pipeline import (
    coordinator_types as coordinator_types,
    seeding as seeding_mod,
)
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.stages.repo import RepoIssueSource
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.requirements_recovery import evidence_digest
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

BASE_SHA = "a" * 40


class _ImmediatePassStage:
    """Complete admitted planning work without an agent job."""

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
        raise AssertionError("the deterministic stage must not submit a job")


def _planning_facts(issue: int) -> IssueFacts:
    """Return one issue that is ready for planning."""
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


def test_repo_source_isolates_issue_classification_failure_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unsafe issue fails once while the next issue enters its normal stage."""
    events: list[tuple[str, int]] = []
    metadata = [
        {"number": 471, "labels": ["state:needs-plan"], "title": "ambiguous plan"},
        {"number": 472, "labels": ["state:needs-plan"], "title": "valid plan"},
    ]

    def classify(issue: int, github: Any) -> IssueFacts:
        del github
        events.append(("classify", issue))
        if issue == 471:
            raise ValueError("ambiguous actor-owned comment aliases")
        return _planning_facts(issue)

    monkeypatch.setattr(
        loop_repo_manager, "_iter_open_issue_meta", lambda _org, _repo: iter(metadata)
    )
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    github = FakeStageGitHub(labels=["state:needs-plan"])
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            max_workers=1,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=github,
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage(events)

    assert coordinator.run() == 1

    assert events == [("classify", 471), ("classify", 472), ("complete", 472)]
    issues = {item.issue: item for item in coordinator.items if item.kind is ItemKind.ISSUE}
    failed = issues[471]
    assert failed.result is not None
    assert failed.result.passed is False
    assert failed.result.final_stage is StageName.REPO
    assert failed.result.reason == (
        "classification failed (ValueError): ambiguous actor-owned comment aliases; "
        "manual recovery required"
    )
    assert issues[472].result is not None and issues[472].result.passed is True
    assert coordinator._terminal_summary.dispositions == {"fail": 1, "pass": 1}
    assert github.mutation_log == [("ensure_state_labels", ())]


def test_repo_source_iterator_failure_still_terminates_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page-fetch failure remains one terminal repository-source failure."""

    def broken_source(_org: str, _repo: str) -> Any:
        yield {"number": 471, "labels": ["state:needs-plan"], "title": "first"}
        raise RuntimeError("page fetch failed")

    monkeypatch.setattr(loop_repo_manager, "_iter_open_issue_meta", broken_source)
    monkeypatch.setattr(
        seeding_mod,
        "seed_issue_from_github",
        lambda issue, _github: _planning_facts(issue),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            max_workers=1,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage([])

    assert coordinator.run() == 1

    failures = [
        item for item in coordinator.items if item.kind is ItemKind.REPO and item.result is not None
    ]
    assert len(failures) == 1
    assert failures[0].result is not None
    assert failures[0].result.reason == "discovery failed: page fetch failed"


def test_explicit_issue_classification_failure_stays_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit issue conflict still stops its direct source classification."""

    def reject(_issue: int, _github: Any) -> IssueFacts:
        raise ValueError("ambiguous actor-owned comment aliases")

    github = FakeStageGitHub(labels=["state:needs-plan"])
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[471],
            loops=1,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=github,
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", reject)
    coordinator._begin_direct_issue_source("repo-a", BASE_SHA)

    with pytest.raises(ValueError, match="ambiguous actor-owned comment aliases"):
        coordinator._drain_direct_issue_source()

    assert [item for item in coordinator.items if item.kind is ItemKind.ISSUE] == []
    assert github.mutation_log == []


def test_repo_source_does_not_misclassify_queue_failure_as_issue_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coordinator admission fault remains outside the row classification boundary."""
    monkeypatch.setattr(
        seeding_mod,
        "seed_issue_from_github",
        lambda issue, _github: _planning_facts(issue),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            max_workers=1,
            dry_run=True,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    source = coordinator_types._ActiveRepoIssueSource(
        repo="repo-a",
        source=RepoIssueSource(
            metadata=iter([{"number": 471, "labels": ["state:needs-plan"], "title": "valid plan"}])
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "_push_item",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("queue invariant")),
    )

    with pytest.raises(RuntimeError, match="queue invariant"):
        coordinator._drain_repo_issue_source(source)

    failures = [
        item
        for item in coordinator.items
        if item.kind is ItemKind.ISSUE and item.result is not None
    ]
    assert failures == []


@pytest.mark.parametrize("label", ["state:skip", "state:plan-blocked"])
def test_lease_backed_direct_exclusion_reaches_finished_and_records_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    """Recovery waves checkpoint terminal overrides instead of dropping them."""
    repo_root = tmp_path / "repo-a"
    repo_root.mkdir()
    store = IssueWaveStore(repo_root, "org", "repo-a")
    lease = store.seal_selection(store.plan_admission(BASE_SHA, 1), [2453])
    github = FakeStageGitHub(labels=[label])
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        issues=[2453],
        loops=1,
        projects_dir=tmp_path,
        repo_roots={"repo-a": repo_root},
    )
    coordinator = Coordinator(
        config,
        github=github,
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator._direct_wave_lease = lease
    coordinator._begin_direct_issue_source("repo-a", BASE_SHA)

    assert coordinator._drain_direct_issue_source() == 1
    item = coordinator.queues[StageName.FINISHED].snapshot()[0]
    assert item.result is not None
    assert item.result.passed is False
    assert "became skipped or blocked" in item.result.reason
    assert github.mutation_log == []

    coordinator._drain_queues()

    checkpoint = store.load()
    assert checkpoint is not None
    outcome = checkpoint.current_wave.outcomes[0]
    assert outcome.issue_number == 2453
    assert outcome.passed is False
    assert outcome.reason == item.result.reason


def test_lease_backed_non_code_intent_reenters_planning_for_label_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash before state:skip is repaired from the durable reviewed intent."""
    repo_root = tmp_path / "repo-a"
    repo_root.mkdir()
    store = IssueWaveStore(repo_root, "org", "repo-a")
    lease = store.seal_selection(store.plan_admission(BASE_SHA, 1), [2453])
    reason = "independently confirmed tracker"
    store.record_non_code_intent(
        lease,
        issue_number=2453,
        reason=reason,
        evidence_digest=evidence_digest("repo-a", 2453, BASE_SHA, "A task", ""),
        repository_revision=BASE_SHA,
        extra_labels=("epic",),
    )
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        issues=[2453],
        loops=1,
        projects_dir=tmp_path,
        repo_roots={"repo-a": repo_root},
    )
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator._direct_wave_lease = lease
    coordinator._begin_direct_issue_source("repo-a", BASE_SHA)

    assert coordinator._drain_direct_issue_source() == 1
    item = coordinator.queues[StageName.PLANNING].snapshot()[0]
    assert item.payload[WAVE_NON_CODE_INTENT_PAYLOAD] == {
        "reason": reason,
        "extra_labels": ["epic"],
        "evidence_digest": evidence_digest("repo-a", 2453, BASE_SHA, "A task", ""),
        "repository_revision": BASE_SHA,
        "explanation": "",
        "retired": False,
    }


def test_lease_backed_retired_intent_projects_cleanup_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process restart carries revoked intent evidence into planning cleanup."""
    repo_root = tmp_path / "repo-a"
    repo_root.mkdir()
    store = IssueWaveStore(repo_root, "org", "repo-a")
    lease = store.seal_selection(store.plan_admission(BASE_SHA, 1), [2454])
    store.record_non_code_intent(
        lease,
        issue_number=2454,
        reason="independently confirmed tracker",
        evidence_digest=evidence_digest("repo-a", 2454, BASE_SHA, "A task", "Old body"),
        repository_revision=BASE_SHA,
        extra_labels=("epic",),
    )
    active = store.non_code_intent_for(lease, 2454)
    assert active is not None
    store.retire_non_code_intent(lease, active)
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        issues=[2454],
        loops=1,
        projects_dir=tmp_path,
        repo_roots={"repo-a": repo_root},
    )
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(
            labels=["state:skip", "epic"],
            issue_body="Implement the worker.",
        ),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator._direct_wave_lease = lease
    coordinator._begin_direct_issue_source("repo-a", BASE_SHA)

    assert coordinator._drain_direct_issue_source() == 1
    item = coordinator.queues[StageName.PLANNING].snapshot()[0]
    assert item.payload[WAVE_NON_CODE_INTENT_PAYLOAD]["retired"] is True
