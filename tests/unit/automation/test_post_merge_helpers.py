"""Unit tests for PostMergeProcessor collaborator (refs #1179)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.post_merge_processor import PostMergeProcessor

_MOD = "hephaestus.automation.post_merge_processor"


def _raise_no_worktree(issue: int, pr: int) -> Path:
    """get_worktree_path stub that raises, to exercise the repo_root fallback."""
    raise RuntimeError("worktree gone post-merge")


def _make_processor(tmp_path: Path) -> tuple[PostMergeProcessor, dict[int, dict[str, Any]]]:
    """Return a PostMergeProcessor and the shared saved-state dict for assertions."""
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


def _make_codex_processor(tmp_path: Path) -> PostMergeProcessor:
    """PostMergeProcessor whose options report agent='codex'."""
    options = MagicMock(dry_run=False, agent="codex")
    saved: dict[int, dict[str, Any]] = {}
    return PostMergeProcessor(
        options_provider=lambda: options,
        repo_root_provider=lambda: tmp_path,
        get_worktree_path=lambda issue, pr: tmp_path / f"{issue}-{pr}",
        load_arming_state=lambda i: saved.get(i),
        save_arming_state=lambda i, r: saved.__setitem__(i, r),
    )


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


class TestRunDriveGreenLearnings:
    """Tests for PostMergeProcessor.run_drive_green_learnings (post_merge_processor.py:102)."""

    def test_claude_path_returns_true_and_captures_evidence(self, tmp_path: Path) -> None:
        processor, _ = _make_processor(tmp_path)
        sentinel = {"mnemosyne_update_status": "ok", "mnemosyne_update_urls": ["u"]}
        with (
            patch(f"{_MOD}.get_repo_slug", return_value="o/r"),
            patch(f"{_MOD}.invoke_claude_with_session", return_value=("learn-stdout", None)) as inv,
            patch(f"{_MOD}.mnemosyne_update_evidence", return_value=sentinel),
        ):
            result = processor.run_drive_green_learnings(1, 2)
        assert result is True
        assert inv.called
        kwargs = inv.call_args.kwargs
        assert kwargs["allowed_tools"] == "Read,Write,Edit,Glob,Grep,Bash"
        assert "extra_args" not in kwargs
        assert processor._last_learn_evidence == sentinel

    def test_codex_path_uses_run_agent_session(self, tmp_path: Path) -> None:
        processor = _make_codex_processor(tmp_path)
        with (
            patch(f"{_MOD}.get_repo_slug", return_value="o/r"),
            patch(f"{_MOD}.run_agent_session", return_value=MagicMock(stdout="codex-out")) as agent,
            patch(f"{_MOD}.invoke_claude_with_session") as inv,
        ):
            result = processor.run_drive_green_learnings(3, 4)
        assert result is True
        assert agent.call_args.kwargs["agent"] == "codex"
        assert not inv.called

    def test_worktree_failure_falls_back_to_repo_root(self, tmp_path: Path) -> None:
        options = MagicMock(dry_run=False, agent="claude")
        proc = PostMergeProcessor(
            options_provider=lambda: options,
            repo_root_provider=lambda: tmp_path,
            get_worktree_path=_raise_no_worktree,
            load_arming_state=lambda i: None,
            save_arming_state=lambda i, r: None,
        )
        with (
            patch(f"{_MOD}.get_repo_slug", return_value="o/r"),
            patch(f"{_MOD}.invoke_claude_with_session", return_value=("ok", None)) as inv,
        ):
            result = proc.run_drive_green_learnings(5, 6)
        assert result is True
        assert inv.call_args.kwargs["cwd"] == tmp_path

    def test_subprocess_exception_is_swallowed_returns_false(self, tmp_path: Path) -> None:
        processor, _ = _make_processor(tmp_path)
        with (
            patch(f"{_MOD}.get_repo_slug", return_value="o/r"),
            patch(
                f"{_MOD}.invoke_claude_with_session",
                side_effect=RuntimeError("claude boom"),
            ),
        ):
            result = processor.run_drive_green_learnings(7, 8)
        assert result is False

    def test_called_process_error_log_includes_stderr(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        processor, _ = _make_processor(tmp_path)
        err = subprocess.CalledProcessError(
            returncode=1,
            cmd=["claude", "--resume", "abc"],
            stderr="No conversation found with session ID abc",
        )
        with (
            patch(f"{_MOD}.get_repo_slug", return_value="o/r"),
            patch(f"{_MOD}.invoke_claude_with_session", side_effect=err),
            caplog.at_level(logging.WARNING, logger=_MOD),
        ):
            result = processor.run_drive_green_learnings(7, 8)

        assert result is False
        assert "No conversation found with session ID abc" in caplog.text


class TestRunDriveGreenCompact:
    """Tests for PostMergeProcessor.run_drive_green_compact (post_merge_processor.py:186)."""

    def test_codex_short_circuits_without_compact(self, tmp_path: Path) -> None:
        processor = _make_codex_processor(tmp_path)
        with patch(f"{_MOD}.compact_session") as compact:
            result = processor.run_drive_green_compact(1, 2)
        assert result is False
        assert not compact.called

    def test_claude_path_calls_compact_session(self, tmp_path: Path) -> None:
        processor, _ = _make_processor(tmp_path)
        with (
            patch(f"{_MOD}.get_repo_slug", return_value="o/r"),
            patch(f"{_MOD}.compact_session", return_value=True) as compact,
        ):
            result = processor.run_drive_green_compact(3, 4)
        assert result is True
        assert compact.call_args.kwargs["cwd"] == tmp_path / "3-4"

    def test_worktree_failure_falls_back_to_repo_root(self, tmp_path: Path) -> None:
        options = MagicMock(dry_run=False, agent="claude")
        proc = PostMergeProcessor(
            options_provider=lambda: options,
            repo_root_provider=lambda: tmp_path,
            get_worktree_path=_raise_no_worktree,
            load_arming_state=lambda i: None,
            save_arming_state=lambda i, r: None,
        )
        with (
            patch(f"{_MOD}.get_repo_slug", return_value="o/r"),
            patch(f"{_MOD}.compact_session", return_value=True) as compact,
        ):
            result = proc.run_drive_green_compact(5, 6)
        assert result is True
        assert compact.call_args.kwargs["cwd"] == tmp_path
