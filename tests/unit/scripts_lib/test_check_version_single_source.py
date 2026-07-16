"""Tests for the dynamic-version single-source check."""

from __future__ import annotations

from pathlib import Path

from hephaestus.scripts_lib import check_version_single_source as mod

VALID_PYPROJECT = """[project]
name = "mypkg"
dynamic = ["version"]

[tool.hatch.version]
source = "vcs"
"""


def test_dynamic_hatch_vcs_configuration_passes(tmp_path: Path) -> None:
    """A dynamic hatch-vcs project retains one version authority."""
    (tmp_path / "pyproject.toml").write_text(VALID_PYPROJECT)
    assert mod.check_pyproject_dynamic_version(tmp_path) is True


def test_static_project_version_is_rejected(tmp_path: Path) -> None:
    """A static value cannot coexist with the git-derived version."""
    static_version = VALID_PYPROJECT.replace('dynamic = ["version"]', 'version = "1.0.0"')
    (tmp_path / "pyproject.toml").write_text(static_version)
    assert mod.check_pyproject_dynamic_version(tmp_path) is False


def test_main_validates_current_repository_configuration(monkeypatch) -> None:
    """The command validates the repository's actual metadata."""
    monkeypatch.setattr(mod, "get_repo_root", lambda: Path(__file__).resolve().parents[3])
    assert mod.main() == 0
