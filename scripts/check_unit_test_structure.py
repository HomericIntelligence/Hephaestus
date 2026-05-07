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


def get_subpackages(root: Path) -> set[str]:
    """Return the set of direct subpackage names under root."""
    return {
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
    }


def check_subpackage_mirror(src_root: Path, tests_root: Path) -> list[str]:
    """Return error lines for each hephaestus subpackage missing a test dir."""
    src_packages = get_subpackages(src_root)
    test_packages = get_subpackages(tests_root)
    return [
        f"  hephaestus/{name}  →  tests/unit/{name}/ (missing)"
        for name in sorted(src_packages - test_packages)
    ]


def check_scripts_coverage(scripts_root: Path, tests_root: Path) -> list[str]:
    """Return error lines if the scripts/ smoke harness is incomplete."""
    errors: list[str] = []
    smoke_dir = tests_root / "scripts"
    conftest = smoke_dir / "conftest.py"
    smoke_test = smoke_dir / "test_scripts_smoke.py"

    if not conftest.exists():
        errors.append(f"  Missing: tests/unit/scripts/{conftest.name}")
    if not smoke_test.exists():
        errors.append(f"  Missing: tests/unit/scripts/{smoke_test.name}")

    if errors:
        errors.insert(
            0,
            "  The scripts/ smoke harness is required so every scripts/*.py is "
            "auto-tested via --help.",
        )
        return errors

    conftest_text = conftest.read_text(encoding="utf-8")
    if 'glob("*.py")' not in conftest_text and "glob('*.py')" not in conftest_text:
        errors.append(
            "  tests/unit/scripts/conftest.py no longer globs scripts/*.py — "
            "auto-coverage is broken."
        )
    if not any(scripts_root.glob("*.py")):
        errors.append("  No scripts/*.py files found — unexpected.")
    return errors


def main() -> int:
    """Run both structural checks; return non-zero if any fail."""
    repo_root = Path(__file__).parent.parent
    src_root = repo_root / "hephaestus"
    tests_root = repo_root / "tests" / "unit"
    scripts_root = repo_root / "scripts"

    for label, path in (("Source", src_root), ("Test", tests_root), ("Scripts", scripts_root)):
        if not path.exists():
            print(f"ERROR: {label} root not found: {path}", file=sys.stderr)
            return 1

    failed = False

    subpkg_errors = check_subpackage_mirror(src_root, tests_root)
    if subpkg_errors:
        failed = True
        print(
            "ERROR: The following hephaestus subpackages have no corresponding "
            "tests/unit/ directory:",
            file=sys.stderr,
        )
        for line in subpkg_errors:
            print(line, file=sys.stderr)

    scripts_errors = check_scripts_coverage(scripts_root, tests_root)
    if scripts_errors:
        failed = True
        print("ERROR: scripts/ test coverage is incomplete:", file=sys.stderr)
        for line in scripts_errors:
            print(line, file=sys.stderr)

    if failed:
        return 1

    n_subpkgs = len(get_subpackages(src_root))
    n_scripts = sum(1 for _ in scripts_root.glob("*.py"))
    print(
        f"OK: {n_subpkgs} subpackages have test directories; "
        f"{n_scripts} scripts/*.py covered by the smoke harness."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
