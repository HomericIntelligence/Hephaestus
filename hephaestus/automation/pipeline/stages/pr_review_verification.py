"""Host-owned verification plans and receipt checks for PR review."""

from __future__ import annotations

import os
from pathlib import Path

from .pr_review_verification_specs import (
    _FULL_UNIT_COVERAGE_SPEC as _FULL_UNIT_COVERAGE_SPEC,
    _NONHERMETIC_HOST_UNIT_TEST_PATHS as _NONHERMETIC_HOST_UNIT_TEST_PATHS,
    _PATH_HOST_VERIFICATION_SPECS as _PATH_HOST_VERIFICATION_SPECS,
    _PYTHON_HOST_VERIFICATION_SPECS as _PYTHON_HOST_VERIFICATION_SPECS,
    _PYTHON_VALIDATION_CONFIG_PATHS as _PYTHON_VALIDATION_CONFIG_PATHS,
    _HostPlan as _HostPlan,
    _HostVerificationSpec as _HostVerificationSpec,
)

HOST_VERIFICATION_TIMEOUT_S = 300
HOST_VERIFICATION_DIAGNOSTIC_MAX = 4_000
_REVIEW_CHANGED_PATH_MAX = 512
_REVIEW_CHANGED_PATH_BYTES_MAX = 64 * 1024


def _review_changed_paths(value: object) -> tuple[str, ...] | None:
    """Return bounded safe paths, or return None for invalid input."""
    if not isinstance(value, (list, tuple)) or len(value) > _REVIEW_CHANGED_PATH_MAX:
        return None
    paths: list[str] = []
    encoded_bytes = 0
    for path in value:
        if not isinstance(path, str):
            return None
        relative = Path(path)
        if (
            not path
            or "\x00" in path
            or relative.is_absolute()
            or relative.as_posix() != path
            or any(component in {"", ".", ".."} for component in relative.parts)
        ):
            return None
        encoded_bytes += len(os.fsencode(path)) + 1
        if encoded_bytes > _REVIEW_CHANGED_PATH_BYTES_MAX:
            return None
        paths.append(path)
    if len(set(paths)) != len(paths):
        return None
    return tuple(paths)


def _changed_unit_pytest_argv(target: str) -> tuple[str, ...]:
    """Return a focused unit-test command with host exclusions."""
    ignore_args = tuple(
        f"--ignore={path}"
        for path in sorted(_NONHERMETIC_HOST_UNIT_TEST_PATHS)
        if path.startswith(f"{target}/")
    )
    return (
        "uv",
        "run",
        "pytest",
        "-o",
        "addopts=",
        target,
        *ignore_args,
        "-q",
        "--tb=short",
    )


def _host_verification_specs(
    review_changed_paths: object,
    *,
    existing_changed_paths: object | None = None,
    profile: str | None = "hephaestus",
) -> _HostPlan:
    """Return the fixed host plan for checkout-derived changed paths."""
    normalized_paths = _review_changed_paths(review_changed_paths)
    if profile != "hephaestus" or normalized_paths is None:
        return ()
    changed_paths = frozenset(normalized_paths)
    normalized_existing_paths = _review_changed_paths(
        normalized_paths if existing_changed_paths is None else existing_changed_paths
    )
    if normalized_existing_paths is None or not set(normalized_existing_paths) <= changed_paths:
        return ()
    path_triggered_specs = tuple(
        spec for spec in _PATH_HOST_VERIFICATION_SPECS if spec.changed_path in changed_paths
    )
    coverage_specs = (
        (_FULL_UNIT_COVERAGE_SPEC,) if changed_paths & {"coverage.toml", "pyproject.toml"} else ()
    )
    if not any(
        path.endswith(".py") or path in _PYTHON_VALIDATION_CONFIG_PATHS for path in changed_paths
    ):
        return path_triggered_specs
    changed_unit_paths = tuple(
        sorted(
            path
            for path in normalized_existing_paths
            if path.startswith("tests/unit/")
            and path.endswith(".py")
            and path not in _NONHERMETIC_HOST_UNIT_TEST_PATHS
        )
    )
    changed_conftest_paths = tuple(
        (path.rsplit("/", 1)[0], path)
        for path in changed_unit_paths
        if path.rsplit("/", 1)[-1] == "conftest.py"
    )
    changed_conftest_directories = {
        directory: path
        for directory, path in changed_conftest_paths
        if not any(
            directory.startswith(f"{other_directory}/")
            for other_directory, _ in changed_conftest_paths
        )
    }
    changed_unit_targets = (
        *((path, directory) for directory, path in sorted(changed_conftest_directories.items())),
        *(
            (path, path)
            for path in changed_unit_paths
            if path.rsplit("/", 1)[-1] != "conftest.py"
            and not any(
                path.startswith(f"{directory}/") for directory in changed_conftest_directories
            )
        ),
    )
    changed_unit_tests = tuple(
        _HostVerificationSpec(
            changed_path=changed_path,
            argv=_changed_unit_pytest_argv(target),
            descr=f"review_changed_unit_test_{index}",
        )
        for index, (changed_path, target) in enumerate(changed_unit_targets)
    )
    return (
        *_PYTHON_HOST_VERIFICATION_SPECS,
        *changed_unit_tests,
        *path_triggered_specs,
        *coverage_specs,
    )


# fmt: off
__all__ = [
    'HOST_VERIFICATION_DIAGNOSTIC_MAX', 'HOST_VERIFICATION_TIMEOUT_S',
    '_NONHERMETIC_HOST_UNIT_TEST_PATHS', '_PATH_HOST_VERIFICATION_SPECS',
    '_PYTHON_HOST_VERIFICATION_SPECS', '_PYTHON_VALIDATION_CONFIG_PATHS', '_HostPlan',
    '_HostVerificationSpec', '_changed_unit_pytest_argv', '_host_verification_specs',
    '_review_changed_paths']
# fmt: on
