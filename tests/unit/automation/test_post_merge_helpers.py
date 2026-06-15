"""Unit tests for PostMergeProcessor collaborator (refs #1179)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hephaestus.automation.post_merge_processor import PostMergeProcessor


def _make_processor(tmp_path: Path) -> tuple[PostMergeProcessor, dict[int, dict[str, Any]]]:
    """Return a PostMergeProcessor and the shared saved-state dict for assertions."""
    from unittest.mock import MagicMock

    options = MagicMock(dry_run=False, agent="claude")
    saved: dict[int, dict[str, Any]] = {}

    def load(issue: int) -> dict[str, Any] | None:
        return saved.get(issue)

    def save(issue: int, record: dict[str, Any]) -> None:
        saved[issue] = record

    proc = PostMergeProcessor(
        options_provider=lambda: options,
        repo_root_provider=lambda: tmp_path,
        get_worktree_path=lambda issue, pr: tmp_path / f"{issue}-{pr}",
        load_arming_state=load,
        save_arming_state=save,
    )
    return proc, saved


class TestMarkDriveGreenLearnResult:
    """Tests for PostMergeProcessor.mark_drive_green_learn_result."""

    def test_succeeded_writes_succeeded_status(self, tmp_path: Path) -> None:
        processor, _ = _make_processor(tmp_path)
        record: dict[str, Any] = {}
        processor.mark_drive_green_learn_result(1, record, succeeded=True)
        assert record["learn_status"] == "succeeded"
        assert "learn_succeeded_at" in record
        assert "learn_attempted_at" in record

    def test_failed_writes_failed_status(self, tmp_path: Path) -> None:
        processor, _ = _make_processor(tmp_path)
        record: dict[str, Any] = {}
        processor.mark_drive_green_learn_result(1, record, succeeded=False)
        assert record["learn_status"] == "failed"
        assert record["learn_succeeded_at"] is None
        assert record["learn_captured_at"] is None

    def test_save_called_on_success(self, tmp_path: Path) -> None:
        processor, saved = _make_processor(tmp_path)
        record: dict[str, Any] = {}
        processor.mark_drive_green_learn_result(42, record, succeeded=True)
        assert saved[42] is record

    def test_save_called_on_failure(self, tmp_path: Path) -> None:
        processor, saved = _make_processor(tmp_path)
        record: dict[str, Any] = {}
        processor.mark_drive_green_learn_result(42, record, succeeded=False)
        assert saved[42] is record
