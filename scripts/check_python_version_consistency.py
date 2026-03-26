#!/usr/bin/env python3
"""Check Python version consistency across project config files.

Verifies that requires-python in pyproject.toml, python_version in mypy config,
and target-version in ruff config are all consistent.

Usage:
    python scripts/check_python_version_consistency.py
"""

import re
import sys
from pathlib import Path


def get_repo_root() -> Path:
    """Find repository root by looking for pyproject.toml."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def extract_pyproject_versions(content: str) -> dict[str, str]:
    """Extract version strings from pyproject.toml content."""
    versions = {}

    # requires-python = ">=3.10"
    match = re.search(r'requires-python\s*=\s*"([^"]+)"', content)
    if match:
        # Normalize to major.minor (e.g., ">=3.10" -> "3.10")
        raw = match.group(1)
        ver_match = re.search(r"(\d+\.\d+)", raw)
        if ver_match:
            versions["requires-python"] = ver_match.group(1)

    # [tool.mypy] python_version = "3.10"
    match = re.search(
        r'\[tool\.mypy\]\n(?:(?!\[).+\n)*?python_version\s*=\s*"([^"]+)"',
        content,
    )
    if match:
        versions["mypy.python_version"] = match.group(1)

    # [tool.ruff] target-version = "py310"
    match = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', content)
    if match:
        versions["ruff.target-version"] = f"{match.group(1)}.{match.group(2)}"

    return versions


def extract_project_version(content: str) -> str | None:
    """Extract version from the [project] section in pyproject.toml content.

    Uses a section-bounded regex with negative lookahead to prevent matching
    version keys that belong to a different TOML section.

    Args:
        content: The raw text content of a pyproject.toml file.

    Returns:
        The version string if found within [project], or None.

    """
    match = re.search(
        r'\[project\]\n(?:(?!\[).+\n)*?version\s*=\s*"([^"]+)"',
        content,
    )
    return match.group(1) if match else None


def extract_pixi_workspace_version(content: str) -> str | None:
    """Extract version from the [workspace] section in pixi.toml content.

    Uses a section-bounded regex with negative lookahead to prevent matching
    version keys that belong to a different TOML section.

    Args:
        content: The raw text content of a pixi.toml file.

    Returns:
        The version string if found within [workspace], or None.

    """
    match = re.search(
        r'\[workspace\]\n(?:(?!\[).+\n)*?version\s*=\s*"([^"]+)"',
        content,
    )
    return match.group(1) if match else None


def main() -> int:
    """Check Python version consistency. Returns 0 if OK, 1 if inconsistent."""
    repo_root = get_repo_root()
    pyproject_path = repo_root / "pyproject.toml"

    if not pyproject_path.exists():
        print(f"ERROR: pyproject.toml not found at {pyproject_path}")
        return 1

    content = pyproject_path.read_text()
    versions = extract_pyproject_versions(content)

    if len(versions) < 2:
        print(f"WARNING: Could only find {len(versions)} version spec(s) in pyproject.toml")
        for key, val in versions.items():
            print(f"  {key}: {val}")
        return 0

    unique_versions = set(versions.values())
    if len(unique_versions) == 1:
        version = next(iter(unique_versions))
        print(f"OK: Python version is consistent at {version}")
        for key, val in versions.items():
            print(f"  {key}: {val}")
        return 0

    print("ERROR: Python version inconsistency detected!")
    for key, val in versions.items():
        print(f"  {key}: {val}")
    print("\nAll version specifications must agree on the same Python version.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
