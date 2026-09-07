"""Contracts for the required reproducible-artifact validation lane."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_required.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load a workflow document as a mapping."""
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _step_run(workflow: dict[str, Any], job_name: str, step_name: str) -> str:
    """Return the shell command from a named workflow step."""
    steps = workflow["jobs"][job_name]["steps"]
    step = next(step for step in steps if step.get("name") == step_name)
    return str(step["run"])


def _step(workflow: dict[str, Any], job_name: str, step_name: str) -> dict[str, Any]:
    """Return one complete named workflow step."""
    steps = workflow["jobs"][job_name]["steps"]
    return cast(dict[str, Any], next(step for step in steps if step.get("name") == step_name))


def test_required_build_job_runs_artifact_suite() -> None:
    """The required build job must fail closed through the artifact marker."""
    workflow = _load_workflow(REQUIRED_WORKFLOW)
    build_run = _step_run(
        workflow,
        "build",
        "Validate reproducible artifacts and package lifecycle",
    )

    assert "-m artifact" in build_run
    assert "--basetemp=build/pytest-artifacts" in build_run
    assert "build" in workflow["jobs"]["required-checks-gate"]["needs"]


def test_general_integration_job_excludes_artifact_suite() -> None:
    """General integration invocations leave the artifact lane isolated."""
    integration_runs = (
        _step_run(
            _load_workflow(REQUIRED_WORKFLOW),
            "integration-tests",
            "Run integration tests (in container)",
        ),
        _step_run(
            _load_workflow(TEST_WORKFLOW),
            "test",
            "Run integration tests",
        ),
    )

    assert all("not nightly and not artifact" in run for run in integration_runs)


def test_release_integration_job_uses_the_offline_artifact_lane() -> None:
    """Release validation repeats the exact offline artifact boundary."""
    workflow = _load_workflow(RELEASE_WORKFLOW)
    release_run = _step_run(workflow, "test", "Run integration tests")
    provision = _step(workflow, "test", "Provision Codex Sigstore fixture")
    artifact_run = _step_run(workflow, "test", "Run offline Codex artifact tests")

    assert "not nightly and not artifact" in release_run
    assert provision["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}
    assert "zstd --version" in provision["run"]
    assert "scripts/provision_codex_sigstore_fixture.py" in provision["run"]
    assert "podman build -f ci/Containerfile -t hephaestus-ci:local ." in _step_run(
        workflow,
        "test",
        "Build CI image for offline artifact tests",
    )
    assert "--network=none" in artifact_run
    assert "HEPHAESTUS_CODEX_SIGSTORE_FIXTURE_ROOT=/codex-sigstore/rust-v0.153.4" in artifact_run
    assert (
        "build/test-fixtures/codex-sigstore/rust-v0.153.4:"
        "/codex-sigstore/rust-v0.153.4:ro" in artifact_run
    )
    assert (
        "build/test-fixtures/codex-sigstore/rust-v0.153.4:"
        "/workspace/build/test-fixtures/codex-sigstore/rust-v0.153.4:ro" in artifact_run
    )
    assert "-m artifact" in artifact_run
