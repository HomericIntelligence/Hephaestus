"""Tests for dirty source workspaces."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hephaestus.agents.workspace import (
    DirtyDirectClaim,
    SourceLane,
    WorkspaceBinding,
    WorkspaceBindingError,
    validate_workspace_binding,
)
from tests.unit.agents.test_workspace import _git, _repository


def _claim() -> DirtyDirectClaim:
    return DirtyDirectClaim(
        branch="12-auto-impl-direct-" + "a" * 32,
        reservation_base_sha="a" * 40,
        plan_revision=5,
        plan_fingerprint="b" * 64,
        review_fingerprint="b" * 64,
        allowed_paths=("tracked.txt",),
        index_sha256="c" * 64,
        worktree_sha256="d" * 64,
        untracked_sha256="e" * 64,
        nonce="f" * 32,
        state="armed",
    )


def test_dirty_claim_round_trip_is_closed() -> None:
    """Keep the claim immutable and reject extra serialized authority."""
    claim = _claim()
    assert DirtyDirectClaim.from_dict(claim.to_dict()) == claim
    with pytest.raises(WorkspaceBindingError):
        DirtyDirectClaim.from_dict({**claim.to_dict(), "permit": True})


@pytest.mark.parametrize("path", ["", "../file", "/file", "./file", "a//b", ".git/config"])
def test_dirty_claim_rejects_unsafe_scope(path: str) -> None:
    """Reject unsafe scope paths in serialized claims."""
    payload = _claim().to_dict()
    payload["allowed_paths"] = [path]
    with pytest.raises(WorkspaceBindingError):
        DirtyDirectClaim.from_dict(payload)


def _dirty_source_binding(tmp_path: Path) -> tuple[Path, WorkspaceBinding]:
    """Build a source binding whose worktree has uncommitted changes."""
    repo, revision = _repository(tmp_path)
    claim = replace(_claim(), reservation_base_sha=revision)
    path = tmp_path / "auto-12-impl"
    _git(repo, "worktree", "add", "-b", claim.branch, str(path), revision)
    (path / "tracked.txt").write_text("pending\n")
    binding = replace(
        WorkspaceBinding.source(
            cwd=path,
            reusable_root=repo,
            repository="example/project",
            ownership_key="example/project:12:impl",
            item_number=12,
            lane=SourceLane.IMPLEMENTATION,
            revision=revision,
            generation=2,
            detached=False,
        ),
        schema_version=2,
        dirty_claim=claim,
    )
    return path, binding


def test_dirty_source_workspace_is_accepted(tmp_path: Path) -> None:
    """Uncommitted changes do not block an agent invocation."""
    path, binding = _dirty_source_binding(tmp_path)

    assert validate_workspace_binding(binding) == path


def test_dirty_workspace_accepted_after_round_trip_through_json(tmp_path: Path) -> None:
    """A deserialized dirty binding works without a process-local permit."""
    path, binding = _dirty_source_binding(tmp_path)

    clone = WorkspaceBinding.from_dict(binding.to_dict())

    assert validate_workspace_binding(clone) == path


def test_dirty_source_workspace_accepted_again_after_use(tmp_path: Path) -> None:
    """A dirty workspace stays usable; no permit is consumed or revoked."""
    _, binding = _dirty_source_binding(tmp_path)

    validate_workspace_binding(binding)
    validate_workspace_binding(binding)


def test_untracked_file_does_not_block_invocation(tmp_path: Path) -> None:
    """An untracked scratch file is data for the agent, not a refusal."""
    path, binding = _dirty_source_binding(tmp_path)
    (path / "scratch.txt").write_text("notes\n")

    assert validate_workspace_binding(binding) == path


def test_workspace_on_a_different_branch_is_accepted(tmp_path: Path) -> None:
    """A reused worktree switched to another branch still runs."""
    path, binding = _dirty_source_binding(tmp_path)
    _git(path, "checkout", "-b", "12-impl-continuation")

    assert validate_workspace_binding(binding) == path


def test_workspace_revision_change_is_still_refused(tmp_path: Path) -> None:
    """A moved HEAD remains a real error, distinct from a dirty tree."""
    path, binding = _dirty_source_binding(tmp_path)
    (path / "extra.txt").write_text("x\n")
    _git(path, "add", "extra.txt")
    _git(path, "commit", "-m", "advance")

    with pytest.raises(WorkspaceBindingError, match="revision changed"):
        validate_workspace_binding(binding)
