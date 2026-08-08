"""Host-owned verification plans and receipt checks for PR review."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _HostVerificationSpec:
    """One repository-owned verification command eligible for PR review."""

    changed_path: str | None
    argv: tuple[str, ...]
    descr: str


# Python reviews run read-only by design. The host therefore performs the
# deterministic static validation against an immutable snapshot before the
# reviewer sees it. Changed unit tests are added separately below; broader
# suites can legitimately require host capabilities denied to the reviewer.
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
    argv=(
        "/bin/sh",
        "-c",
        "set -e; uv run pytest tests/unit --override-ini=addopts= -v --strict-markers -m "
        "'not nightly' --cov=hephaestus --cov-report=term-missing --cov-report=xml && "
        "uv run hephaestus-check-coverage --coverage-file coverage.xml --config coverage.toml",
    ),
    descr="review_full_unit_coverage",
)
# This suite exercises the host verifier's own disk-image and sandbox
# primitives. Running it inside that verifier would require nested mounts and
# produces runner failures rather than meaningful code evidence.
_NONHERMETIC_HOST_UNIT_TEST_PATHS = frozenset(
    {"tests/unit/automation/pipeline/test_worker_pool.py"}
)
# Some Python regressions require additional bounded execution beyond the
# baseline review plan. Their path trigger is derived only from a real Git
# diff header, never from reviewer or GitHub prose.
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


def _host_verification_specs(pr_diff: object) -> tuple[_HostVerificationSpec, ...]:
    """Return the complete fixed host plan activated by the verified diff."""
    if not isinstance(pr_diff, str):
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
    '_PYTHON_HOST_VERIFICATION_SPECS', '_PYTHON_VALIDATION_CONFIG_PATHS', '_HostVerificationSpec',
    '_changed_new_side_paths', '_changed_unit_pytest_argv', '_host_verification_specs',
    'annotations', 'dataclass', 're']
# fmt: on
