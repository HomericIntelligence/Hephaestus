"""Shared integration-test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from .artifact_support import ControlledArtifacts, build_controlled_artifacts


def pytest_configure(config: pytest.Config) -> None:
    """Create the parent of a repository-relative pytest basetemp if needed."""
    basetemp = config.getoption("basetemp")
    if basetemp:
        Path(basetemp).parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def controlled_artifacts(tmp_path_factory: pytest.TempPathFactory) -> ControlledArtifacts:
    """Build and share deterministic artifacts across the integration session."""
    artifact_root = tmp_path_factory.mktemp("controlled-artifacts")
    return build_controlled_artifacts(Path(artifact_root))
