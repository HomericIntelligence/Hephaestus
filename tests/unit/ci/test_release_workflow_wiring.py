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
RELEASE_JOBS = (
    "test",
    "type-check",
    "publish-testpypi",
    "build-and-publish",
    "deploy-docs",
)


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


def _needs(job_name: str) -> list[str]:
    needs = _job(job_name)["needs"]
    return [needs] if isinstance(needs, str) else needs


class TestPublishTestPyPIJobExists:
    """The staging job must exist and run before build-and-publish."""

    def test_job_present(self) -> None:
        assert "publish-testpypi" in _load_workflow()["jobs"]

    def test_needs_resolver_test_and_type_check(self) -> None:
        assert _job("publish-testpypi")["needs"] == [
            "resolve-release",
            "test",
            "type-check",
        ]

    def test_uses_testpypi_environment(self) -> None:
        assert _job("publish-testpypi")["environment"] == "testpypi"


class TestBuildAndPublishDependsOnTestPyPI:
    """Production publish must not proceed until the TestPyPI gate passes."""

    def test_needs_includes_publish_testpypi(self) -> None:
        assert "publish-testpypi" in _job("build-and-publish")["needs"]

    def test_still_needs_resolver_test_and_type_check(self) -> None:
        needs = _job("build-and-publish")["needs"]
        assert "resolve-release" in needs
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
    """The smoke-install must retry for propagation in a UV-created environment."""

    def _smoke_step(self) -> dict[str, Any]:
        steps = _steps("publish-testpypi")
        matches = [s for s in steps if s.get("name") == "Smoke-install from TestPyPI"]
        assert len(matches) == 1
        return matches[0]

    def test_uses_uv_to_create_the_smoke_environment(self) -> None:
        run = self._smoke_step()["run"]
        assert "uv venv /tmp/testpypi-smoke" in run

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


class TestImmutableReleaseRef:
    """Every release operation must consume one centrally resolved tag commit."""

    def _resolve_step(self) -> dict[str, Any]:
        matches = [step for step in _steps("resolve-release") if step.get("id") == "resolve"]
        assert len(matches) == 1
        return matches[0]

    def test_resolver_exports_one_tag_and_sha(self) -> None:
        assert _job("resolve-release")["outputs"] == {
            "tag": "${{ steps.resolve.outputs.tag }}",
            "sha": "${{ steps.resolve.outputs.sha }}",
        }

    def test_dispatch_input_has_priority_and_resolves_exact_tag_commit(self) -> None:
        step = self._resolve_step()
        run = step["run"]

        assert step["env"]["INPUT_TAG"] == "${{ inputs.tag }}"
        assert run.index('if [ -n "$INPUT_TAG" ]') < run.index(
            'elif [[ "$EVENT_REF" == refs/tags/* ]]'
        )
        assert 'git rev-parse --verify "refs/tags/${TAG}^{commit}"' in run
        assert 'echo "sha=${SHA}" >> "$GITHUB_OUTPUT"' in run

    def test_resolver_fails_closed_for_missing_or_noncommit_tag(self) -> None:
        run = self._resolve_step()["run"]

        assert "git for-each-ref" in run
        assert 'if ! SHA="$(git rev-parse --verify "refs/tags/${TAG}^{commit}")"; then' in run
        assert "::error::No release tag could be resolved" in run
        assert "::error::Release tag ${TAG} does not resolve to a commit" in run

    def test_every_release_job_checks_out_resolved_sha(self) -> None:
        expected_ref = "${{ needs.resolve-release.outputs.sha }}"

        for job_name in RELEASE_JOBS:
            assert "resolve-release" in _needs(job_name)
            checkout = _step_using(job_name, "actions/checkout")
            assert checkout["with"]["ref"] == expected_ref

    def test_downstream_jobs_do_not_reresolve_or_checkout_the_tag(self) -> None:
        for job_name in RELEASE_JOBS:
            steps = _steps(job_name)
            scripts = "\n".join(str(step.get("run", "")) for step in steps)

            assert all(step.get("name") != "Resolve tag" for step in steps)
            for command in (
                "git tag --list",
                "git for-each-ref",
                "git rev-parse",
                "git checkout",
            ):
                assert command not in scripts

            job_text = yaml.safe_dump(_job(job_name), sort_keys=False)
            assert "${{ inputs.tag }}" not in job_text
            assert "${{ github.ref }}" not in job_text

    def test_downstream_tag_consumers_use_resolver_output(self) -> None:
        workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        assert "steps.tag.outputs.tag" not in workflow_text
        assert "${{ needs.resolve-release.outputs.tag }}" in workflow_text
