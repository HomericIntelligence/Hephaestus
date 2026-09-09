"""Regression tests for failed remediation reply recovery."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.reply_handoff import PENDING_IMPLEMENTATION_REPLY_HANDOFF
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import (
    Continue,
    JobRequest,
    StageContext,
    StageOutcome,
)
from hephaestus.automation.pipeline.stages.implementation import (
    REMEDIATION_FAILURE_DIAGNOSTIC_MAX,
    ImplementationStage,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline.worker_pool import (
    WorkerPool,
    _candidate_commit_tree_evidence,
    _dirty_worktree_content_snapshot,
)
from hephaestus.automation.source_worktree import SourceWorkspaceManager
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
    """A failed dirty writer prepares its commit before the reply gate."""
    repo = tmp_path / "repo"
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
    head = git("rev-parse", "HEAD").stdout.strip()
    manager = SourceWorkspaceManager(repo, repository="test-repo")
    binding = manager.prepare_bounded(
        2973, SourceLane.IMPLEMENTATION, head, branch="2973-auto-impl"
    )
    writer = binding.cwd
    (writer / "module.py").write_text("value = 2\n", encoding="utf-8")

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
            "issue_title": "Repair publication",
            "issue_body": "Keep workers local.",
            "_impl_source_revision": head,
            "_impl_source_workspace": binding.to_dict(),
            "remediation_thread_snapshots": [
                {
                    "id": "thread-1",
                    "isResolved": False,
                    "path": "module.py",
                    "line": 1,
                    "side": "RIGHT",
                    "comments": [{"id": "comment-1", "author": "reviewer", "body": "Fix it."}],
                }
            ],
        }
    )
    ctx = StageContext(
        config=PipelineConfig(org="test-org", repos=["test-repo"], rate_guard_enabled=False),
        org="test-org",
        dry_run=False,
        github=FakeStageGitHub(),
        paths=SimpleNamespace(repo_root=repo, source_workspaces=manager),
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
        inspection = pool._run_git(inspection_request.job)
    finally:
        pool.shutdown()
    assert inspection.ok is True
    assert isinstance(inspection.value, dict)
    assert inspection.value["outcome"] == "dirty"
    assert inspection.value["source_workspace"] == binding.to_dict()
    # The coordinator delivers completion while the item is still in its
    # submitting state. It assigns ``on_done_state`` after this callback.
    stage.on_job_done(item, inspection, ctx)
    item.state = inspection_request.on_done_state
    mapped_route = stage.step(item, ctx)
    assert mapped_route == Continue(next_state="TEST_WAIT")
    item.state = mapped_route.next_state
    commit_route = stage.step(item, ctx)
    assert commit_route == Continue(next_state="COMMIT_PUSH_WAIT")
    item.state = commit_route.next_state
    prepare_route = stage.step(item, ctx)
    assert prepare_route == Continue(next_state="REMEDIATION_PREPARE_WAIT")
    item.state = prepare_route.next_state
    prepare_request = stage.step(item, ctx)
    assert isinstance(prepare_request, JobRequest)
    assert isinstance(prepare_request.job, GitJob)
    assert prepare_request.job.op == "prepare_remediation_recovery"
    assert inspection_request.job.op not in {"recover_dirty_worktree", "commit_push", "push"}
    assert prepare_request.job.kwargs["expected_recovery_head"] == head
    assert (
        prepare_request.job.kwargs["expected_recovery_content_snapshot"]
        == inspection.value["content_snapshot"]
    )
    assert (
        prepare_request.job.kwargs["expected_recovery_tree_sha"]
        == inspection.value["candidate_tree_sha"]
    )
    assert prepare_request.job.kwargs["remediation_repository"] == "test-org/test-repo"
    assert prepare_request.job.kwargs["remediation_pr_number"] == 1001
    assert (
        prepare_request.job.kwargs["remediation_thread_snapshots"]
        == item.payload["remediation_thread_snapshots"]
    )
    assert "remediation_replies" not in prepare_request.job.kwargs
    assert len(prepare_request.job.kwargs["remediation_batch_nonce"]) == 32


def test_recovery_commit_error_preserves_dirty_writer_without_handoff(tmp_path: Path) -> None:
    """An ambiguous failed recovery commit cannot report an unchanged head."""
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    writer = repo / "build" / "writer"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
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
    git("remote", "add", "origin", str(remote))
    git("worktree", "add", "-q", "-b", "2973-auto-impl", str(writer))
    git("push", "-q", "-u", "origin", "2973-auto-impl", cwd=writer)
    head = git("rev-parse", "HEAD", cwd=writer).stdout.strip()
    (writer / "module.py").write_text("value = 2\n", encoding="utf-8")
    snapshot = _dirty_worktree_content_snapshot(writer, timeout=60)
    candidate_tree, candidate_diff = _candidate_commit_tree_evidence(writer, head, timeout=60)
    assert git("rev-list", "--count", "@{upstream}..HEAD", cwd=writer).stdout.strip() == "0"

    item = WorkItem(
        repo="test-repo",
        kind=ItemKind.ISSUE,
        issue=2973,
        pr=1001,
        stage=StageName.IMPLEMENTATION,
        state="COMMIT_PUSH_WAIT",
    )
    item.branch = "2973-auto-impl"
    item.worktree = str(writer)
    item.payload.update(
        {
            "implementation_remediation": True,
            "existing_pr": True,
            "issue_title": "Repair publication",
            "issue_body": "Keep workers local.",
            "_impl_source_revision": head,
            "remediation_writer_inspection": {
                "head_sha": head,
                "content_snapshot": snapshot,
                "candidate_tree_sha": candidate_tree,
                "diff": candidate_diff.text,
                "diff_sha256": candidate_diff.sha256,
                "candidate_add_paths": ["module.py"],
                "candidate_update_paths": [],
            },
            "remediation_thread_snapshots": [
                {
                    "id": "thread-1",
                    "isResolved": False,
                    "path": "module.py",
                    "line": 1,
                    "side": "RIGHT",
                    "comments": [{"id": "comment-1", "author": "reviewer", "body": "Fix it."}],
                }
            ],
            "remediation_output": {
                "addressed": ["thread-1"],
                "replies": {"thread-1": "Mapped."},
            },
        }
    )
    ctx = StageContext(
        config=PipelineConfig(org="test-org", repos=["test-repo"], rate_guard_enabled=False),
        org="test-org",
        dry_run=False,
        github=FakeStageGitHub(),
        paths=SimpleNamespace(repo_root=repo),
        budget_fn=lambda _name: 2,
    )
    stage = ImplementationStage()
    prepare_route = stage.step(item, ctx)
    assert prepare_route == Continue(next_state="REMEDIATION_PREPARE_WAIT")
    item.state = prepare_route.next_state
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    assert request.job.op == "prepare_remediation_recovery"

    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=CompletionQueue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        with (
            patch(
                "hephaestus.automation.pipeline.worker_pool._controlled_git_signing_env",
                return_value={},
            ),
            patch(
                "hephaestus.automation.git_utils._commit_changes",
                side_effect=RuntimeError("commit failed after validation"),
            ),
            patch("hephaestus.automation.git_utils.push_branch") as push,
        ):
            result = pool._git_commit_push(request.job)
    finally:
        pool.shutdown()

    assert result.ok is False
    assert result.error == "remediation writer commit did not complete"
    assert git("rev-parse", "HEAD", cwd=writer).stdout.strip() == head
    assert git("status", "--porcelain", cwd=writer).stdout.strip() == "M module.py"
    push.assert_not_called()

    stage.on_job_done(item, result, ctx)
    item.state = request.on_done_state
    retry = stage.step(item, ctx)
    assert retry == StageOutcome(Disposition.RETRY, "remediation preparation failed")
    assert PENDING_IMPLEMENTATION_REPLY_HANDOFF not in item.payload
