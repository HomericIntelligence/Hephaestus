"""Tests for dependency floor consistency between pyproject.toml and pixi.toml.

Validates that PyGithub floor versions match between the two manifest files,
ensuring the published install contract does not permit API-incompatible
versions that are never tested.
"""

from pathlib import Path

import pytest


def _floor(spec: str) -> str:
    """Extract the floor version (>=X.Y.Z) from a PEP 508 / pixi constraint string.

    Args:
        spec: A constraint string like "PyGithub>=2.9.1,<3" or ">=2.9.1,<3".

    Returns:
        The floor version (e.g., "2.9.1").

    Raises:
        AssertionError: If no ">=" clause is present in the spec.

    """
    if ">=" not in spec:
        raise AssertionError(f"No '>=' floor found in constraint spec: {spec}")

    # Extract the part after ">=" and before the next comma or end of string
    after_gte = spec.split(">=", 1)[1]
    version = after_gte.split(",")[0].strip()
    return version


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parents[3]


class TestDependencyFloorConsistency:
    """Tests for PyGithub floor consistency across manifests."""

    def test_pygithub_floor_matches_pixi(self, repo_root: Path) -> None:
        """PyGithub floor in pyproject.toml must match pixi.toml.

        Both manifests declare a PyGithub floor version. Since PyGithub 1.x
        and 2.x are API-incompatible, they must match to ensure the published
        install contract aligns with the tested (dev) environment.
        """
        import tomllib

        # Load pyproject.toml
        pyproject_path = repo_root / "pyproject.toml"
        assert pyproject_path.exists(), f"pyproject.toml not found at {pyproject_path}"

        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        # Navigate to [project.optional-dependencies.github]
        assert "project" in pyproject, "No [project] section in pyproject.toml"
        assert "optional-dependencies" in pyproject["project"], (
            "No [project.optional-dependencies] section in pyproject.toml"
        )
        assert "github" in pyproject["project"]["optional-dependencies"], (
            "No [project.optional-dependencies.github] section in pyproject.toml"
        )

        github_deps = pyproject["project"]["optional-dependencies"]["github"]
        assert isinstance(github_deps, list), (
            f"[project.optional-dependencies.github] is not a list: {type(github_deps)}"
        )

        pyproject_spec = None
        for dep in github_deps:
            if dep.startswith("PyGithub"):
                pyproject_spec = dep
                break

        assert pyproject_spec is not None, (
            "PyGithub not found in [project.optional-dependencies.github]"
        )

        # Load pixi.toml
        pixi_path = repo_root / "pixi.toml"
        assert pixi_path.exists(), f"pixi.toml not found at {pixi_path}"

        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        # Navigate to [dependencies.pygithub]
        assert "dependencies" in pixi, "No [dependencies] section in pixi.toml"
        assert "pygithub" in pixi["dependencies"], "No [dependencies.pygithub] entry in pixi.toml"

        pixi_spec = pixi["dependencies"]["pygithub"]
        assert isinstance(pixi_spec, str), (
            f"[dependencies.pygithub] is not a string: {type(pixi_spec)}"
        )

        # Extract and compare floors
        pyproject_floor = _floor(pyproject_spec)
        pixi_floor = _floor(pixi_spec)

        assert pyproject_floor == pixi_floor, (
            f"PyGithub floor skew: pyproject.toml={pyproject_floor} vs pixi.toml={pixi_floor}"
        )

    def test_pygithub_floor_is_2x_or_higher(self, repo_root: Path) -> None:
        """PyGithub floor must be 2.x or higher.

        PyGithub 1.x and 2.x are API-incompatible. The floor must be at least 2.x
        to ensure the code is tested against the versions it can use.
        """
        import tomllib

        pyproject_path = repo_root / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        github_deps = pyproject["project"]["optional-dependencies"]["github"]
        pyproject_spec = next(
            (dep for dep in github_deps if dep.startswith("PyGithub")),
            None,
        )

        assert pyproject_spec is not None, (
            "PyGithub not found in [project.optional-dependencies.github]"
        )

        floor = _floor(pyproject_spec)
        major_version = int(floor.split(".")[0])

        assert major_version >= 2, (
            f"PyGithub floor must be 2.x or higher for API compatibility; found {floor}"
        )
