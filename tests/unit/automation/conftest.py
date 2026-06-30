"""Shared pytest fixtures for the automation test package.

The circuit-breaker reset is autouse at PACKAGE scope (#708) so every test
under ``tests/unit/automation/`` starts with a clean breaker state. Without
this, a test that trips the GitHub API breaker (e.g. via rate-limit
exhaustion paths) leaves it OPEN, and any later test that exercises the
github_api / pr_reviewer code paths inherits the open breaker and fails with
``GitHub API circuit breaker is open due to sustained unavailability``
instead of the domain-specific error it was actually asserting against.

The fixture was previously declared only inside ``test_github_api.py`` —
which protected that file but not the rest of the package. Promoting it
here keeps the contract obvious (one autouse fixture, one place) without
expanding scope to tests that don't depend on automation primitives.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.resilience.circuit_breaker import reset_all_circuit_breakers


@pytest.fixture(autouse=True)
def _reset_circuit_breakers_for_automation_tests() -> None:
    """Reset all circuit breakers before each automation test.

    Prevents cross-test contamination when a prior test trips a breaker via
    rate-limit retries.
    """
    reset_all_circuit_breakers()


@dataclass
class GitMocks:
    """Started mocks for a module's ``run`` and ``get_repo_root`` symbols."""

    run: MagicMock
    repo_root: MagicMock


def _patch_run_and_repo_root(module: str) -> Generator[GitMocks, None, None]:
    """Patch ``<module>.run`` and ``<module>.get_repo_root`` for one test (#1417).

    Replaces the 30+ duplicated ``@patch`` decorator pairs that every test
    stacked to mock the module-local ``run``/``get_repo_root`` symbols.
    """
    with (
        patch(f"{module}.run") as mock_run,
        patch(f"{module}.get_repo_root") as mock_repo_root,
    ):
        yield GitMocks(run=mock_run, repo_root=mock_repo_root)


@pytest.fixture
def git_utils_mocks() -> Generator[GitMocks, None, None]:
    """Mock ``run`` + ``get_repo_root`` as seen by ``git_utils`` (#1417)."""
    yield from _patch_run_and_repo_root("hephaestus.automation.git_utils")


@pytest.fixture
def worktree_mocks() -> Generator[GitMocks, None, None]:
    """Mock ``run`` + ``get_repo_root`` as seen by ``worktree_manager`` (#1417)."""
    yield from _patch_run_and_repo_root("hephaestus.automation.worktree_manager")
