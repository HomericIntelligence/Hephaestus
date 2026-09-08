"""Tests for the UV-managed Bandit SAST configuration."""

from __future__ import annotations

import json
import re
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


def test_required_workflow_and_precommit_use_uv_bandit() -> None:
    """Both required enforcement paths invoke the project-managed Bandit."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/_required.yml").read_text())
    sast = workflow["jobs"]["security-sast-scan"]
    run_step = next(
        step for step in sast["steps"] if step.get("name") == "Run bandit (SAST, in container)"
    )
    assert "uv run bandit" in run_step["run"]
    assert "podman run" in run_step["run"]
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "bandit"
    )
    assert hook["entry"].startswith("uv run bandit")


def test_low_baseline_scan_defers_exit_status_to_checker() -> None:
    """The LOW scan must emit its report before the custom checker decides."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["sast"]["steps"]
    low_step = next(step for step in steps if step.get("id") == "bandit-low")
    summary_step = next(step for step in steps if step.get("name") == "Post bandit summary")

    assert "--exit-zero" in low_step["run"]
    assert "bandit_baseline_check.py" in low_step["run"]
    assert summary_step["env"]["BANDIT_LOW_OUTCOME"] == ("${{ steps.bandit-low.outcome }}")


def test_low_baseline_ids_have_reviewed_pyproject_rationale_bullets() -> None:
    """Every checked-in LOW finding ID has a rationale in the Bandit block."""
    baseline = json.loads(
        (REPO_ROOT / "hephaestus/ci/bandit_low_baseline.json").read_text(encoding="utf-8")
    )
    baseline_ids = set(baseline["counts"])
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^\[tool\.bandit\]\n(?P<body>.*?)(?=^\[|\Z)", pyproject)
    assert match is not None

    rationale_by_id: dict[str, str] = {}
    for line in match.group("body").splitlines():
        bullet = re.match(
            r"^#\s+((?:B\d{3})(?:/B\d{3})*)\s+\((?P<rationale>.+)$",
            line,
        )
        if bullet is None:
            continue
        rationale = bullet.group("rationale").strip()
        for test_id in bullet.group(1).split("/"):
            rationale_by_id[test_id] = rationale

    assert "B110" in rationale_by_id
    assert baseline_ids <= set(rationale_by_id)
    assert all(rationale_by_id[test_id] for test_id in baseline_ids)
