"""Unit tests for ``hephaestus.config.paths.resolve_projects_dir``.

Covers the priority order (override > env > default), the warning emitted
on fallback (distinct for unset vs. nonexistent ``PROJECTS_ROOT``), and the
per-process de-duplication guard.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.config import paths as paths_mod
from hephaestus.config.paths import DEFAULT_PROJECTS_DIR, resolve_projects_dir


@pytest.fixture(autouse=True)
def _reset_warned_keys() -> Iterator[None]:
    """Clear the per-process warning-dedup guard before & after each test.

    Without this, tests would leak state into each other and the
    de-duplication test would race other cases that share the
    ``(None, None)`` key.
    """
    paths_mod._warned_keys.clear()
    yield
    paths_mod._warned_keys.clear()


def test_override_takes_priority_no_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """An explicit override wins over both env and default; no warning fires."""
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="hephaestus.config.paths"):
        result = resolve_projects_dir("/some/explicit/path")
    assert result == Path("/some/explicit/path")
    assert caplog.records == []


def test_env_var_used_when_dir_exists_no_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """PROJECTS_ROOT pointing at a real directory is honored silently."""
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="hephaestus.config.paths"):
        result = resolve_projects_dir()
    assert result == tmp_path
    assert caplog.records == []


def test_env_var_dir_missing_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A nonexistent PROJECTS_ROOT triggers a 'does not exist' warning + default."""
    nonexistent = tmp_path / "does-not-exist"
    monkeypatch.setenv("PROJECTS_ROOT", str(nonexistent))
    with caplog.at_level(logging.WARNING, logger="hephaestus.config.paths"):
        result = resolve_projects_dir()
    assert result == DEFAULT_PROJECTS_DIR
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "does not exist" in caplog.records[0].getMessage()
    assert str(nonexistent) in caplog.records[0].getMessage()


def test_prefer_cwd_parent_uses_current_checkout_parent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Loop callers can default to the projects root that owns the cwd checkout."""
    checkout = tmp_path / "projects" / "ProjectHephaestus"
    checkout.mkdir(parents=True)
    monkeypatch.delenv("PROJECTS_ROOT", raising=False)
    result_mock = MagicMock(stdout=f"{checkout}\n")

    with (
        patch("hephaestus.config.paths.subprocess.run", return_value=result_mock),
        caplog.at_level(logging.INFO, logger="hephaestus.config.paths"),
    ):
        result = resolve_projects_dir(prefer_cwd_parent=True)

    assert result == checkout.parent
    assert caplog.records == []


def test_prefer_cwd_parent_falls_back_when_git_root_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cwd-parent preference is best-effort and keeps the historical fallback."""
    monkeypatch.delenv("PROJECTS_ROOT", raising=False)

    with patch(
        "hephaestus.config.paths.subprocess.run",
        side_effect=subprocess.CalledProcessError(128, ["git"]),
    ):
        result = resolve_projects_dir(prefer_cwd_parent=True)

    assert result == DEFAULT_PROJECTS_DIR


def test_no_override_no_env_no_warning_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Unset PROJECTS_ROOT is benign: nothing at INFO/WARNING, only DEBUG (#1556)."""
    monkeypatch.delenv("PROJECTS_ROOT", raising=False)
    with caplog.at_level(logging.INFO, logger="hephaestus.config.paths"):
        result = resolve_projects_dir()
    assert result == DEFAULT_PROJECTS_DIR
    # Benign fallback must not surface at INFO or above on a default run.
    assert caplog.records == []


def test_no_override_no_env_emits_debug_under_verbose(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The 'not set' fallback message is still available at DEBUG (verbose mode)."""
    monkeypatch.delenv("PROJECTS_ROOT", raising=False)
    with caplog.at_level(logging.DEBUG, logger="hephaestus.config.paths"):
        result = resolve_projects_dir()
    assert result == DEFAULT_PROJECTS_DIR
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert "PROJECTS_ROOT not set" in caplog.records[0].getMessage()


def test_debug_deduplicated_within_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Calling resolve twice with the same (override, env) key logs only once."""
    monkeypatch.delenv("PROJECTS_ROOT", raising=False)
    with caplog.at_level(logging.DEBUG, logger="hephaestus.config.paths"):
        first = resolve_projects_dir()
        second = resolve_projects_dir()
    assert first == DEFAULT_PROJECTS_DIR
    assert second == DEFAULT_PROJECTS_DIR
    assert len(caplog.records) == 1
