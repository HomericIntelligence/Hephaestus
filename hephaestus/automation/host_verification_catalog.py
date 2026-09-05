"""Closed host-verification commands for immutable PR review checkouts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class HostVerificationSpec:
    """One fixed command that the host verifier can execute."""

    descr: str
    argv: tuple[str, ...]
    changed_path: str | None = None

    @property
    def command_id(self) -> str:
        """Return the stable catalog identifier for this command."""
        return self.descr


HostVerificationPlan = tuple[HostVerificationSpec, ...]


_PYTHON_VALIDATION_CONFIG_PATHS = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "coverage.toml",
        "mypy.ini",
        "pytest.ini",
        "ruff.toml",
        "setup.cfg",
        "tox.ini",
    }
)

HOST_VERIFICATION_CATALOG: HostVerificationPlan = (
    HostVerificationSpec(
        descr="review_python_ruff_check",
        argv=(
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "ruff",
            "check",
            "hephaestus/",
            "tests/",
        ),
    ),
    HostVerificationSpec(
        descr="review_python_ruff_format",
        argv=(
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "ruff",
            "format",
            "--check",
            "hephaestus/",
            "tests/",
        ),
    ),
    HostVerificationSpec(
        descr="review_python_mypy",
        argv=(
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "mypy",
            "--cache-dir=../scratch/cache/mypy",
            "hephaestus/",
            "scripts/",
            "tests/",
        ),
    ),
    HostVerificationSpec(
        descr="review_python_unit_suite",
        argv=(
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit",
            "--ignore=tests/unit/automation/pipeline/test_worker_pool.py",
            "--strict-markers",
            "-m",
            "not nightly and not linux_host_verification",
            "-q",
            "--tb=short",
        ),
    ),
    HostVerificationSpec(
        descr="review_full_unit_coverage",
        changed_path="coverage.toml",
        argv=(
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "python",
            "-m",
            "hephaestus.automation.host_coverage",
        ),
    ),
    HostVerificationSpec(
        descr="review_migration_version_currency",
        changed_path="docs/MIGRATION.md",
        argv=(
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/docs/test_version_currency.py",
            "-q",
            "--tb=short",
        ),
    ),
    HostVerificationSpec(
        descr="review_worker_pool_agent_execution_error",
        changed_path="tests/unit/automation/pipeline/test_worker_pool.py",
        argv=(
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/automation/pipeline/test_worker_pool.py::"
            "TestAgentErrorHandling::test_codex_event_failure_is_explicit_agent_error",
            "-q",
            "--tb=short",
        ),
    ),
    HostVerificationSpec(
        descr="review_stalled_consumer_verification",
        changed_path="tests/performance/test_worker_pool_load.py",
        argv=(
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "pytest",
            "-o",
            "addopts=",
            "tests/performance/test_worker_pool_load.py",
            "-q",
            "--load-report=../scratch/outputs/worker-pool.json",
        ),
    ),
)

_CATALOG_BY_ID = {spec.descr: spec for spec in HOST_VERIFICATION_CATALOG}
_PYTHON_COMMAND_IDS = tuple(spec.descr for spec in HOST_VERIFICATION_CATALOG[:4])

# The build image includes each regular source file under this prefix. The
# exact path set provides the other, non-directory build inputs.
BUILD_CONTEXT_EXACT_PATHS = frozenset(
    {
        ".pre-commit-config.yaml",
        "README.md",
        "ci/Containerfile",
        "ci/linux_host_verify",
        "pyproject.toml",
        "uv.lock",
    }
)
BUILD_CONTEXT_DIRECTORY_PREFIXES = ("hephaestus/",)

CONTRACT_DIGEST_PATHS = frozenset(
    {
        "ci/Containerfile",
        "ci/linux_host_verify",
        "ci/linux_host_verify_allocation",
        "hephaestus/automation/host_verification_catalog.py",
        "hephaestus/automation/linux_host_verification.py",
        "hephaestus/automation/linux_host_verification_contract.py",
        "hephaestus/automation/pipeline/stages/pr_review_jobs.py",
        "hephaestus/automation/pipeline/stages/pr_review_threads.py",
        "hephaestus/automation/pipeline/stages/pr_review_verification.py",
        "hephaestus/automation/pipeline/worker_pool.py",
        "pyproject.toml",
        "uv.lock",
    }
)


def _normalized_changed_paths(review_changed_paths: object) -> frozenset[str]:
    """Return checkout-derived paths, or no paths when their type is invalid."""
    if not isinstance(review_changed_paths, (list, tuple)):
        return frozenset()
    if not all(
        isinstance(path, str) and path and "\x00" not in path for path in review_changed_paths
    ):
        return frozenset()
    return frozenset(review_changed_paths)


def select_host_verification_specs(review_changed_paths: object) -> HostVerificationPlan:
    """Select fixed commands from checkout-derived old and new changed paths."""
    changed_paths = _normalized_changed_paths(review_changed_paths)
    selected_ids: list[str] = []
    if any(
        path.endswith(".py") or path in _PYTHON_VALIDATION_CONFIG_PATHS for path in changed_paths
    ):
        selected_ids.extend(_PYTHON_COMMAND_IDS)
    if changed_paths & {"coverage.toml", "pyproject.toml"}:
        selected_ids.append("review_full_unit_coverage")
    if "docs/MIGRATION.md" in changed_paths:
        selected_ids.append("review_migration_version_currency")
    if "tests/unit/automation/pipeline/test_worker_pool.py" in changed_paths:
        selected_ids.append("review_worker_pool_agent_execution_error")
    if "tests/performance/test_worker_pool_load.py" in changed_paths:
        selected_ids.append("review_stalled_consumer_verification")
    return tuple(_CATALOG_BY_ID[command_id] for command_id in selected_ids)


def resolve_host_verification_spec(
    command_id: object, argv: Sequence[object]
) -> HostVerificationSpec:
    """Resolve one requested command and reject an unknown or altered argv."""
    if not isinstance(command_id, str) or command_id not in _CATALOG_BY_ID:
        raise ValueError("unknown host verification command")
    if not all(isinstance(argument, str) for argument in argv):
        raise ValueError("host verification argv must contain only strings")
    spec = _CATALOG_BY_ID[command_id]
    if tuple(argv) != spec.argv:
        raise ValueError("host verification command does not match catalog argv")
    return spec


def is_build_context_path(path: str) -> bool:
    """Return whether one logical path is a sealed image build input."""
    return path in BUILD_CONTEXT_EXACT_PATHS or path.startswith(BUILD_CONTEXT_DIRECTORY_PREFIXES)


def _host_verification_specs(
    review_changed_paths: object, *, profile: str | None = "hephaestus"
) -> HostVerificationPlan:
    """Return the Hephaestus plan from only checkout-derived changed paths."""
    if profile != "hephaestus":
        return ()
    return select_host_verification_specs(review_changed_paths)


# Compatibility names keep stage consumers stable while the selector moves to
# the repository-owned catalog.
_HostVerificationSpec = HostVerificationSpec
_HostPlan = HostVerificationPlan
_PYTHON_HOST_VERIFICATION_SPECS = HOST_VERIFICATION_CATALOG[:4]
_FULL_UNIT_COVERAGE_SPEC = _CATALOG_BY_ID["review_full_unit_coverage"]
_PATH_HOST_VERIFICATION_SPECS = HOST_VERIFICATION_CATALOG[5:]
_NONHERMETIC_HOST_UNIT_TEST_PATHS = frozenset(
    {"tests/unit/automation/pipeline/test_worker_pool.py"}
)


__all__ = [
    "BUILD_CONTEXT_DIRECTORY_PREFIXES",
    "BUILD_CONTEXT_EXACT_PATHS",
    "CONTRACT_DIGEST_PATHS",
    "HOST_VERIFICATION_CATALOG",
    "_FULL_UNIT_COVERAGE_SPEC",
    "_NONHERMETIC_HOST_UNIT_TEST_PATHS",
    "_PATH_HOST_VERIFICATION_SPECS",
    "_PYTHON_HOST_VERIFICATION_SPECS",
    "_PYTHON_VALIDATION_CONFIG_PATHS",
    "HostVerificationPlan",
    "HostVerificationSpec",
    "_HostPlan",
    "_HostVerificationSpec",
    "_host_verification_specs",
    "is_build_context_path",
    "resolve_host_verification_spec",
    "select_host_verification_specs",
]
