"""Tests for bounded plan reads and per-item admission failures."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import admission
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    IssueComment,
    PlanDiscoveryResult,
    discover_plan_from_comments,
    render_current_plan,
)
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.conftest import FakeWorkerPool, fake_worker_factories
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def test_plan_admission_reads_share_one_deadline_and_cancel_signal(tmp_path: Path) -> None:
    """Journal pages and actor lookup use one bounded transport attempt each."""
    calls: list[tuple[list[str], dict[str, Any]]] = []
    shutdown = threading.Event()
    deadline_s = time.monotonic() + 10.0

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        body = (
            "bot"
            if argv == ["api", "user", "--jq", ".login"]
            else json.dumps(
                [
                    {
                        "id": 8,
                        "body": render_current_plan("## Files to Modify\n- `src/worker.py`"),
                        "user": {"login": "bot"},
                    }
                ]
            )
        )
        return subprocess.CompletedProcess(argv, 0, stdout=body)

    github = PipelineGitHub("org", repo="repo", repo_root=tmp_path, command_runner=run)

    files = admission._fetch_planned_files(
        7, github=github, deadline_s=deadline_s, shutdown=shutdown
    )

    assert files == {"src/worker.py"}
    assert len(calls) == 2
    assert "/repos/org/repo/issues/7/comments" in calls[0][0][1]
    for _argv, options in calls:
        assert options["deadline_s"] == deadline_s
        assert options["shutdown"] is shutdown
        assert options["max_retries"] == 1
        assert options["retry_on_rate_limit"] is False
        assert 0 < options["timeout"] <= 10


def test_plan_admission_read_error_is_not_an_absent_plan(tmp_path: Path) -> None:
    """A failed comment read cannot freeze an empty file reservation."""
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 1)

    github = PipelineGitHub("org", repo="repo", repo_root=tmp_path, command_runner=run)

    with pytest.raises(CommentJournalReadError):
        admission._fetch_planned_files(
            7, github=github, deadline_s=time.monotonic() + 10, shutdown=threading.Event()
        )

    assert len(calls) == 1


def _coordinator(tmp_path: Path, github: FakeStageGitHub) -> Coordinator:
    """Create a two-worker queue with separate completion channels."""
    return Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            issues=[1, 2],
            max_workers=2,
            projects_dir=tmp_path,
            rate_guard_enabled=False,
        ),
        github=github,
        **fake_worker_factories(FakeWorkerPool(), FakeWorkerPool()),
        install_signals=False,
    )


def test_direct_plan_marker_conflict_does_not_abort_later_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first foreign canonical marker fails only its direct source row."""
    foreign_plan = render_current_plan("## Files to Modify\n- `src/worker.py`")

    class ForeignPlanGitHub(FakeStageGitHub):
        def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
            if issue_number == 1:
                return discover_plan_from_comments(
                    [IssueComment(body=foreign_plan, author_login="other", viewer_did_author=False)]
                )
            return PlanDiscoveryResult.absent()

    monkeypatch.setattr(
        admission, "_filter_open_issues", lambda _repo, issues, **_kwargs: list(issues)
    )
    coordinator = _coordinator(tmp_path, ForeignPlanGitHub(labels=[STATE_PLAN_GO]))
    coordinator._begin_direct_issue_source("repo", "a" * 40)

    assert coordinator._drain_direct_issue_source() == 1

    failed = [item for item in coordinator.items if item.issue == 1]
    assert len(failed) == 1
    assert failed[0].result is not None and not failed[0].result.passed
    assert "CommentAliasConflictError" in failed[0].result.reason
    assert [item.issue for item in coordinator.queues[StageName.IMPLEMENTATION].snapshot()] == [2]
    assert coordinator._direct_issue_source is None


@pytest.mark.parametrize("direct_source", [False, True])
def test_plan_read_failure_uses_existing_timer_without_freezing_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direct_source: bool
) -> None:
    """A transient plan failure retains one owner and retries after its timer."""
    clock = [100.0]
    reads: list[int] = []

    def read_plan(issue: int, **_kwargs: Any) -> set[str]:
        reads.append(issue)
        if len(reads) == 1:
            raise CommentJournalReadError("temporary transport failure")
        return {"src/worker.py"}

    monkeypatch.setattr(admission, "_fetch_planned_files", read_plan)
    monkeypatch.setattr(
        admission, "_filter_open_issues", lambda _repo, issues, **_kwargs: list(issues)
    )
    coordinator = _coordinator(tmp_path, FakeStageGitHub(labels=[STATE_PLAN_GO]))
    coordinator._monotonic = lambda: clock[0]
    if direct_source:
        coordinator.config.issues[:] = [1]
        coordinator._begin_direct_issue_source("repo", "a" * 40)
        assert coordinator._drain_direct_issue_source() == 1
        item = next(item for item in coordinator.items if item.issue == 1)
    else:
        item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.IMPLEMENTATION)
        assert coordinator._push_item(item, item.stage, enter=True)
        assert coordinator._select_implementation_dispatch([item]) == []

    assert "_implementation_file_claims" not in item.payload
    assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == []
    assert len(coordinator.timers) == 1 and coordinator.timers[0][2] is item
    assert coordinator.live_work_count == 1
    coordinator._wake_timers()
    assert reads == [1]
    clock[0] = coordinator.timers[0][0]
    coordinator._wake_timers()

    assert coordinator._select_implementation_dispatch([item]) == [item]
    assert item.payload["_implementation_file_claims"] == frozenset(
        {(("org", "repo"), "src/worker.py")}
    )
    assert reads == [1, 1]


def test_plan_admission_read_failures_stop_at_the_existing_implementation_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated transport failures release the item after its bounded retry budget."""
    reads: list[int] = []

    def read_plan(issue: int, **_kwargs: Any) -> set[str]:
        reads.append(issue)
        raise CommentJournalReadError("temporary transport failure")

    monkeypatch.setattr(admission, "_fetch_planned_files", read_plan)
    coordinator = _coordinator(tmp_path, FakeStageGitHub(labels=[STATE_PLAN_GO]))
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.IMPLEMENTATION)
    assert coordinator._push_item(item, item.stage, enter=True)
    budget = coordinator._ctx_for(item).budget("implement")

    for attempt in range(budget):
        assert coordinator._select_implementation_dispatch([item]) == []
        if attempt + 1 < budget:
            assert len(coordinator.timers) == 1
            wake = coordinator.timers[0][0]

            def current_time(value: float = wake) -> float:
                return value

            coordinator._monotonic = current_time
            coordinator._wake_timers()

    assert len(reads) == budget
    assert coordinator.timers == []
    assert item.result is not None and not item.result.passed
    assert "plan admission" in item.result.reason and "exhausted" in item.result.reason
    assert coordinator.live_work_count == 0
    assert "_implementation_file_claims" not in item.payload
