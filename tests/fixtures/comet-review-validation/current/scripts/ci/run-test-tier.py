#!/usr/bin/env python3
"""Run one test tier and fail closed."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any, TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = REPOSITORY_ROOT / "tests"
DEFAULT_INVENTORY = TEST_ROOT / "test-tiers.yaml"
PROFILES = ("pr", "nightly", "promotion")
GIT_REPOSITORY_VARIABLES = frozenset(
    [
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    ]
)


class TierConfigurationError(ValueError):
    """Report an invalid test-tier configuration."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Load safe YAML and reject duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    """Construct one YAML mapping with unique keys."""
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise TierConfigurationError("YAML mapping keys must be scalar values.") from error
        if duplicate:
            raise TierConfigurationError(f"duplicate YAML mapping key: {key!r}.")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class TestCountPlugin:
    """Store the number of tests that pytest collects."""

    def __init__(self) -> None:
        """Initialize an empty test count."""
        self.test_count = 0

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Store the final collection count."""
        self.test_count = len(session.items)


def _validate_module_path(repository_root: Path, value: object) -> str:
    if not isinstance(value, str):
        raise TierConfigurationError("Each nightly module path must be a string.")

    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != "tests"
        or path.name == ""
        or not path.name.startswith("test_")
        or path.suffix != ".py"
        or ".." in path.parts
    ):
        raise TierConfigurationError(f"The nightly module path is not valid: {value}.")

    resolved = repository_root / path
    if resolved.is_symlink() or not resolved.is_file():
        raise TierConfigurationError(f"The nightly module does not exist: {value}.")
    return path.as_posix()


def load_inventory(path: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict[str, Any]:
    """Load and validate the test-tier inventory."""
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as error:
        raise TierConfigurationError(
            f"The runner cannot read the test-tier inventory: {error}"
        ) from error

    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "baseline",
        "pr_modules",
        "nightly_modules",
    }:
        raise TierConfigurationError("The test-tier inventory has invalid top-level fields.")
    if raw["version"] != 1:
        raise TierConfigurationError("The test-tier inventory version must be 1.")
    if not isinstance(raw["baseline"], dict):
        raise TierConfigurationError("The baseline field must be a mapping.")

    validated_lists: dict[str, tuple[str, ...]] = {}
    for name in ("pr_modules", "nightly_modules"):
        modules = raw[name]
        if not isinstance(modules, list) or not modules:
            raise TierConfigurationError(f"The {name} list must not be empty.")
        validated = [_validate_module_path(repository_root, item) for item in modules]
        if len(validated) != len(set(validated)):
            raise TierConfigurationError(f"The {name} list contains a duplicate path.")
        validated_lists[name] = tuple(sorted(validated))

    overlap = sorted(set(validated_lists["pr_modules"]) & set(validated_lists["nightly_modules"]))
    if overlap:
        raise TierConfigurationError("The test-tier lists overlap: " + ", ".join(overlap))

    root_modules, _ = discover_modules(repository_root=repository_root)
    classified = set(validated_lists["pr_modules"]) | set(validated_lists["nightly_modules"])
    if classified != set(root_modules):
        missing = sorted(set(root_modules) - classified)
        stale = sorted(classified - set(root_modules))
        details = []
        if missing:
            details.append("unclassified modules: " + ", ".join(missing))
        if stale:
            details.append("unknown modules: " + ", ".join(stale))
        raise TierConfigurationError(
            "The test-tier lists do not partition root modules: " + "; ".join(details)
        )

    result = dict(raw)
    result.update(validated_lists)
    return result


def discover_modules(
    *, repository_root: Path = REPOSITORY_ROOT
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Discover root and PostgreSQL test modules."""
    test_root = repository_root / "tests"
    root_modules = tuple(
        path.relative_to(repository_root).as_posix()
        for path in sorted(test_root.glob("test_*.py"))
        if path.is_file() and not path.is_symlink()
    )
    postgres_modules = tuple(
        path.relative_to(repository_root).as_posix()
        for path in sorted((test_root / "postgres").glob("test_*.py"))
        if path.is_file() and not path.is_symlink()
    )
    return root_modules, postgres_modules


def select_modules(
    profile: str,
    inventory: dict[str, Any],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> tuple[str, ...]:
    """Select modules for one profile."""
    if profile not in PROFILES:
        raise TierConfigurationError(f"The test profile is not valid: {profile}.")

    root_modules, _ = discover_modules(repository_root=repository_root)
    nightly_modules = tuple(inventory["nightly_modules"])
    pr_modules = tuple(inventory["pr_modules"])

    if profile == "nightly":
        selected = nightly_modules
    elif profile == "promotion":
        selected = root_modules
    else:
        selected = pr_modules

    if not selected:
        raise TierConfigurationError(f"The {profile} profile selected no test modules.")
    return selected


def _git_repository_environment() -> dict[str, str]:
    """Get Git variables for the caller repository."""
    return {
        name: value
        for name, value in os.environ.items()
        if name in GIT_REPOSITORY_VARIABLES
        or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }


def run_profile(
    profile: str,
    *,
    pytest_arguments: Sequence[str] = (),
    inventory_path: Path = DEFAULT_INVENTORY,
    repository_root: Path = REPOSITORY_ROOT,
    pytest_main: Callable[..., int | pytest.ExitCode] = pytest.main,
    monotonic: Callable[[], float] = time.monotonic,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run a profile and return a process exit code."""
    inventory = load_inventory(inventory_path, repository_root=repository_root)
    modules = select_modules(profile, inventory, repository_root=repository_root)
    arguments = [*modules, *pytest_arguments]
    if not any(
        argument == "--durations" or argument.startswith("--durations=") for argument in arguments
    ):
        arguments.append("--durations=20")

    plugin = TestCountPlugin()
    started = monotonic()
    git_environment = _git_repository_environment()
    try:
        for name in git_environment:
            del os.environ[name]
        result = int(pytest_main(arguments, plugins=[plugin]))
    finally:
        for name in _git_repository_environment():
            del os.environ[name]
        os.environ.update(git_environment)
    duration = monotonic() - started
    print(
        f"profile={profile} modules={len(modules)} tests={plugin.test_count} "
        f"duration_seconds={duration:.3f}",
        file=stdout,
    )

    if result != 0:
        print(f"pytest failed with exit code {result}.", file=stderr)
        return result
    if plugin.test_count <= 0:
        print("pytest collected no tests.", file=stderr)
        return int(pytest.ExitCode.NO_TESTS_COLLECTED)
    return 0


def _parse_arguments(arguments: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    runner_arguments = arguments[:separator]
    pytest_arguments = list(arguments[separator + 1 :]) if separator < len(arguments) else []

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES)
    parsed = parser.parse_args(runner_arguments)
    return parsed, pytest_arguments


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    parsed, pytest_arguments = _parse_arguments(sys.argv[1:] if arguments is None else arguments)
    try:
        return run_profile(
            parsed.profile,
            pytest_arguments=pytest_arguments,
        )
    except TierConfigurationError as error:
        print(f"The test-tier configuration has an error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
