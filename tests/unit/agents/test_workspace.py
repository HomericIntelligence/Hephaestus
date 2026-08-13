"""Tests for provider-neutral agent workspace bindings."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.agents.workspace import (
    SourceLane,
    WorkspaceBinding,
    WorkspaceBindingError,
    WorkspaceKind,
    validate_workspace_binding,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_source_binding_rejects_reusable_repository_root(tmp_path: Path) -> None:
    """A typed source binding never permits the ambient primary checkout."""
    repo, revision = _repository(tmp_path)
    binding = WorkspaceBinding.source(
        cwd=repo,
        reusable_root=repo,
        repository="example/project",
        ownership_key="example/project:12:impl",
        item_number=12,
        lane=SourceLane.IMPLEMENTATION,
        revision=revision,
        generation=1,
        detached=False,
    )

    with pytest.raises(WorkspaceBindingError, match="reusable repository root"):
        validate_workspace_binding(binding)


def test_session_only_binding_rejects_source_read_tools(tmp_path: Path) -> None:
    """Session-only storage cannot be upgraded into a source workspace."""
    binding = WorkspaceBinding.session_only(tmp_path)

    with pytest.raises(WorkspaceBindingError, match="source-reading tool"):
        validate_workspace_binding(binding, allowed_tools="Read,Glob,Grep")


def test_workspace_binding_json_is_strict(tmp_path: Path) -> None:
    """Unknown serialized fields fail closed."""
    binding = WorkspaceBinding.external(tmp_path)
    payload = binding.to_dict()
    payload["unexpected"] = True

    with pytest.raises(WorkspaceBindingError, match="unknown fields"):
        WorkspaceBinding.from_dict(payload)

    assert WorkspaceBinding.from_dict(binding.to_dict()).kind is WorkspaceKind.EXTERNAL
