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


def test_required_workflow_and_precommit_use_uv_bandit() -> None:
    """Both required enforcement paths invoke the project-managed Bandit."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/_required.yml").read_text())
    sast = workflow["jobs"]["security-sast-scan"]
    run_step = next(step for step in sast["steps"] if step.get("name") == "Run bandit (SAST)")
    assert "uv run bandit" in run_step["run"]

    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "bandit"
    )
    assert hook["entry"].startswith("uv run bandit")


def test_low_severity_baseline_is_checked_in_weekly_security_workflow() -> None:
    """The weekly scan retains its low-severity baseline regression check."""
    security = (REPO_ROOT / ".github/workflows/security.yml").read_text()
    assert "bandit_baseline_check.py" in security
    baseline = json.loads((REPO_ROOT / "hephaestus/ci/bandit_low_baseline.json").read_text())
    assert all(isinstance(value, int) for value in baseline["counts"].values())
