"""Tests for one-use dirty workspace data and active permits."""

from contextvars import copy_context
from dataclasses import replace
from pathlib import Path

import pytest

from hephaestus.agents.workspace import (
    DirtyDirectClaim,
    SourceLane,
    WorkspaceBinding,
    WorkspaceBindingError,
    _dirty_workspace_permit,
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


def test_dirty_binding_needs_exact_active_permit(tmp_path: Path) -> None:
    """Serialized, cloned, and expired data cannot grant dirty execution."""
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
    clone = WorkspaceBinding.from_dict(binding.to_dict())
    with pytest.raises(WorkspaceBindingError, match="permit"):
        validate_workspace_binding(binding)
    with _dirty_workspace_permit(binding) as permit:
        captured_context = copy_context()
        assert validate_workspace_binding(binding, dirty_permit=permit) == path
        with pytest.raises(WorkspaceBindingError, match="permit"):
            validate_workspace_binding(clone, dirty_permit=permit)
    with pytest.raises(WorkspaceBindingError, match="permit"):
        validate_workspace_binding(binding, dirty_permit=permit)
    with pytest.raises(WorkspaceBindingError, match="permit"):
        captured_context.run(validate_workspace_binding, binding, dirty_permit=permit)
