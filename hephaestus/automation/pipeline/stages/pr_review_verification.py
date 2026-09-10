"""Host-owned verification plans and receipt checks for PR review."""

from __future__ import annotations

import re
from dataclasses import dataclass

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

_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", flags=re.MULTILINE)


def _changed_new_side_paths(pr_diff: str) -> frozenset[str]:
    """Return non-deleted changed paths from each diff's new-file side."""
    paths: set[str] = set()
    pending_header_path: str | None = None

    def flush_pending_header_path() -> None:
        nonlocal pending_header_path
        if pending_header_path is not None:
            paths.add(pending_header_path)
            pending_header_path = None

    for raw_line in pr_diff.splitlines():
        header = _DIFF_GIT_HEADER_RE.match(raw_line)
        if header:
            flush_pending_header_path()
            pending_header_path = header.group(2)
            continue

        if raw_line.startswith("+++ ") and pending_header_path is not None:
            target = raw_line[4:].strip()
            pending_header_path = None
            if target == "/dev/null":
                continue
            paths.add(target[2:] if target.startswith("b/") else target)

    flush_pending_header_path()
    return frozenset(paths)


def _changed_unit_pytest_argv(target: str) -> tuple[str, ...]:
    """Return the changed-unit pytest command while preserving host exclusions."""
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


def _host_verification_specs(pr_diff: object, *, profile: str | None = "hephaestus") -> _HostPlan:
    """Return the complete fixed host plan activated by the verified diff."""
    if profile != "hephaestus" or not isinstance(pr_diff, str):
        return ()
    changed_paths = {match.group(2) for match in _DIFF_GIT_HEADER_RE.finditer(pr_diff)}
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
    changed_new_side_paths = _changed_new_side_paths(pr_diff)
    changed_unit_paths = tuple(
        sorted(
            path
            for path in changed_new_side_paths
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
    'HOST_VERIFICATION_DIAGNOSTIC_MAX', 'HOST_VERIFICATION_TIMEOUT_S', '_DIFF_GIT_HEADER_RE',
    '_NONHERMETIC_HOST_UNIT_TEST_PATHS', '_PATH_HOST_VERIFICATION_SPECS',
    '_PYTHON_HOST_VERIFICATION_SPECS', '_PYTHON_VALIDATION_CONFIG_PATHS', '_HostPlan',
    '_HostVerificationSpec',
    '_changed_new_side_paths', '_changed_unit_pytest_argv', '_host_verification_specs',
    'annotations', 'dataclass', 're']
# fmt: on
