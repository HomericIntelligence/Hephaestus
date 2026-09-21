#!/usr/bin/env python3
"""Run one test tier and fail closed."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
import yaml
import yaml.resolver

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any, TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = REPOSITORY_ROOT / "tests"
DEFAULT_INVENTORY = TEST_ROOT / "test-tiers.yaml"
DEFAULT_DURATIONS = TEST_ROOT / "test-durations.json"
DEFAULT_TEST_SECONDS = 0.1
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


class TestShardPlugin:
    """Select one duration-balanced partition of the collected tests."""

    def __init__(self, shard_index: int, shard_count: int, durations: dict[str, float]) -> None:
        """Bind the shard identity and timing weights."""
        self.shard_index = shard_index
        self.shard_count = shard_count
        self.durations = durations

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        """Keep collected order and report the tests assigned to other shards."""
        selected = set(
            shard_nodes(
                [item.nodeid for item in items],
                shard_index=self.shard_index,
                shard_count=self.shard_count,
                durations=self.durations,
            )
        )
        deselected = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        config.hook.pytest_deselected(items=deselected)


def load_durations(path: Path = DEFAULT_DURATIONS) -> dict[str, float]:
    """Read positive timing weights without selecting tests."""

    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError("The timing document has a duplicate key.")
            fields[key] = value
        return fields

    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
        durations = document["durations"]
        if (
            document["version"] != 1
            or document["default_seconds"] != DEFAULT_TEST_SECONDS
            or not isinstance(durations, dict)
        ):
            raise ValueError("The timing document has invalid fields.")
        for node, duration in durations.items():
            if (
                not isinstance(node, str)
                or not node.startswith("tests/")
                or "::" not in node
                or isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError("The timing document has an invalid weight.")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise TierConfigurationError(f"The runner cannot read test durations: {error}") from error
    return durations


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


def shard_nodes(
    nodes: Sequence[str],
    *,
    shard_index: int,
    shard_count: int,
    durations: dict[str, float],
) -> tuple[str, ...]:
    """Assign exact node IDs to deterministic, duration-balanced shards."""
    _validate_shard(shard_index, shard_count)
    if len(nodes) != len(set(nodes)):
        raise pytest.UsageError("The collected tests have duplicate node IDs.")
    assignments: list[list[str]] = [[] for _ in range(shard_count)]
    loads = [0.0] * shard_count
    for node in sorted(nodes, key=lambda item: (-durations.get(item, DEFAULT_TEST_SECONDS), item)):
        target = min(range(shard_count), key=lambda item: (loads[item], item))
        assignments[target].append(node)
        loads[target] += durations.get(node, DEFAULT_TEST_SECONDS)
    selected = tuple(sorted(assignments[shard_index]))
    if not selected:
        raise pytest.UsageError("The full-profile shard has no tests.")
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
    shard_index: int = 0,
    shard_count: int = 1,
) -> int:
    """Run a profile and return a process exit code."""
    inventory = load_inventory(inventory_path, repository_root=repository_root)
    modules = select_modules(profile, inventory, repository_root=repository_root)
    _validate_shard(shard_index, shard_count)
    if shard_count != 1:
        print(
            json.dumps(
                {
                    "profile": profile,
                    "shard_index": shard_index,
                    "shard_count": shard_count,
                    "modules": modules,
                }
            ),
            file=stdout,
        )
    arguments = [*modules, *pytest_arguments]
    if not any(
        argument == "--durations" or argument.startswith("--durations=") for argument in arguments
    ):
        arguments.append("--durations=20")

    plugin = TestCountPlugin()
    plugins: list[object] = [plugin]
    if shard_count != 1:
        plugins.append(TestShardPlugin(shard_index, shard_count, load_durations()))
    started = monotonic()
    git_environment = _git_repository_environment()
    try:
        for name in git_environment:
            del os.environ[name]
        result = int(pytest_main(arguments, plugins=plugins))
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


class _SelectedNodePlugin:
    """Record collection and execution for the selected nodes."""

    def __init__(self, nodes: Sequence[str]) -> None:
        """Set the expected nodes and empty result arrays."""
        self.nodes = list(nodes)
        self.results: dict[str, list[str]] = {
            name: [] for name in ("collected", "executed", "passed", "skipped", "failed", "errors")
        }
        self.phases: set[tuple[str, str | None]] = set()
        self.call_passed: set[str] = set()

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Reject an incorrect collection of node IDs."""
        self.results["collected"] = [item.nodeid for item in session.items]
        if sorted(self.results["collected"]) != sorted(self.nodes):
            raise pytest.UsageError("The collected nodes are not the selected nodes.")

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Record collection failures."""
        if report.failed:
            self.results["errors"].append(report.nodeid)
        elif report.skipped:
            self.results["skipped"].append(report.nodeid)

    def pytest_deselected(self, items: Sequence[pytest.Item]) -> None:
        """Record unexpected deselection as an error."""
        self.results["errors"].extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Record test calls and all setup and teardown results."""
        phase = (report.nodeid, report.when)
        if phase in self.phases:
            self.results["errors"].append(report.nodeid)
        self.phases.add(phase)
        if report.when == "call":
            self.results["executed"].append(report.nodeid)
            if report.passed:
                self.call_passed.add(report.nodeid)
            elif report.failed:
                self.results["failed"].append(report.nodeid)
        elif report.failed:
            self.results["errors"].append(report.nodeid)
        if report.skipped:
            self.results["skipped"].append(report.nodeid)
        if report.when == "teardown" and report.passed and report.nodeid in self.call_passed:
            self.results["passed"].append(report.nodeid)


def run_selected_nodes(
    nodes: Sequence[str],
    *,
    repository_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    junitxml: str | None = None,
) -> int:
    """Run the selected nodes and require complete passing results."""
    if (
        not nodes
        or any(not isinstance(node, str) or not node or node.startswith("-") for node in nodes)
        or len(nodes) != len(set(nodes))
    ):
        raise TierConfigurationError("The selected nodes must be nonempty and unique.")
    plugin = _SelectedNodePlugin(nodes)
    directory = Path.cwd()
    git_environment = _git_repository_environment()
    addopts = os.environ.pop("PYTEST_ADDOPTS", None)
    try:
        for name in git_environment:
            del os.environ[name]
        os.chdir(repository_root)
        arguments = [*nodes, "-q", "--durations=20"]
        if junitxml is not None:
            arguments.append(f"--junitxml={junitxml}")
        result = int(pytest.main(arguments, plugins=[plugin]))
    finally:
        os.chdir(directory)
        for name in _git_repository_environment():
            del os.environ[name]
        os.environ.update(git_environment)
        if addopts is not None:
            os.environ["PYTEST_ADDOPTS"] = addopts
        else:
            os.environ.pop("PYTEST_ADDOPTS", None)
        print(json.dumps(plugin.results, sort_keys=True), file=stdout)
    expected = sorted(nodes)
    complete = all(
        sorted(plugin.results[name]) == expected for name in ("collected", "executed", "passed")
    )
    complete = complete and plugin.phases == {
        (node, phase) for node in nodes for phase in ("setup", "call", "teardown")
    }
    if (
        result
        or not complete
        or any(plugin.results[name] for name in ("skipped", "failed", "errors"))
    ):
        print("The selected tests did not give complete passing results.", file=stderr)
        return result or 1
    return 0


def _run_affected(parsed: argparse.Namespace, pytest_arguments: Sequence[str]) -> int:
    """Validate the current scope before a job can execute tests."""
    if parsed.profile != "pr" or pytest_arguments:
        raise TierConfigurationError("A scoped call requires pr without extra pytest arguments.")
    context = (
        parsed.event_name,
        parsed.run_id,
        parsed.run_attempt,
        parsed.base_sha,
        parsed.head_sha,
    )
    if not all(context):
        raise TierConfigurationError("A scoped call requires the complete current event.")
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    from k2_ci_policy import JOB_IDS, validate_scope

    job_id = "test" if parsed.affected_job is None else parsed.affected_job
    if job_id not in JOB_IDS:
        raise TierConfigurationError("The affected job is not a fixed job ID.")
    checkout = subprocess.run(
        ["git", "--no-optional-locks", "rev-parse", "--verify", "HEAD"],
        cwd=REPOSITORY_ROOT,
        env={name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    scope = validate_scope(
        json.loads(parsed.affected_scope_json),
        event={
            "name": parsed.event_name,
            "run_id": parsed.run_id,
            "run_attempt": parsed.run_attempt,
        },
        base=parsed.base_sha,
        head=parsed.head_sha,
        checkout_head=checkout,
        policy_sha256=hashlib.sha256(
            (REPOSITORY_ROOT / "scripts/k2_ci_policy.py").read_bytes()
        ).hexdigest(),
    )
    allocation = scope["jobs"][job_id]
    evidence: dict[str, Any] = {"scope": scope, "job": job_id, **allocation}
    evidence.update(shard_index=parsed.shard_index, shard_count=parsed.shard_count)
    if job_id == "recycling-coverage" or scope["route"] == "promotion":
        print(json.dumps(evidence, sort_keys=True))
        return 0
    if scope["route"] == "legacy":
        if job_id != "test":
            raise TierConfigurationError("A legacy contract must use its fixed dispatcher.")
        result = run_profile(
            "pr",
            shard_index=parsed.shard_index,
            shard_count=parsed.shard_count,
            pytest_arguments=([f"--junitxml={parsed.junitxml}"] if parsed.junitxml else []),
        )
    elif parsed.shard_index != 0:
        evidence["shard_result"] = "NO_OP"
        result = 0
    elif allocation["decision"] == "run":
        output = io.StringIO()
        result = run_selected_nodes(
            allocation["selectors"],
            repository_root=REPOSITORY_ROOT,
            stdout=output,
            stderr=sys.stderr,
            junitxml=parsed.junitxml,
        )
        evidence.update(json.loads(output.getvalue()))
    else:
        result = 0
    print(json.dumps(evidence, sort_keys=True))
    return result


def _validate_shard(index: int, count: int) -> None:
    if count < 1 or index < 0 or index >= count:
        raise TierConfigurationError("The shard index or count is not valid.")


def _parse_arguments(arguments: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    runner_arguments = arguments[:separator]
    pytest_arguments = list(arguments[separator + 1 :]) if separator < len(arguments) else []

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument("--affected-scope-json")
    parser.add_argument("--affected-job")
    parser.add_argument("--event-name")
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt")
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--junitxml")
    parsed = parser.parse_args(runner_arguments)
    return parsed, pytest_arguments


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    parsed, pytest_arguments = _parse_arguments(sys.argv[1:] if arguments is None else arguments)
    try:
        _validate_shard(parsed.shard_index, parsed.shard_count)
        if parsed.affected_scope_json is not None:
            try:
                return _run_affected(parsed, pytest_arguments)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise TierConfigurationError(str(error)) from error
        if any(
            value is not None
            for value in (
                parsed.affected_job,
                parsed.event_name,
                parsed.run_id,
                parsed.run_attempt,
                parsed.base_sha,
                parsed.head_sha,
            )
        ):
            raise TierConfigurationError("Scope context requires an explicit scope record.")
        return run_profile(
            parsed.profile,
            pytest_arguments=[
                *pytest_arguments,
                *([f"--junitxml={parsed.junitxml}"] if parsed.junitxml else []),
            ],
            shard_index=parsed.shard_index,
            shard_count=parsed.shard_count,
        )
    except TierConfigurationError as error:
        print(f"The test-tier configuration has an error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
