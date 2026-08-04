"""Regression tests for the Windows-only tzdata requirement (issue #2149)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def _tzdata_requirement() -> Requirement:
    """Return the sole tzdata requirement from project dependencies."""
    project = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]
    requirements = [Requirement(spec) for spec in project["dependencies"]]
    matches = [requirement for requirement in requirements if requirement.name == "tzdata"]
    assert len(matches) == 1, f"expected one tzdata dependency, found {matches}"
    return matches[0]


def test_tzdata_uses_supported_2026_range() -> None:
    """Tzdata must stay within the supported 2026 release series."""
    requirement = _tzdata_requirement()

    assert requirement.specifier == SpecifierSet(">=2026.2,<2027")


def test_tzdata_remains_windows_only() -> None:
    """POSIX installations must not acquire the Windows timezone-data fallback."""
    marker = _tzdata_requirement().marker

    assert marker is not None
    assert marker.evaluate({"platform_system": "Windows"})
    assert not marker.evaluate({"platform_system": "Linux"})
    assert not marker.evaluate({"platform_system": "Darwin"})
