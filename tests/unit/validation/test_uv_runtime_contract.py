"""Regression coverage for the source-checkout uv runtime contract."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_default_uv_groups_include_automation_without_widening_base_dependencies() -> None:
    """Source-checkout automation has pydantic while the published base does not."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "automation" in config["tool"]["uv"]["default-groups"]
    assert any(
        requirement.startswith("pydantic")
        for requirement in config["dependency-groups"]["automation"]
    )
    assert not any(
        requirement.startswith("pydantic") for requirement in config["project"]["dependencies"]
    )


def test_uv_is_the_only_project_environment_manifest() -> None:
    """The repository exposes uv's lockfile, not a second environment manager."""
    assert (REPO_ROOT / "uv.lock").is_file()
    assert not (REPO_ROOT / "pixi.toml").exists()
    assert not (REPO_ROOT / "pixi.lock").exists()
