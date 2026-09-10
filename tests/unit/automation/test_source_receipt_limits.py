"""Tests for bounded reads of source ownership receipts."""

from dataclasses import replace
from pathlib import Path

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.source_worktree import SourceWorkspaceError, SourceWorkspaceManager
from tests.unit.automation.test_source_worktree import _repository


def test_source_receipt_rejects_a_file_above_the_existing_evidence_limit(tmp_path: Path) -> None:
    """A valid JSON prefix must not permit an unbounded ownership receipt read."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    path = manager._receipt_path(42, SourceLane.IMPLEMENTATION)
    original = path.read_bytes()
    path.write_bytes(original + b" " * (65537 - len(original)))

    with pytest.raises(SourceWorkspaceError):
        manager._require_receipt(42, SourceLane.IMPLEMENTATION)

    assert path.stat().st_size == 65537


def test_source_receipt_write_limit_preserves_the_existing_receipt(tmp_path: Path) -> None:
    """A rejected receipt must not replace the current ownership record."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="repo")
    manager.prepare(42, SourceLane.IMPLEMENTATION, revision, branch="writer")
    current = manager._require_receipt(42, SourceLane.IMPLEMENTATION)
    path = manager._receipt_path(42, SourceLane.IMPLEMENTATION)
    original = path.read_bytes()

    with pytest.raises(SourceWorkspaceError):
        manager._write_receipt(replace(current, obligations=("x" * 65536,)))

    assert path.read_bytes() == original
