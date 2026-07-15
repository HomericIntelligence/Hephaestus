"""Structural regression tests for the TestPyPI staging gate in release.yml.

Parses the checked-in workflow YAML and asserts the job graph / artifact
wiring introduced by the TestPyPI staging gate (issue #1475). Does not spin
up GitHub Actions; it is a static guard against a wiring regression such as
an artifact-name rename that silently breaks build-once/deploy-elsewhere
reuse between ``publish-testpypi`` and ``build-and-publish``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load_workflow() -> dict[str, Any]:
    return yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


def _job(name: str) -> dict[str, Any]:
    return _load_workflow()["jobs"][name]


def _steps(job_name: str) -> list[dict[str, Any]]:
    return _job(job_name)["steps"]


def _step_using(job_name: str, action_substring: str) -> dict[str, Any]:
    matches = [s for s in _steps(job_name) if action_substring in str(s.get("uses", ""))]
    assert len(matches) == 1, (
        f"expected exactly one step using '{action_substring}' in job '{job_name}', "
        f"found {len(matches)}"
    )
    return matches[0]


class TestPublishTestPyPIJobExists:
    """The staging job must exist and run before build-and-publish."""

    def test_job_present(self) -> None:
        assert "publish-testpypi" in _load_workflow()["jobs"]

    def test_needs_prepare_test_and_type_check(self) -> None:
        assert _job("publish-testpypi")["needs"] == [
            "prepare",
            "test",
            "type-check",
        ]

    def test_uses_testpypi_environment(self) -> None:
        assert _job("publish-testpypi")["environment"] == "testpypi"


class TestBuildAndPublishDependsOnTestPyPI:
    """Production publish must not proceed until the TestPyPI gate passes."""

    def test_needs_includes_prepare_and_publish_testpypi(self) -> None:
        assert "publish-testpypi" in _job("build-and-publish")["needs"]
        assert "prepare" in _job("build-and-publish")["needs"]

    def test_still_needs_test_and_type_check(self) -> None:
        needs = _job("build-and-publish")["needs"]
        assert "test" in needs
        assert "type-check" in needs

    def test_uses_pypi_environment(self) -> None:
        assert _job("build-and-publish")["environment"] == "pypi"


class TestArtifactReuse:
    """The dist built in publish-testpypi must be the exact bytes promoted to PyPI."""

    def test_publish_testpypi_uploads_release_dist(self) -> None:
        step = _step_using("publish-testpypi", "actions/upload-artifact")
        assert step["with"]["name"] == "release-dist"

    def test_build_and_publish_downloads_release_dist(self) -> None:
        step = _step_using("build-and-publish", "actions/download-artifact")
        assert step["with"]["name"] == "release-dist"

    def test_build_and_publish_does_not_rebuild(self) -> None:
        """A duplicate build step would defeat byte-identical reuse."""
        names = [s.get("name", "") for s in _steps("build-and-publish")]
        assert not any(name == "Build package" for name in names)


class TestTestPyPIPublishStrictness:
    """The staging gate must test THIS run's artifact, not a stale one."""

    def test_publishes_to_testpypi_index(self) -> None:
        step = _step_using("publish-testpypi", "gh-action-pypi-publish")
        assert step["with"]["repository-url"] == "https://test.pypi.org/legacy/"

    def test_no_skip_existing_on_testpypi_publish(self) -> None:
        """Guard against re-testing a stale artifact instead of this run's build.

        skip-existing would let a re-dispatch smoke-test a stale TestPyPI
        artifact instead of the freshly built dist, breaking the staging-gate
        guarantee that what is smoke-tested is what gets promoted.
        """
        step = _step_using("publish-testpypi", "gh-action-pypi-publish")
        assert "skip-existing" not in step.get("with", {})

    def test_production_publish_remains_strict(self) -> None:
        step = _step_using("build-and-publish", "gh-action-pypi-publish")
        assert "skip-existing" not in step.get("with", {})
        assert "repository-url" not in step.get("with", {})


class TestSmokeInstallStep:
    """The smoke-install must retry for propagation and use pixi, not ambient python."""

    def _smoke_step(self) -> dict[str, Any]:
        steps = _steps("publish-testpypi")
        matches = [s for s in steps if s.get("name") == "Smoke-install from TestPyPI"]
        assert len(matches) == 1
        return matches[0]

    def test_uses_pixi_run_python(self) -> None:
        run = self._smoke_step()["run"]
        assert "pixi run python -m venv" in run

    def test_retries_on_propagation_delay(self) -> None:
        run = self._smoke_step()["run"]
        assert "ATTEMPTS=10" in run
        assert "SLEEP_SECONDS=30" in run
        assert "no matching distribution" in run.lower()

    def test_fails_hard_on_non_propagation_error(self) -> None:
        run = self._smoke_step()["run"]
        assert "not retrying" in run

    def test_verifies_installed_version_matches_tag(self) -> None:
        run = self._smoke_step()["run"]
        assert "import hephaestus; print(hephaestus.__version__)" in run
        assert "version mismatch" in run.lower()
