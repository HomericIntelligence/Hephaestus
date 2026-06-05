"""Tests for dependency floor consistency between pyproject.toml and pixi.toml.

Validates that PyGithub floor versions match between the two manifest files,
ensuring the published install contract does not permit API-incompatible
versions that are never tested.
"""

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


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


def _upper_cap(spec: str) -> str:
    """Extract the upper-cap version (<X[.Y[.Z]]) from a PEP 508 / pixi spec.

    Args:
        spec: A constraint string like "mypy>=1.8.0,<2" or ">=1.8.0,<2".

    Returns:
        The upper-cap version (e.g., "2").

    Raises:
        AssertionError: If no "<" cap clause is present in the spec.

    """
    # Split off any name prefix (PEP 508) so we only scan the version range.
    range_part = spec.split(">=", 1)[1] if ">=" in spec else spec
    for clause in range_part.split(","):
        clause = clause.strip()
        if clause.startswith("<") and not clause.startswith("<="):
            return clause[1:].strip()
    raise AssertionError(f"No '<' upper-cap found in constraint spec: {spec}")


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


class TestMypyUpperCapConsistency:
    """Tests for mypy upper-cap consistency across pyproject.toml and pixi.toml.

    mypy 1.x and 2.x have different error semantics; the pixi-driven CI
    (`pixi run mypy`) only ever resolves the cap declared in pixi.toml, so
    a pip-install user on `.[dev]` must see the same cap or they land on
    an untested major. Tracks issue #748.
    """

    def test_mypy_upper_cap_matches_across_manifests(self, repo_root: Path) -> None:
        """Mypy upper-cap in pyproject.toml dev extra must equal both pixi features."""
        pyproject_path = repo_root / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        pyproject_spec = next(
            (dep for dep in dev_deps if dep.startswith("mypy")),
            None,
        )
        assert pyproject_spec is not None, "mypy not found in [project.optional-dependencies.dev]"

        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        pixi_dev_spec = pixi["feature"]["dev"]["dependencies"]["mypy"]
        pixi_lint_spec = pixi["feature"]["lint"]["dependencies"]["mypy"]

        pyproject_cap = _upper_cap(pyproject_spec)
        pixi_dev_cap = _upper_cap(pixi_dev_spec)
        pixi_lint_cap = _upper_cap(pixi_lint_spec)

        assert pyproject_cap == pixi_dev_cap == pixi_lint_cap, (
            "mypy upper-cap skew: "
            f"pyproject.toml dev={pyproject_cap} vs "
            f"pixi.toml [feature.dev]={pixi_dev_cap} vs "
            f"[feature.lint]={pixi_lint_cap}. "
            "Update all three together — see issue #748."
        )

    def test_mypy_floor_matches_across_manifests(self, repo_root: Path) -> None:
        """Mypy floor in pyproject.toml dev extra must equal both pixi features."""
        pyproject_path = repo_root / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        pyproject_spec = next(
            (dep for dep in dev_deps if dep.startswith("mypy")),
            None,
        )
        assert pyproject_spec is not None

        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        pixi_dev_spec = pixi["feature"]["dev"]["dependencies"]["mypy"]
        pixi_lint_spec = pixi["feature"]["lint"]["dependencies"]["mypy"]

        pyproject_floor = _floor(pyproject_spec)
        pixi_dev_floor = _floor(pixi_dev_spec)
        pixi_lint_floor = _floor(pixi_lint_spec)

        assert pyproject_floor == pixi_dev_floor == pixi_lint_floor, (
            "mypy floor skew: "
            f"pyproject.toml dev={pyproject_floor} vs "
            f"pixi.toml [feature.dev]={pixi_dev_floor} vs "
            f"[feature.lint]={pixi_lint_floor}."
        )
