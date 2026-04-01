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


def check_project_version_consistency(repo_root: Path) -> bool:
    """Check that pyproject.toml and pixi.toml project versions agree (if both present).

    Reads the [project].version from pyproject.toml and the [workspace].version
    from pixi.toml (if it exists) and verifies they match.  If pixi.toml has no
    version field the check passes — that is the expected state for this project.

    Args:
        repo_root: Repository root directory.

    Returns:
        True if versions are consistent (or pixi.toml has no version), False otherwise.

    """
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return True  # nothing to compare against

    project_version = extract_project_version(pyproject_path.read_text())

    pixi_path = repo_root / "pixi.toml"
    if not pixi_path.exists():
        if project_version:
            print(f"OK: pyproject.toml project version = {project_version!r} (no pixi.toml)")
        return True

    pixi_version = extract_pixi_workspace_version(pixi_path.read_text())

    if pixi_version is None:
        # pixi.toml exists but has no version — expected state
        if project_version:
            print(f"OK: pyproject.toml project version = {project_version!r}")
        print("OK: pixi.toml has no [workspace].version (as expected)")
        return True

    # Both files have a version — they must match
    if project_version == pixi_version:
        print(f"OK: project version is consistent at {project_version!r}")
        return True

    print("ERROR: project version mismatch between pyproject.toml and pixi.toml!")
    print(f"  pyproject.toml [project].version = {project_version!r}")
    print(f"  pixi.toml [workspace].version    = {pixi_version!r}")
    print("  pyproject.toml is the single source of truth — update pixi.toml to match.")
    return False


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
        python_ok = True
    else:
        unique_versions = set(versions.values())
        if len(unique_versions) == 1:
            version = next(iter(unique_versions))
            print(f"OK: Python version is consistent at {version}")
            for key, val in versions.items():
                print(f"  {key}: {val}")
            python_ok = True
        else:
            print("ERROR: Python version inconsistency detected!")
            for key, val in versions.items():
                print(f"  {key}: {val}")
            print("\nAll version specifications must agree on the same Python version.")
            python_ok = False

    project_version_ok = check_project_version_consistency(repo_root)

    if python_ok and project_version_ok:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
