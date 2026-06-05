"""Tests for hephaestus.constants path helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus import constants


def test_repo_root_resolves_to_repo_containing_pyproject() -> None:
    """repo_root() finds the directory containing pyproject.toml."""
    root = constants.repo_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "hephaestus").is_dir()


def test_repo_root_honors_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """repo_root() uses HEPHAESTUS_REPO_ROOT env var when it contains pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setenv("HEPHAESTUS_REPO_ROOT", str(tmp_path))
    assert constants.repo_root() == tmp_path


def test_repo_root_ignores_env_without_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """repo_root() falls back to walk-up if env var path lacks pyproject.toml."""
    monkeypatch.setenv("HEPHAESTUS_REPO_ROOT", str(tmp_path))  # no pyproject.toml
    root = constants.repo_root()
    assert (root / "pyproject.toml").is_file()


def test_repo_root_ignores_nonexistent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """repo_root() falls back to walk-up if env var path does not exist."""
    monkeypatch.setenv("HEPHAESTUS_REPO_ROOT", "/nonexistent/path/xyz")
    root = constants.repo_root()
    assert (root / "pyproject.toml").is_file()


def test_scripts_dir_matches_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """scripts_dir() returns repo_root() / 'scripts'."""
    monkeypatch.delenv("HEPHAESTUS_REPO_ROOT", raising=False)
    assert constants.scripts_dir() == constants.repo_root() / "scripts"
    assert constants.scripts_dir().is_dir()
