#!/usr/bin/env python3
"""Enforce that tests/unit/ mirrors the hephaestus/ package structure.

For every subpackage in hephaestus/ there should be a corresponding
directory under tests/unit/ containing at least one test file.

Usage:
    python scripts/check_unit_test_structure.py
"""

import sys
from pathlib import Path


def get_subpackages(root: Path) -> set[str]:
    """Return the set of direct subpackage names under root."""
    return {
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
    }


def main() -> int:
    """Check that tests/unit mirrors hephaestus/ subpackage structure."""
    repo_root = Path(__file__).parent.parent
    src_root = repo_root / "hephaestus"
    tests_root = repo_root / "tests" / "unit"

    if not src_root.exists():
        print(f"ERROR: Source root not found: {src_root}", file=sys.stderr)
        return 1

    if not tests_root.exists():
        print(f"ERROR: Test root not found: {tests_root}", file=sys.stderr)
        return 1

    src_packages = get_subpackages(src_root)
    test_packages = get_subpackages(tests_root)

    missing = src_packages - test_packages
    if missing:
        print(
            "ERROR: The following hephaestus subpackages have no corresponding "
            "tests/unit/ directory:",
            file=sys.stderr,
        )
        for name in sorted(missing):
            print(f"  hephaestus/{name}  →  tests/unit/{name}/ (missing)", file=sys.stderr)
        return 1

    print(f"OK: All {len(src_packages)} source subpackages have test directories.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
