"""Regression coverage for explicit CLI scope checkout synchronization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import (
    Disposition,
    PipelineScope,
    StageName,
    StageOutcome,
)
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.work_item import WorkItem
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _ImmediatePassStage:
    """Finish a seeded issue without an agent job."""

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx

    def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
        del item, ctx
        return StageOutcome(Disposition.FINISH_PASS, "complete")

    def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
        del item, result, ctx
        raise AssertionError("the deterministic stage does not submit jobs")


class _RecordingPool(FakeWorkerPool):
    """Record the synchronization submission in the shared event trace."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def submit(self, job: Any, on_done_state: Any, **kwargs: Any) -> Any:
        if isinstance(job, GitJob) and job.op == "clone":
            self._events.append("clone")
        if isinstance(job, GitJob) and job.op == "sync_checkout":
            self._events.append("sync")
        return super().submit(job, on_done_state, **kwargs)


class _RecordingGitHub(FakeStageGitHub):
    """Record the first label mutation relative to direct classification."""

    def __init__(self, events: list[str], **kwargs: Any) -> None:
        super().__init__(labels=["state:needs-plan"], **kwargs)
        self._events = events

    def ensure_state_labels(self) -> None:
        self._events.append("labels")
        super().ensure_state_labels()

    def find_issue_for_pr(self, pr_number: int) -> int | None:
        self._events.append("classify-pr")
        return super().find_issue_for_pr(pr_number)


def _facts(issue: int) -> IssueFacts:
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


def test_explicit_scope_syncs_before_labels_and_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scoped run gates direct source admission on a clean-main sync job."""
    events: list[str] = []
    checkout = tmp_path / "repo-a"
    checkout.mkdir()
    pool = _RecordingPool(events)
    github = _RecordingGitHub(events)

    def classify(issue: int, github_arg: Any) -> IssueFacts:
        del github_arg
        events.append("classify")
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda *_args: [])
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})),
        ),
        github=github,
        pool=pool,
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 0
    assert events[:3] == ["sync", "labels", "classify"]


def test_missing_direct_scope_checkout_clones_then_syncs_before_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing direct-scope checkout is synchronized before it is admitted."""
    events: list[str] = []
    pool = _RecordingPool(events)
    github = _RecordingGitHub(events)

    def classify(issue: int, github_arg: Any) -> IssueFacts:
        del github_arg
        events.append("classify")
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda *_args: [])
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})),
        ),
        github=github,
        pool=pool,
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 0
    assert events[:4] == ["clone", "sync", "labels", "classify"]
    issue_item = next(item for item in coordinator.items if item.issue == 101)
    assert issue_item.payload["_direct_scope_base_sha"] == "a" * 40


def test_direct_issue_carries_the_bootstrap_checkout_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An issue cursor preserves the exact default-branch SHA it was admitted under."""
    checkout = tmp_path / "repo-a"
    checkout.mkdir()
    pin = "a" * 40
    pool = FakeWorkerPool()
    pool.script(JobResult(ok=True, value=pin))
    github = FakeStageGitHub(labels=["state:needs-plan"])

    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda *_args: [])
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING})),
        ),
        github=github,
        pool=pool,
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 0
    issue_item = next(item for item in coordinator.items if item.issue == 101)
    assert issue_item.payload["_direct_scope_base_sha"] == pin


def test_explicit_pr_scope_syncs_before_labels_and_pr_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit PR follows the same checkout proof before its first read."""
    events: list[str] = []
    (tmp_path / "repo-a").mkdir()
    pool = _RecordingPool(events)
    github = _RecordingGitHub(events, pr_issue=101, pr_impl_state=(True, False))

    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda *_args: [])
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            prs=[77],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.MERGE_WAIT})),
        ),
        github=github,
        pool=pool,
        install_signals=False,
    )
    coordinator.stages[StageName.MERGE_WAIT] = _ImmediatePassStage()

    assert coordinator.run() == 0
    assert events[:3] == ["sync", "labels", "classify-pr"]


@pytest.mark.parametrize(
    "sync_error",
    ["fetch unavailable", "checkout is dirty", "cannot fast-forward checkout"],
)
def test_explicit_scope_sync_failure_blocks_labels_sources_and_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_error: str
) -> None:
    """Fetch, dirty, or stale checkout failures cannot enter a scoped run."""
    checkout = tmp_path / "repo-a"
    checkout.mkdir()
    classifications: list[int] = []
    pool = FakeWorkerPool()
    pool.script(
        JobResult(ok=False, error=sync_error),
        JobResult(ok=False, error=sync_error),
    )
    github = FakeStageGitHub(labels=["state:needs-plan"])

    def classify(issue: int, github_arg: Any) -> IssueFacts:
        del github_arg
        classifications.append(issue)
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda *_args: [])
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo-a"], issues=[101], projects_dir=tmp_path),
        github=github,
        pool=pool,
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 1
    assert classifications == []
    assert github.mutation_log == []
    assert [handle.job.op for handle in pool.submitted if isinstance(handle.job, GitJob)] == [
        "sync_checkout",
        "sync_checkout",
    ]
    assert not any(isinstance(handle.job, AgentJob) for handle in pool.submitted)
    assert len(coordinator.ledger) == 1
    assert "clone exhausted" in coordinator.ledger[0].reason
