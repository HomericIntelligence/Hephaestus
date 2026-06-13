#!/usr/bin/env python3
"""Check that the project version has exactly one source of truth.

This project uses hatch-vcs dynamic versioning: the version is derived from git
tags, not stored in any file. The single-source-of-truth invariant is therefore:

- ``pyproject.toml`` declares ``version`` in ``[project].dynamic`` and has NO
  static ``[project].version`` field.
- ``pyproject.toml`` configures ``[tool.hatch.version]`` with ``source = "vcs"``.
- ``pixi.toml`` has no ``version`` field under ``[workspace]``.

Any deviation (a reintroduced static version, a removed dynamic declaration, a
pixi workspace version) means the version now has two — or zero — authorities.

Usage:
    python scripts/check_version_single_source.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from hephaestus.utils.helpers import get_repo_root as _get_repo_root

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib


def get_repo_root() -> Path:
    """Find repository root, anchored to this module's location.

    Delegates to the canonical :func:`hephaestus.utils.helpers.get_repo_root`,
    seeding the search from this file so resolution is independent of the
    current working directory (this script may run from anywhere).
    """
    return _get_repo_root(Path(__file__).resolve().parent)


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file into a dict."""
    with path.open("rb") as fh:
        return tomllib.load(fh)


def check_pyproject_dynamic_version(repo_root: Path) -> bool:
    """Verify pyproject.toml uses hatch-vcs dynamic versioning with no static version.

    Returns True only when the version has exactly one authority (git tags via
    hatch-vcs). Returns False if a static ``[project].version`` was reintroduced
    or the dynamic/hatch-vcs configuration is missing.
    """
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        print(f"ERROR: pyproject.toml not found at {pyproject_path}")
        return False

    data = _load_toml(pyproject_path)
    project = data.get("project", {})

    if "version" in project:
        print(
            'ERROR: pyproject.toml [project] has a static "version" field.\n'
            "  This project uses hatch-vcs dynamic versioning (version from git tags).\n"
            '  Remove [project].version and keep "version" in [project].dynamic.'
        )
        return False

    dynamic = project.get("dynamic", [])
    if "version" not in dynamic:
        print(
            'ERROR: pyproject.toml [project].dynamic does not contain "version".\n'
            "  hatch-vcs dynamic versioning requires it."
        )
        return False

    hatch_version = data.get("tool", {}).get("hatch", {}).get("version", {})
    if hatch_version.get("source") != "vcs":
        print(
            'ERROR: pyproject.toml [tool.hatch.version] source is not "vcs".\n'
            "  Expected hatch-vcs as the single version authority."
        )
        return False

    print("OK: pyproject.toml uses hatch-vcs dynamic versioning (single source)")
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
        print("  pyproject.toml (hatch-vcs) is the single source of truth for the version.")
        print("  Remove the version field from pixi.toml [workspace].")
        return False

    print("OK: pixi.toml has no version field (as expected)")
    return True


def main() -> int:
    """Check version single source of truth. Returns 0 if OK, 1 if issues found."""
    repo_root = get_repo_root()

    pyproject_ok = check_pyproject_dynamic_version(repo_root)
    pixi_ok = check_pixi_no_version(repo_root)

    if pyproject_ok and pixi_ok:
        print("\nVersion single source of truth: PASS")
        return 0

    print("\nVersion single source of truth: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
