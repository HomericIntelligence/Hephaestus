"""Tests for the single pyproject dependency contract."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef, unused-ignore]


def test_tooling_dependencies_are_versioned_in_the_dev_group() -> None:
    """All developer tools have bounded versions in the single project manifest."""
    root = Path(__file__).resolve().parents[3]
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    dependencies = config["dependency-groups"]["dev"]
    for package in ("pytest", "pytest-cov", "ruff", "mypy", "bandit", "pdoc"):
        spec = next(item for item in dependencies if item.startswith(package))
        assert ">=" in spec and ",<" in spec
