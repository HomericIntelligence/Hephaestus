"""Verify supported-platform metadata and the removal of Windows dependencies."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def test_base_dependencies_do_not_include_windows_timezone_fallback() -> None:
    """Supported installations do not declare the Windows timezone fallback."""
    project = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]
    requirements = [Requirement(spec) for spec in project["dependencies"]]
    assert all(requirement.name != "tzdata" for requirement in requirements)


def test_platform_classifiers_declare_only_linux_and_macos() -> None:
    """Package metadata declares the supported operating systems explicitly."""
    project = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]
    platforms = {
        value for value in project["classifiers"] if value.startswith("Operating System ::")
    }
    assert platforms == {
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS :: MacOS X",
    }
