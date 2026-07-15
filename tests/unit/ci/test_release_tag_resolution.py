"""Regression tests for centralized release-tag resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
DOWNSTREAM_JOBS = (
    "test",
    "type-check",
    "publish-testpypi",
    "build-and-publish",
    "deploy-docs",
)
PREPARED_TAG = "${{ needs.prepare.outputs.tag }}"


def _workflow() -> dict[str, Any]:
    """Load the release workflow."""
    return yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


def _step(job_name: str, step_name: str) -> dict[str, Any]:
    """Return one named step from a release job."""
    matches = [
        step for step in _workflow()["jobs"][job_name]["steps"] if step.get("name") == step_name
    ]
    assert len(matches) == 1
    return matches[0]


def test_prepare_is_only_tag_resolver() -> None:
    """Exactly one job must own tag resolution and expose its result."""
    workflow = _workflow()
    resolvers = [
        job_name
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if step.get("name") == "Resolve tag"
    ]
    assert resolvers == ["prepare"]
    assert workflow["jobs"]["prepare"]["outputs"]["tag"] == ("${{ steps.tag.outputs.tag }}")


def test_prepare_preserves_resolution_precedence_and_fails_empty() -> None:
    """Resolution must retain input/ref/latest precedence and fail closed."""
    script = _step("prepare", "Resolve tag")["run"]
    assert script.index('[ -n "$INPUT_TAG" ]') < script.index('[[ "$REF_TAG" == refs/tags/* ]]')
    assert script.index('[[ "$REF_TAG" == refs/tags/* ]]') < script.index("git tag --list 'v*'")
    assert '[ -z "$TAG" ]' in script
    assert "git show-ref --verify --quiet" in script
    assert "| head -1" not in script
    assert "| sed -n '1p'" in script


def test_all_downstream_jobs_depend_on_prepare() -> None:
    """Every job consuming the resolved tag must directly need prepare."""
    jobs = _workflow()["jobs"]
    for job_name in DOWNSTREAM_JOBS:
        needs = jobs[job_name]["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert "prepare" in needs


def test_all_downstream_checkouts_use_prepared_tag() -> None:
    """Validation and release jobs must checkout the identical prepared tag."""
    for job_name in DOWNSTREAM_JOBS:
        checkout = next(
            step
            for step in _workflow()["jobs"][job_name]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == PREPARED_TAG


def test_release_tag_consumers_use_prepare_output() -> None:
    """Publishing steps must consume the canonical prepare output."""
    assert _step("publish-testpypi", "Smoke-install from TestPyPI")["env"]["TAG"] == (PREPARED_TAG)
    assert (
        _step("build-and-publish", "Verify tag matches package version")["env"]["TAG"]
        == PREPARED_TAG
    )
    assert _step("build-and-publish", "Check for existing release")["env"]["TAG"] == (PREPARED_TAG)
    assert _step("build-and-publish", "Create GitHub Release")["with"]["tag_name"] == (PREPARED_TAG)
    assert (
        _step("build-and-publish", "Attach build artifacts to existing release")["env"]["TAG"]
        == PREPARED_TAG
    )
