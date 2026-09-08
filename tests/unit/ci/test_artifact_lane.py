"""Contracts for the required reproducible-artifact validation lane."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_required.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def test_default_pytest_options_select_the_fast_lane() -> None:
    """A normal host run selects only the shared fast test lane."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    marker_expression = next(
        addopts[index + 1] for index, value in enumerate(addopts) if value == "-m"
    )

    assert marker_expression == "precommit"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load a workflow document as a mapping."""
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _step_run(workflow: dict[str, Any], job_name: str, step_name: str) -> str:
    """Return the shell command from a named workflow step."""
    steps = workflow["jobs"][job_name]["steps"]
    step = next(step for step in steps if step.get("name") == step_name)
    return str(step["run"])


def test_nightly_build_job_runs_artifact_suite() -> None:
    """The nightly build job must fail closed through the artifact marker."""
    workflow = _load_workflow(REPO_ROOT / ".github" / "workflows" / "nightly-tests.yml")
    build_run = _step_run(
        workflow,
        "build",
        "Validate reproducible artifacts and package lifecycle",
    )

    assert '-m "artifact and not codex_release_artifact"' in build_run
    assert "--basetemp=build/pytest-artifacts" in build_run
    assert "build" not in _load_workflow(REQUIRED_WORKFLOW)["jobs"]


def test_required_workflow_has_no_standalone_test_jobs() -> None:
    """PR CI relies on the fast pre-commit hook for normal test execution."""
    jobs = _load_workflow(REQUIRED_WORKFLOW)["jobs"]

    retired_jobs = {
        "unit-tests",
        "integration-tests",
        "installed-cli-tests",
        "shell-tests",
        "build",
    }
    assert retired_jobs.isdisjoint(jobs)


def test_release_integration_job_uses_the_full_release_selection() -> None:
    """Release validation runs every normal integration test except its fixture lane."""
    workflow = _load_workflow(RELEASE_WORKFLOW)
    release_run = _step_run(workflow, "test", "Run integration tests")

    assert "not performance and not contract and not codex_release_artifact" in release_run
    assert "not artifact" not in release_run
    test_steps = workflow["jobs"]["test"]["steps"]
    assert not any(step.get("name") == "Provision Codex Sigstore fixture" for step in test_steps)


def test_release_unit_job_uses_the_explicit_full_release_selection() -> None:
    """Release unit validation must not inherit the fast pre-commit default."""
    workflow = _load_workflow(RELEASE_WORKFLOW)
    release_run = _step_run(workflow, "test", "Run unit tests")

    assert '--override-ini="addopts="' in release_run
    assert "not performance and not contract" in release_run


def test_nightly_functional_job_does_not_repeat_full_unit_coverage() -> None:
    """The functional lane owns the remaining integration tests, not unit coverage."""
    workflow = _load_workflow(REPO_ROOT / ".github" / "workflows" / "nightly-tests.yml")
    functional_run = _step_run(workflow, "functional-tests", "Run remaining functional tests")

    assert "pytest tests/integration" in functional_run
    assert "tests/unit" not in functional_run
