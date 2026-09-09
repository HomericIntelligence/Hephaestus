"""Tests for queue-owned finalization of one consumed dirty writer."""

import queue
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import DirtyPlanIdentity, SourceLane
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import JobRequest, StageOutcome, implementation
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.automation.worktree_snapshot import _dirty_worktree_content_snapshot
from tests.unit.agents.test_dirty_workspace import _claim
from tests.unit.automation.test_source_worktree import _git, _repository


def test_confirmed_dirty_pr_queues_source_finalization_before_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_ctx: Any,
    make_work_item: Any,
) -> None:
    """Only the worker may retire the consumed claim after strict PR readback."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    claim = replace(_claim(), reservation_base_sha=revision)
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, revision, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(
        12, claim=claim, expected_generation=original.generation
    )
    identity = DirtyPlanIdentity(
        claim.plan_revision, claim.plan_fingerprint, claim.review_fingerprint, claim.allowed_paths
    )
    with manager.acquire(binding, dirty_plan_identity=identity):
        pass
    with manager.dirty_direct_publication(binding) as advance:
        _git(binding.cwd, "commit", "-am", "controlled writer commit")
        head = _git(binding.cwd, "rev-parse", "HEAD")
        advance(head)
    ctx = make_ctx(paths=SimpleNamespace(repo_root=root, source_workspaces=manager))
    item = make_work_item(repo="repo", issue=12, state="COMMIT_PUSH_WAIT")
    item.worktree = str(binding.cwd)
    item.branch = claim.branch
    item.payload.update(
        dirty_direct_active=True,
        dirty_direct_preserve=True,
        dirty_direct_binding=binding.to_dict(),
        _direct_scope_reservation={"branch": claim.branch, "base_sha": revision},
    )
    stage = implementation.ImplementationStage()
    stage.on_job_done(item, JobResult(ok=True, value={"pushed": True, "head_sha": head}), ctx)
    item.state = "PR_CREATE"
    create = Mock(return_value=17)
    monkeypatch.setattr(ctx.github, "create_pr", create)
    monkeypatch.setattr(
        ctx.github,
        "gh_pr_state",
        lambda number: {"state": "OPEN", "headRefOid": head, "baseRefName": "main"},
    )
    monkeypatch.setattr(ctx.github, "get_pr_head_branch", lambda number: claim.branch)
    monkeypatch.setattr(ctx.github, "pr_head_is_writable", lambda number: True)
    live_source = Mock(side_effect=AssertionError("coordinator entered source finalization"))
    monkeypatch.setattr(implementation, "SourceWorkspaceManager", live_source)

    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    assert request.job.op == "finish_dirty_direct_publication"
    assert request.on_done_state == "PR_CREATE"
    assert item.payload["dirty_direct_preserve"] is True
    assert create.call_args.kwargs["strict_absence"] is True
    live_source.assert_not_called()
    pool = WorkerPool(size=1, shutdown=threading.Event(), completion_q=queue.Queue())
    try:
        completion = pool._run_git(request.job)
        assert completion.ok
        stage.on_job_done(item, completion, ctx)
        outcome = stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition is Disposition.ADVANCE
        create.assert_called_once()
        receipt = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
        assert receipt.schema_version == 1 and receipt.dirty_claim is None
        assert receipt.revision == head
        assert "dirty_direct_preserve" not in item.payload
        assert "_direct_scope_reservation" not in item.payload
    finally:
        pool.shutdown(mark_interrupted=False)
