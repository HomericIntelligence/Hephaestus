"""Tests for dependency floor consistency between pyproject.toml and pixi.toml.

Validates that PyGithub floor versions match between the two manifest files,
ensuring the published install contract does not permit API-incompatible
versions that are never tested.
"""

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from packaging.version import Version


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

        pixi_shared_spec = pixi["feature"]["shared"]["dependencies"]["mypy"]

        pyproject_cap = _upper_cap(pyproject_spec)
        pixi_shared_cap = _upper_cap(pixi_shared_spec)

        assert pyproject_cap == pixi_shared_cap, (
            "mypy upper-cap skew: "
            f"pyproject.toml dev={pyproject_cap} vs "
            f"pixi.toml [feature.shared]={pixi_shared_cap}. "
            "Update both together — see issue #748."
        )

    def test_mypy_floor_matches_across_manifests(self, repo_root: Path) -> None:
        """Mypy floor in pyproject.toml dev extra must equal pixi shared feature."""
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

        pixi_shared_spec = pixi["feature"]["shared"]["dependencies"]["mypy"]

        pyproject_floor = _floor(pyproject_spec)
        pixi_shared_floor = _floor(pixi_shared_spec)

        assert pyproject_floor == pixi_shared_floor, (
            "mypy floor skew: "
            f"pyproject.toml dev={pyproject_floor} vs "
            f"pixi.toml [feature.shared]={pixi_shared_floor}."
        )


class TestPytestConsistency:
    """Tests for pytest and pytest-cov floor/cap consistency across manifests.

    pytest 9.x and pytest-cov 7.x are bleeding-edge majors; the pixi-driven
    CI (`pixi run pytest`) resolves the cap declared in pixi.toml, so a
    pip-install user on `.[dev]` must see the same cap or they land on an
    untested major. Tracks issue #785.

    Comparisons use packaging.version.Version (PEP 440) so that "9.0"
    (pyproject style) and "9.0.0" (pixi style) compare equal semantically
    — raw string equality would fail despite the manifests being aligned.
    """

    @staticmethod
    def _find_dep(dev_deps: list[str], name: str) -> str | None:
        """Return the first dep in dev_deps whose package name is exactly `name`.

        Uses PEP 508 specifier punctuation to avoid matching prefix collisions
        (e.g., "pytest" must not match "pytest-cov").
        """
        for dep in dev_deps:
            head = dep.split(";", 1)[0].strip()
            for sep in ("<=", ">=", "==", "!=", "~=", "<", ">", "="):
                if sep in head:
                    pkg = head.split(sep, 1)[0].strip()
                    break
            else:
                pkg = head.strip()
            if pkg == name:
                return dep
        return None

    def test_pytest_floor_and_cap_match_across_manifests(self, repo_root: Path) -> None:
        """Pytest floor and cap must match [feature.dev]."""
        pyproject_path = repo_root / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        pyproject_spec = self._find_dep(dev_deps, "pytest")
        assert pyproject_spec is not None, "pytest not found in [project.optional-dependencies.dev]"

        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        pixi_dev_spec = pixi["feature"]["dev"]["dependencies"]["pytest"]

        pyproject_floor = Version(_floor(pyproject_spec))
        pixi_dev_floor = Version(_floor(pixi_dev_spec))
        assert pyproject_floor == pixi_dev_floor, (
            "pytest floor skew (semantic): "
            f"pyproject.toml dev={pyproject_floor} vs "
            f"pixi.toml [feature.dev]={pixi_dev_floor}. "
            "Update both together — see issue #785."
        )

        pyproject_cap = Version(_upper_cap(pyproject_spec))
        pixi_dev_cap = Version(_upper_cap(pixi_dev_spec))
        assert pyproject_cap == pixi_dev_cap, (
            "pytest upper-cap skew (semantic): "
            f"pyproject.toml dev={pyproject_cap} vs "
            f"pixi.toml [feature.dev]={pixi_dev_cap}. "
            "Update both together — see issue #785."
        )

    def test_pytest_cov_floor_and_cap_match_across_manifests(self, repo_root: Path) -> None:
        """pytest-cov floor and cap must match [feature.dev]."""
        pyproject_path = repo_root / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        pyproject_spec = self._find_dep(dev_deps, "pytest-cov")
        assert pyproject_spec is not None, (
            "pytest-cov not found in [project.optional-dependencies.dev]"
        )

        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        pixi_dev_spec = pixi["feature"]["dev"]["dependencies"]["pytest-cov"]

        pyproject_floor = Version(_floor(pyproject_spec))
        pixi_dev_floor = Version(_floor(pixi_dev_spec))
        assert pyproject_floor == pixi_dev_floor, (
            "pytest-cov floor skew (semantic): "
            f"pyproject.toml dev={pyproject_floor} vs "
            f"pixi.toml [feature.dev]={pixi_dev_floor}. "
            "Update both together — see issue #785."
        )

        pyproject_cap = Version(_upper_cap(pyproject_spec))
        pixi_dev_cap = Version(_upper_cap(pixi_dev_spec))
        assert pyproject_cap == pixi_dev_cap, (
            "pytest-cov upper-cap skew (semantic): "
            f"pyproject.toml dev={pyproject_cap} vs "
            f"pixi.toml [feature.dev]={pixi_dev_cap}. "
            "Update both together — see issue #785."
        )


class TestRuffConsistency:
    """Tests for ruff floor/cap consistency across pyproject.toml and pixi.toml.

    ruff 0.1.x and 0.15.x enforce different lint rulesets; the pixi-driven CI
    resolves the cap declared in pixi.toml [feature.shared], so a pip-install
    user on ``.[dev]`` must see the same floor or they land on an untested
    ruleset. Tracks issue #1201.

    Comparisons use packaging.version.Version (PEP 440) so that "0.15"
    and "0.15.0" compare equal semantically.

    Note: reads pixi.toml [feature.shared.dependencies] only — if ruff is ever
    added under [feature.lint.dependencies], extend this test to check that key.
    """

    def test_ruff_floor_matches_across_manifests(self, repo_root: Path) -> None:
        """Ruff floor in pyproject.toml dev extra must equal pixi shared feature."""
        pyproject_path = repo_root / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        pyproject_spec = TestPytestConsistency._find_dep(dev_deps, "ruff")
        assert pyproject_spec is not None, "ruff not found in [project.optional-dependencies.dev]"

        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        pixi_shared_spec = pixi["feature"]["shared"]["dependencies"]["ruff"]

        pyproject_floor = Version(_floor(pyproject_spec))
        pixi_shared_floor = Version(_floor(pixi_shared_spec))

        assert pyproject_floor == pixi_shared_floor, (
            "ruff floor skew (semantic): "
            f"pyproject.toml dev={pyproject_floor} vs "
            f"pixi.toml [feature.shared]={pixi_shared_floor}. "
            "Update both together — see issue #1201."
        )

    def test_ruff_upper_cap_matches_across_manifests(self, repo_root: Path) -> None:
        """Ruff upper-cap in pyproject.toml dev extra must equal pixi shared feature."""
        pyproject_path = repo_root / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        pyproject_spec = TestPytestConsistency._find_dep(dev_deps, "ruff")
        assert pyproject_spec is not None, "ruff not found in [project.optional-dependencies.dev]"

        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        pixi_shared_spec = pixi["feature"]["shared"]["dependencies"]["ruff"]

        pyproject_cap = Version(_upper_cap(pyproject_spec))
        pixi_shared_cap = Version(_upper_cap(pixi_shared_spec))

        assert pyproject_cap == pixi_shared_cap, (
            "ruff upper-cap skew (semantic): "
            f"pyproject.toml dev={pyproject_cap} vs "
            f"pixi.toml [feature.shared]={pixi_shared_cap}. "
            "Update both together — see issue #1201."
        )


class TestPipPinning:
    """pip in pixi.toml [dependencies] must carry both a >= floor and a < cap.

    pip is load-bearing for the dev-install editable path and pip-audit flows;
    an unbounded '*' would permit an untested major to silently land. Tracks
    issue #1202.
    """

    def test_pip_has_floor_and_cap(self, repo_root: Path) -> None:
        """Pip spec must be parseable by both _floor() and _upper_cap()."""
        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        assert "dependencies" in pixi, "No [dependencies] section in pixi.toml"
        assert "pip" in pixi["dependencies"], "pip not found in [dependencies]"

        spec = pixi["dependencies"]["pip"]
        # Delegate to the established helpers — raises AssertionError if floor/cap absent.
        _floor(spec)
        _upper_cap(spec)

    def test_pip_floor_is_at_least_23(self, repo_root: Path) -> None:
        """Pip floor must be >= 23.0 for stable PEP 660 editable-install support."""
        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        spec = pixi["dependencies"]["pip"]
        floor = Version(_floor(spec))
        assert floor >= Version("23.0"), (
            f"pip floor too low ({floor}); must be >= 23.0 for PEP 660 editable support"
        )

    def test_pip_cap_blocks_next_major_only(self, repo_root: Path) -> None:
        """Pip cap must be exactly one major ahead of the installed 26.x series.

        Asserts Version("26") < cap <= Version("27") so the test rejects both
        a downgrade cap (<= 26) and a permissive cap (>= 28) that would admit
        untested majors.
        """
        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        spec = pixi["dependencies"]["pip"]
        cap = Version(_upper_cap(spec))
        assert Version("26") < cap <= Version("27"), (
            f"pip cap {cap} must be > 26 (no downgrade) and <= 27 (block untested major); "
            "update when CI tests pip 27.x"
        )


class TestSecurityFloorTransitivePinning:
    """Security-fix transitive pins in pixi.toml must keep next-major caps."""

    @pytest.mark.parametrize(
        ("name", "minimum_floor"),
        [
            ("pygments", Version("2.20.0")),
            ("urllib3", Version("2.7.0")),
            ("pyjwt", Version("2.13.0")),
        ],
    )
    def test_security_floor_transitives_have_next_major_caps(
        self, repo_root: Path, name: str, minimum_floor: Version
    ) -> None:
        """Named security pins must keep their fix floor and block untested 3.x."""
        pixi_path = repo_root / "pixi.toml"
        with open(pixi_path, "rb") as f:
            pixi = tomllib.load(f)

        spec = pixi["dependencies"][name]
        floor = Version(_floor(spec))
        cap = Version(_upper_cap(spec))

        assert floor >= minimum_floor, (
            f"{name} floor {floor} is below required security floor {minimum_floor}"
        )
        assert cap == Version(str(floor.major + 1)), (
            f"{name} cap {cap} must be the next major after floor {floor}"
        )


class TestAllExtraDocsInSync:
    """Guard that `[all]`'s member extras are documented in both docs.

    The `[all]` aggregator's member extras must be documented in both the
    pyproject.toml aggregator comment and the README `[all]` bullet (#1498).

    Guards a DRY/POLA invariant: `[all]` silently included the `automation`
    product layer (and its pydantic dependency) while both docs omitted it.
    The truth is derived from the manifest `all = [...]` spec rather than
    hardcoded, so adding a future extra to `[all]` without documenting it
    fails this test.
    """

    @staticmethod
    def _all_members(repo_root: Path) -> set[str]:
        """Return the set of extras named inside the `[all]` aggregator spec."""
        pyproject_path = repo_root / "pyproject.toml"
        with pyproject_path.open("rb") as f:
            pyproject = tomllib.load(f)
        all_specs = pyproject["project"]["optional-dependencies"]["all"]
        assert all_specs, "[project.optional-dependencies.all] is empty"
        # all = ["HomericIntelligence-Hephaestus[automation,github,nats,...]"]
        inner = re.search(r"\[([^\]]+)\]", all_specs[0])
        assert inner, f"No extras bracket found in [all] spec: {all_specs[0]!r}"
        return {e.strip() for e in inner.group(1).split(",") if e.strip()}

    def test_pyproject_comment_lists_all_members(self, repo_root: Path) -> None:
        """Assert the pyproject aggregator comment lists every `[all]` member.

        The comment block preceding [project.optional-dependencies] must
        enumerate every member extra of `[all]`.
        """
        members = self._all_members(repo_root)
        text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        # Isolate the aggregator comment block preceding the header.
        comment = text.split("[project.optional-dependencies]", 1)[0]
        missing = sorted(m for m in members if m not in comment)
        assert not missing, f"pyproject.toml aggregator comment omits [all] member(s): {missing}"

    def test_readme_all_bullet_lists_all_members(self, repo_root: Path) -> None:
        """Assert the README `[all]` bullet lists every `[all]` member.

        The bullet under "### Optional dependencies" must enumerate every
        member extra of `[all]`.
        """
        members = self._all_members(repo_root)
        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        # The `[all]` bullet enumerates the runtime extras just after the header.
        section = readme.split("### Optional dependencies", 1)[1].split("###", 1)[0]
        missing = sorted(m for m in members if m not in section)
        assert not missing, f"README `[all]` section omits member(s): {missing}"
