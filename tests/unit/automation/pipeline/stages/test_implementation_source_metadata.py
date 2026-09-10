"""Tests for source metadata consumed by the implementation coordinator."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillJob
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.stages import JobRequest, implementation
from hephaestus.automation.remediation_prepublication import canonical_source_receipt_json
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


@pytest.mark.parametrize("stale", [False, True])
def test_pretest_input_uses_exact_frozen_receipt_without_live_io(
    tmp_path: Path,
    make_ctx: Any,
    make_work_item: Any,
    monkeypatch: pytest.MonkeyPatch,
    stale: bool,
) -> None:
    """A stage may consume only the frozen receipt for its current binding."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    receipt = manager._require_receipt(42, SourceLane.IMPLEMENTATION)
    if stale:
        receipt = replace(receipt, generation=receipt.generation + 1)
    item = make_work_item(repo="repo", issue=42, pr=43, state="IMPLEMENT_WAIT")
    item.worktree = str(binding.cwd)
    item.branch = "writer"
    item.payload.update(
        _impl_source_workspace=binding.to_dict(),
        _impl_source_revision=revision,
        _impl_source_receipt=receipt,
        remediation_thread_snapshots=[
            {
                "id": "thread-1",
                "comments": [{"id": "comment-1", "author": "reviewer", "body": "Fix."}],
            }
        ],
    )
    ctx = make_ctx(paths=SimpleNamespace(repo_root=root, source_workspaces=manager))
    live = Mock(side_effect=AssertionError("coordinator read live source metadata"))
    monkeypatch.setattr(manager, "_require_receipt", live)
    monkeypatch.setattr(
        implementation, "_pretest_scope", lambda *args: (("tracked.txt",), "b" * 64)
    )
    if stale:
        with pytest.raises(ValueError):
            implementation._new_pretest_input(item, ctx)
    else:
        inputs = implementation._new_pretest_input(item, ctx)
        assert inputs.source_receipt_json == canonical_source_receipt_json(receipt)
    live.assert_not_called()
