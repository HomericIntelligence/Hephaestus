"""Tests for queue-owned current plan scope reads."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.github_jobs import (
    CurrentPlanScopeRead,
    GitHubJob,
    ReadCurrentPlanScopeRequest,
)
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.stages import JobRequest, StageOutcome, implementation
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.test_source_worktree import _repository


@pytest.mark.parametrize("completion", ["valid", "failed", "wrong_request"])
def test_test_fix_requires_one_current_scope_read_per_turn(
    tmp_path: Path,
    make_ctx: Any,
    make_work_item: Any,
    monkeypatch: pytest.MonkeyPatch,
    completion: str,
) -> None:
    """Only a correlated scope receipt may authorize the next test-fix request."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    item = make_work_item(repo="repo", issue=42, state="TESTFIX_WAIT")
    item.worktree = str(binding.cwd)
    item.branch = "writer"
    item.payload.update(_impl_source_workspace=binding.to_dict(), _impl_source_revision=revision)
    ctx = make_ctx(paths=SimpleNamespace(repo_root=root, source_workspaces=manager))
    live = Mock(side_effect=RuntimeError("coordinator must not discover the live plan"))
    monkeypatch.setattr(ctx.github, "discover_plan", live)
    stage = implementation.ImplementationStage()
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitHubJob)
    assert isinstance(request.job.request, ReadCurrentPlanScopeRequest)
    assert request.on_done_state == "TESTFIX_WAIT"
    pending = request.job.request
    response_request = (
        replace(pending, issue_number=43) if completion == "wrong_request" else pending
    )
    receipt = CurrentPlanScopeRead(response_request, ("tracked.txt",), "b" * 64)
    stage.on_job_done(
        item,
        JobResult(ok=completion != "failed", value=receipt if completion != "failed" else None),
        ctx,
    )
    following = stage.step(item, ctx)
    if completion != "valid":
        assert isinstance(following, StageOutcome)
    else:
        assert isinstance(following, JobRequest)
        assert isinstance(following.job, AgentJob)
        assert following.job.source_operation is not None
        assert following.job.source_operation.allowed_paths == ("tracked.txt",)
        assert following.job.workspace == binding
        next_turn = stage.step(item, ctx)
        assert isinstance(next_turn, JobRequest)
        assert isinstance(next_turn.job, GitHubJob)
        assert isinstance(next_turn.job.request, ReadCurrentPlanScopeRequest)
        assert next_turn.job.request != pending
    live.assert_not_called()
