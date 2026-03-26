#!/usr/bin/env python3
"""Check that pyproject.toml is the single source of truth for the project version.

Validates that:
- pyproject.toml has a valid version under [project]
- pixi.toml does NOT have a version field under [workspace]

This prevents version drift by ensuring only one authoritative version source exists.

Usage:
    python scripts/check_version_single_source.py
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


def check_pyproject_has_version(repo_root: Path) -> bool:
    """Verify pyproject.toml has a version under [project]."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        print(f"ERROR: pyproject.toml not found at {pyproject_path}")
        return False

    content = pyproject_path.read_text()
    match = re.search(r'\[project\].*?version\s*=\s*"([^"]+)"', content, re.DOTALL)
    if not match:
        print("ERROR: No version found under [project] in pyproject.toml")
        return False

    print(f'OK: pyproject.toml [project] version = "{match.group(1)}"')
    return True


def check_pixi_no_version(repo_root: Path) -> bool:
    """Verify pixi.toml does NOT have a version under [workspace]."""
    pixi_path = repo_root / "pixi.toml"
    if not pixi_path.exists():
        # No pixi.toml is fine — nothing to conflict
        return True

    content = pixi_path.read_text()

    # Check for version = "..." in the [workspace] section
    # The [workspace] section ends at the next [section] header or EOF
    workspace_match = re.search(r"\[workspace\](.*?)(?=\n\[|\Z)", content, re.DOTALL)
    if not workspace_match:
        # No [workspace] section — nothing to conflict
        return True

    workspace_content = workspace_match.group(1)
    version_match = re.search(r"^\s*version\s*=", workspace_content, re.MULTILINE)
    if version_match:
        print("ERROR: pixi.toml [workspace] contains a 'version' field.")
        print("  pyproject.toml is the single source of truth for the project version.")
        print("  Remove the version field from pixi.toml [workspace].")
        return False

    print("OK: pixi.toml has no version field (as expected)")
    return True


def main() -> int:
    """Check version single source of truth. Returns 0 if OK, 1 if issues found."""
    repo_root = get_repo_root()

    pyproject_ok = check_pyproject_has_version(repo_root)
    pixi_ok = check_pixi_no_version(repo_root)

    if pyproject_ok and pixi_ok:
        print("\nVersion single source of truth: PASS")
        return 0

    print("\nVersion single source of truth: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
