"""Tests for the closed PR-review host-verification catalog."""

from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType

import pytest


def test_closed_host_verification_catalog_is_available() -> None:
    """PR review has one importable, repository-owned command catalog."""
    assert importlib.util.find_spec("hephaestus.automation.host_verification_catalog") is not None


def _catalog() -> ModuleType:
    """Import the catalog only after its availability contract is checked."""
    return importlib.import_module("hephaestus.automation.host_verification_catalog")


def test_python_change_selects_the_fixed_offline_python_catalog() -> None:
    """A Python path selects each fixed Python verification in catalog order."""
    catalog = _catalog()
    selected = catalog.select_host_verification_specs(["hephaestus/automation/example.py"])

    assert tuple(spec.descr for spec in selected) == (
        "review_python_ruff_check",
        "review_python_ruff_format",
        "review_python_mypy",
        "review_python_unit_suite",
    )
    assert selected[0].argv == (
        "uv",
        "run",
        "--offline",
        "--no-sync",
        "ruff",
        "check",
        "hephaestus/",
        "tests/",
    )
    assert selected[-1].argv == (
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
    )


def test_catalog_commands_have_the_exact_documented_argv() -> None:
    """The closed catalog cannot drift from the Linux runtime contract."""
    catalog = _catalog()

    assert {spec.descr: spec.argv for spec in catalog.HOST_VERIFICATION_CATALOG} == {
        "review_python_ruff_check": (
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "ruff",
            "check",
            "hephaestus/",
            "tests/",
        ),
        "review_python_ruff_format": (
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
        "review_python_mypy": (
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
        "review_python_unit_suite": (
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
        "review_full_unit_coverage": (
            "uv",
            "run",
            "--offline",
            "--no-sync",
            "python",
            "-m",
            "hephaestus.automation.host_coverage",
        ),
        "review_migration_version_currency": (
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
        "review_worker_pool_agent_execution_error": (
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
        "review_stalled_consumer_verification": (
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
    }


@pytest.mark.parametrize(
    ("changed_paths", "expected_ids"),
    [
        (
            ["pyproject.toml"],
            (
                "review_python_ruff_check",
                "review_python_ruff_format",
                "review_python_mypy",
                "review_python_unit_suite",
                "review_full_unit_coverage",
            ),
        ),
        (
            ["docs/MIGRATION.md"],
            ("review_migration_version_currency",),
        ),
        (
            ["tests/unit/automation/pipeline/test_worker_pool.py"],
            (
                "review_python_ruff_check",
                "review_python_ruff_format",
                "review_python_mypy",
                "review_python_unit_suite",
                "review_worker_pool_agent_execution_error",
            ),
        ),
        (
            ["tests/performance/test_worker_pool_load.py"],
            (
                "review_python_ruff_check",
                "review_python_ruff_format",
                "review_python_mypy",
                "review_python_unit_suite",
                "review_stalled_consumer_verification",
            ),
        ),
    ],
)
def test_changed_path_predicates_select_only_closed_catalog_entries(
    changed_paths: list[str], expected_ids: tuple[str, ...]
) -> None:
    """Every path predicate selects the fixed catalog entries in stable order."""
    selected = _catalog().select_host_verification_specs(changed_paths)

    assert tuple(spec.descr for spec in selected) == expected_ids


def test_deleted_and_renamed_python_paths_activate_python_catalog_once() -> None:
    """Checkout-derived old and new paths need no diff-text parsing."""
    selected = _catalog().select_host_verification_specs(
        [
            "tests/unit/removed_test.py",
            "tests/unit/renamed_from.py",
            "tests/unit/renamed_to.py",
            "tests/unit/renamed_to.py",
        ]
    )

    assert tuple(spec.descr for spec in selected) == (
        "review_python_ruff_check",
        "review_python_ruff_format",
        "review_python_mypy",
        "review_python_unit_suite",
    )


def test_unknown_or_mismatched_command_request_is_rejected() -> None:
    """A worker can execute only the exact catalog argv for its command ID."""
    catalog = _catalog()
    known = catalog.HOST_VERIFICATION_CATALOG[0]

    assert catalog.resolve_host_verification_spec(known.descr, known.argv) is known
    with pytest.raises(ValueError, match="unknown host verification command"):
        catalog.resolve_host_verification_spec("review_untrusted", known.argv)
    with pytest.raises(ValueError, match="does not match catalog argv"):
        catalog.resolve_host_verification_spec(known.descr, (*known.argv, "--unsafe"))


def test_digest_path_sets_include_catalog_consumers() -> None:
    """The sealed build and contract sets bind the catalog and bypass gate."""
    catalog = _catalog()
    assert "hephaestus/automation/host_verification_catalog.py" in catalog.CONTRACT_DIGEST_PATHS
    assert (
        "hephaestus/automation/pipeline/stages/pr_review_threads.py"
        in catalog.CONTRACT_DIGEST_PATHS
    )
    assert "ci/Containerfile" in catalog.BUILD_CONTEXT_EXACT_PATHS
    assert "pyproject.toml" in catalog.BUILD_CONTEXT_EXACT_PATHS
