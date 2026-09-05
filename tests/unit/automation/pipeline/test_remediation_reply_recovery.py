"""Regression tests for failed remediation reply recovery."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageContext
from hephaestus.automation.pipeline.stages.implementation import (
    REMEDIATION_FAILURE_DIAGNOSTIC_MAX,
    ImplementationStage,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def test_file_change_failure_records_bounded_redacted_recovery_evidence() -> None:
    """A failed tool event preserves only bounded recovery evidence."""
    item = WorkItem(
        repo="Hephaestus",
        kind=ItemKind.ISSUE,
        issue=2973,
        stage=StageName.IMPLEMENTATION,
        state="IMPLEMENT_WAIT",
    )
    item.payload["implementation_remediation"] = True
    error = "codex_tool_or_provider_failure: file_change status=failed " + "x" * 600

    ImplementationStage._on_implement_done(item, JobResult(ok=False, error=error))

    assert item.attempts["implement"] == 1
    assert item.attempts["remediation_reply"] == 0
    assert item.payload["implement_error"] is True
    assert item.payload["remediation_reply_inspection_required"] is True
    assert (
        item.payload["remediation_failure_diagnostic"] == error[:REMEDIATION_FAILURE_DIAGNOSTIC_MAX]
    )


def test_dirty_failed_writer_cannot_publish_before_reply_mapping(tmp_path: Path) -> None:
    """A failed dirty writer has one read-only mapping gate before commit."""
    repo = tmp_path / "repo"
    writer = repo / "build" / "writer"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    (repo / "module.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "module.py")
    git("commit", "-q", "--no-gpg-sign", "-m", "test: base")
    git("worktree", "add", "-q", "-b", "2973-auto-impl", str(writer))
    (writer / "module.py").write_text("value = 2\n", encoding="utf-8")
    head = git("rev-parse", "HEAD", cwd=writer).stdout.strip()

    stage = ImplementationStage()
    item = WorkItem(
        repo="test-repo",
        kind=ItemKind.ISSUE,
        issue=2973,
        pr=1001,
        stage=StageName.IMPLEMENTATION,
        state="IMPLEMENT_WAIT",
    )
    item.branch = "2973-auto-impl"
    item.worktree = str(writer)
    item.payload.update(
        {
            "implementation_remediation": True,
            "existing_pr": True,
            "_impl_source_revision": head,
            "remediation_thread_snapshots": [{"id": "thread-1"}],
        }
    )
    ctx = StageContext(
        config=PipelineConfig(org="test-org", repos=["test-repo"]),
        org="test-org",
        dry_run=False,
        github=FakeStageGitHub(),
        paths=SimpleNamespace(repo_root=repo),
        budget_fn=lambda _name: 2,
    )

    stage.on_job_done(
        item,
        JobResult(ok=False, error="codex_tool_or_provider_failure: file_change failed"),
        ctx,
    )
    item.state = "TEST_WAIT"
    route = stage.step(item, ctx)
    assert route == Continue(next_state="WORKTREE_WAIT")
    item.state = route.next_state
    inspection_request = stage.step(item, ctx)
    assert isinstance(inspection_request, JobRequest)
    assert isinstance(inspection_request.job, GitJob)
    assert inspection_request.job.op == "inspect_implementation_worktree"

    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=CompletionQueue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        inspection = pool._git_inspect_implementation_worktree(inspection_request.job)
    finally:
        pool.shutdown()
    assert inspection.ok is True
    assert isinstance(inspection.value, dict)
    assert inspection.value["outcome"] == "dirty"
    item.state = inspection_request.on_done_state
    stage.on_job_done(item, inspection, ctx)
    mapped_route = stage.step(item, ctx)
    assert mapped_route == Continue(next_state="REMEDIATION_REPLY_RECOVERY_WAIT")
    item.state = mapped_route.next_state
    mapping_request = stage.step(item, ctx)
    assert isinstance(mapping_request, JobRequest)
    assert isinstance(mapping_request.job, AgentJob)
    assert mapping_request.job.allowed_tools == "Read,Glob,Grep"
    assert inspection_request.job.op not in {"recover_dirty_worktree", "commit_push", "push"}

    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value={"addressed": ["thread-1"], "replies": {"thread-1": "Mapped."}},
        ),
        ctx,
    )
    item.state = mapping_request.on_done_state
    test_route = stage.step(item, ctx)
    assert test_route == Continue(next_state="TEST_WAIT")
    item.state = test_route.next_state
    commit_route = stage.step(item, ctx)
    assert commit_route == Continue(next_state="COMMIT_PUSH_WAIT")
    item.state = commit_route.next_state
    commit_request = stage.step(item, ctx)
    assert isinstance(commit_request, JobRequest)
    assert isinstance(commit_request.job, GitJob)
    assert commit_request.job.op == "commit_push"
    assert commit_request.job.kwargs["expected_recovery_head"] == head
    assert (
        commit_request.job.kwargs["expected_recovery_content_snapshot"]
        == (inspection.value["content_snapshot"])
    )
