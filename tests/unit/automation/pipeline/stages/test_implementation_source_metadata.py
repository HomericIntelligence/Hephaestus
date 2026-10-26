"""Tests for source metadata consumed by the implementation coordinator."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillJob
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.stages import JobRequest, implementation
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.test_source_worktree import _repository


@pytest.mark.parametrize("state", ["ADVISE_WAIT", "IMPLEMENT_WAIT"])
def test_prepared_writer_jobs_use_frozen_source_metadata(
    tmp_path: Path,
    make_ctx: Any,
    make_work_item: Any,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """A prepared writer must not repeat live source preparation on the coordinator."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    item = make_work_item(repo="repo", issue=42, state=state)
    item.worktree = str(binding.cwd)
    item.branch = "writer"
    item.payload.update(_impl_source_workspace=binding.to_dict(), _impl_source_revision=revision)
    ctx = make_ctx(
        paths=SimpleNamespace(repo_root=root, source_workspaces=manager),
        config_overrides={"no_advise": False},
    )
    prepare = Mock(side_effect=AssertionError("coordinator repeated source preparation"))
    monkeypatch.setattr(manager, "prepare_bounded", prepare)
    monkeypatch.setattr(manager, "prepare", prepare)
    request = implementation.ImplementationStage().step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, AgentJob | AthenaSkillJob)
    actual = (
        request.job.workspace
        if isinstance(request.job, AgentJob)
        else request.job.request.workspace
    )
    assert actual == binding
    prepare.assert_not_called()
