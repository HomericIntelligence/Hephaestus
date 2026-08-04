"""Tests for the UV-managed Bandit SAST configuration."""

from __future__ import annotations

import tomllib  # type: ignore[no-redef, unused-ignore]
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_bandit_is_a_versioned_dev_dependency() -> None:
    """The project-managed development environment supplies Bandit."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    assert any(
        dependency.startswith("bandit>=") for dependency in config["dependency-groups"]["dev"]
    )


def test_bandit_configuration_excludes_generated_and_test_paths() -> None:
    """Bandit ignores test, build, and local-environment paths."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    excluded = config["tool"]["bandit"]["exclude_dirs"]
    assert {"tests", "build", ".venv"}.issubset(excluded)


def test_precommit_uses_uv_bandit() -> None:
    """The local enforcement path invokes the project-managed Bandit."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "bandit"
    )
    assert hook["entry"].startswith("uv run bandit")
