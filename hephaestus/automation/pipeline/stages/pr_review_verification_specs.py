"""Fixed host-owned verification specifications for PR review."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _HostVerificationSpec:
    """One repository-owned verification command eligible for PR review."""

    changed_path: str | None
    argv: tuple[str, ...]
    descr: str


_HostPlan = tuple[_HostVerificationSpec, ...]

_PYTHON_HOST_VERIFICATION_SPECS: tuple[_HostVerificationSpec, ...] = (
    _HostVerificationSpec(
        changed_path=None,
        argv=("uv", "run", "ruff", "check", "hephaestus/", "tests/"),
        descr="review_python_ruff_check",
    ),
    _HostVerificationSpec(
        changed_path=None,
        argv=("uv", "run", "ruff", "format", "--check", "hephaestus/", "tests/"),
        descr="review_python_ruff_format",
    ),
    _HostVerificationSpec(
        changed_path=None,
        argv=(
            "uv",
            "run",
            "mypy",
            "--cache-dir=/dev/null",
            "hephaestus/",
            "scripts/",
            "tests/",
        ),
        descr="review_python_mypy",
    ),
)
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
_FULL_UNIT_COVERAGE_SPEC = _HostVerificationSpec(
    changed_path="coverage.toml",
    argv=("uv", "run", "python", "-m", "hephaestus.automation.host_coverage"),
    descr="review_full_unit_coverage",
)
# The host verifier cannot run its disk-image and sandbox tests inside itself.
_NONHERMETIC_HOST_UNIT_TEST_PATHS = frozenset(
    {"tests/unit/automation/pipeline/test_worker_pool.py"}
)
_PATH_HOST_VERIFICATION_SPECS: tuple[_HostVerificationSpec, ...] = (
    _HostVerificationSpec(
        changed_path="docs/MIGRATION.md",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/docs/test_version_currency.py",
            "-q",
            "--tb=short",
        ),
        descr="review_migration_version_currency",
    ),
    _HostVerificationSpec(
        changed_path="tests/unit/automation/pipeline/test_worker_pool.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/automation/pipeline/test_worker_pool.py::TestAgentErrorHandling::test_codex_event_failure_is_explicit_agent_error",
            "-q",
            "--tb=short",
        ),
        descr="review_worker_pool_agent_execution_error",
    ),
    _HostVerificationSpec(
        changed_path="tests/unit/automation/pipeline/test_worker_pool.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/automation/pipeline/test_worker_pool.py::TestHostVerificationGitExecPath::test_active_sandbox_git_reads_validated_system_config",
            "-q",
            "--tb=short",
        ),
        descr="review_worker_pool_git_exec_path",
    ),
    _HostVerificationSpec(
        changed_path="tests/performance/test_worker_pool_load.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/performance/test_worker_pool_load.py",
            "-q",
            "--load-report=../scratch/outputs/worker-pool.json",
        ),
        descr="review_stalled_consumer_verification",
    ),
)

__all__ = [
    "_FULL_UNIT_COVERAGE_SPEC",
    "_NONHERMETIC_HOST_UNIT_TEST_PATHS",
    "_PATH_HOST_VERIFICATION_SPECS",
    "_PYTHON_HOST_VERIFICATION_SPECS",
    "_PYTHON_VALIDATION_CONFIG_PATHS",
    "_HostPlan",
    "_HostVerificationSpec",
]
