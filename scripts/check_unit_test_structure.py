#!/usr/bin/env python3
"""Enforce test-suite structure for ProjectHephaestus.

Two checks:

1. **Subpackage mirror** — every subpackage in ``hephaestus/`` has a matching
   ``tests/unit/<name>/`` directory.
2. **Scripts coverage** — every ``scripts/*.py`` is auto-covered by the
   smoke harness. We assert that ``tests/unit/scripts/conftest.py`` and
   ``tests/unit/scripts/test_scripts_smoke.py`` both exist (the conftest
   globs ``scripts/*.py`` so coverage is automatic), and we re-check the
   glob marker so a refactor that breaks auto-discovery is caught here.

Usage:
    python scripts/check_unit_test_structure.py
"""

import sys
from pathlib import Path

from hephaestus.validation.test_structure import (
    _get_subpackages,
    check_scripts_coverage,
    check_test_directory_mirrors,
)


def main() -> int:
    """Run the subpackage-mirror and scripts-coverage checks; non-zero on failure."""
    repo_root = Path(__file__).parent.parent
    src_root = repo_root / "hephaestus"
    tests_root = repo_root / "tests" / "unit"
    scripts_root = repo_root / "scripts"

    for label, path in (("Source", src_root), ("Test", tests_root), ("Scripts", scripts_root)):
        if not path.exists():
            print(f"ERROR: {label} root not found: {path}", file=sys.stderr)
            return 1

    failed = False

    mirrored, missing = check_test_directory_mirrors(src_root, tests_root)
    if not mirrored:
        failed = True
        print(
            "ERROR: The following hephaestus subpackages have no corresponding "
            "tests/unit/ directory:",
            file=sys.stderr,
        )
        for name in sorted(missing):
            print(f"  hephaestus/{name}  →  tests/unit/{name}/ (missing)", file=sys.stderr)

    ok_scripts, scripts_errors = check_scripts_coverage(scripts_root, tests_root)
    if not ok_scripts:
        failed = True
        print("ERROR: scripts/ test coverage is incomplete:", file=sys.stderr)
        for line in scripts_errors:
            print(line, file=sys.stderr)

    if failed:
        return 1

    n_subpkgs = len(_get_subpackages(src_root))
    n_scripts = sum(1 for _ in scripts_root.glob("*.py"))
    print(
        f"OK: {n_subpkgs} subpackages have test directories; "
        f"{n_scripts} scripts/*.py covered by the smoke harness."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
