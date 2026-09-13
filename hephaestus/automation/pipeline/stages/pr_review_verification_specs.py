"""Fixed host-owned verification specifications for PR review."""

from __future__ import annotations

from .pr_review_verification_paths import (
    _PATH_HOST_VERIFICATION_SPECS as _PATH_HOST_VERIFICATION_SPECS,
    _HostVerificationSpec as _HostVerificationSpec,
)

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
__all__ = [
    "_FULL_UNIT_COVERAGE_SPEC",
    "_NONHERMETIC_HOST_UNIT_TEST_PATHS",
    "_PATH_HOST_VERIFICATION_SPECS",
    "_PYTHON_HOST_VERIFICATION_SPECS",
    "_PYTHON_VALIDATION_CONFIG_PATHS",
    "_HostPlan",
    "_HostVerificationSpec",
]
