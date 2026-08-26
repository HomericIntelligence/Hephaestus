"""Unit tests for explicit/filesystem-only project-directory resolution."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.config.paths import DEFAULT_PROJECTS_DIR, resolve_projects_dir


def test_override_takes_priority_no_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit override wins over poison environment state."""
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    result = resolve_projects_dir("/some/explicit/path")
    assert result == Path("/some/explicit/path")


def test_relative_override_is_resolved_from_the_invocation_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative CLI projects root cannot leak into Git worktree commands."""
    monkeypatch.chdir(tmp_path)

    result = resolve_projects_dir("loop-validation")

    assert result == tmp_path / "loop-validation"
    assert result.is_absolute()


def test_projects_root_environment_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The retired PROJECTS_ROOT variable cannot affect the default."""
    projects_dir = tmp_path / "loop-validation"
    projects_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROJECTS_ROOT", "loop-validation")

    result = resolve_projects_dir()

    assert result == DEFAULT_PROJECTS_DIR


def test_prefer_cwd_parent_uses_current_checkout_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loop callers can default to the projects root that owns the cwd checkout."""
    checkout = tmp_path / "projects" / "Hephaestus"
    checkout.mkdir(parents=True)
    result_mock = MagicMock(stdout=f"{checkout}\n")

    with (
        patch("hephaestus.config.paths.subprocess.run", return_value=result_mock),
    ):
        result = resolve_projects_dir(prefer_cwd_parent=True)

    assert result == checkout.parent


def test_prefer_cwd_parent_unwraps_automation_issue_worktree(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Loop defaults must not treat build/.worktrees as the projects root."""
    checkout = tmp_path / "projects" / "Hephaestus"
    issue_worktree = checkout / "build" / ".worktrees" / "issue-1442"
    issue_worktree.mkdir(parents=True)
    result_mock = MagicMock(stdout=f"{issue_worktree}\n")

    with (
        patch("hephaestus.config.paths.subprocess.run", return_value=result_mock),
        caplog.at_level(logging.WARNING, logger="hephaestus.config.paths"),
    ):
        result = resolve_projects_dir(prefer_cwd_parent=True)

    assert result == checkout.parent
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "automation issue worktree" in caplog.records[0].getMessage()
    assert str(issue_worktree) in caplog.records[0].getMessage()


def test_prefer_cwd_parent_falls_back_when_git_root_missing(
    tmp_path: Path,
) -> None:
    """The cwd-parent preference is best-effort and keeps the historical fallback."""
    with patch(
        "hephaestus.config.paths.subprocess.run",
        side_effect=subprocess.CalledProcessError(128, ["git"]),
    ):
        result = resolve_projects_dir(prefer_cwd_parent=True)

    assert result == DEFAULT_PROJECTS_DIR


def test_no_override_uses_fixed_default() -> None:
    """Without explicit discovery preference the historical default remains."""
    assert resolve_projects_dir() == DEFAULT_PROJECTS_DIR
