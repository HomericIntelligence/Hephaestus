"""Tests for the UV-managed Bandit SAST configuration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef, unused-ignore]


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
    """The pre-commit hook invokes the project-managed Bandit."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "bandit"
    )
    assert hook["entry"].startswith("uv run bandit")


def test_low_severity_baseline_counts_are_integers() -> None:
    """The checked-in low-severity baseline stores integer counts."""
    baseline = json.loads((REPO_ROOT / "hephaestus/ci/bandit_low_baseline.json").read_text())
    assert all(isinstance(value, int) for value in baseline["counts"].values())
