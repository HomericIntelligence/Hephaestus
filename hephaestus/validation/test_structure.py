"""Validate unit test directory structure.

Provides two complementary checks:

1. **Mirror check**: Every subpackage in the source directory has a corresponding
   directory under ``tests/unit/``.
2. **No-loose-files check**: No ``test_*.py`` files exist directly under
   ``tests/unit/`` root — they must live in sub-packages that mirror the source
   layout.

Usage::

    hephaestus-check-test-structure
    hephaestus-check-test-structure --src-package mypackage
    hephaestus-check-test-structure --repo-root /path/to/repo --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hephaestus.utils.helpers import get_repo_root

ALLOWED_ROOT_FILES: frozenset[str] = frozenset({"__init__.py", "conftest.py"})


def _get_subpackages(root: Path) -> set[str]:
    """Return the set of direct subpackage names under *root*.

    Ignores directories starting with ``_`` or ``.``.
    """
    if not root.is_dir():
        return set()
    return {
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
    }


def check_test_directory_mirrors(
    src_root: Path,
    test_root: Path,
) -> tuple[bool, set[str]]:
    """Check that every source subpackage has a matching test directory.

    Args:
        src_root: Path to the source package (e.g. ``mypackage/``).
        test_root: Path to the unit test root (e.g. ``tests/unit/``).

    Returns:
        Tuple of ``(all_mirrored, missing_test_dirs)`` where *missing_test_dirs*
        is the set of subpackage names without a corresponding test directory.

    """
    src_packages = _get_subpackages(src_root)
    test_packages = _get_subpackages(test_root)
    missing = src_packages - test_packages
    return len(missing) == 0, missing


def check_no_loose_test_files(
    unit_root: Path,
    allowed_names: frozenset[str] = ALLOWED_ROOT_FILES,
) -> tuple[bool, list[Path]]:
    """Check that no ``test_*.py`` files exist directly under *unit_root*.

    Test files must live in sub-packages that mirror the source layout
    (e.g. ``tests/unit/metrics/``), not at the root of ``tests/unit/``.

    Args:
        unit_root: Root of the unit test directory to inspect.
        allowed_names: Filenames that are allowed at the root level.

    Returns:
        Tuple of ``(no_violations, violating_paths)`` where *violating_paths*
        is a sorted list of files that should be moved into sub-packages.

    """
    if not unit_root.is_dir():
        return True, []

    violations = sorted(p for p in unit_root.glob("test_*.py") if p.name not in allowed_names)
    return len(violations) == 0, violations


def check_test_structure(
    repo_root: Path,
    src_package: str | None = None,
    verbose: bool = False,
) -> bool:
    """Run both test structure checks.

    Args:
        repo_root: Repository root directory.
        src_package: Name of the source package directory. If None, attempts
            auto-detection from ``pyproject.toml``.
        verbose: Print detailed output.

    Returns:
        True if all checks pass, False otherwise.

    """
    if src_package is None:
        src_package = _detect_src_package(repo_root)

    src_root = repo_root / src_package
    test_root = repo_root / "tests" / "unit"
    all_passed = True

    # Check 1: Mirror structure
    if not src_root.is_dir():
        print(f"ERROR: Source root not found: {src_root}", file=sys.stderr)
        return False

    if not test_root.is_dir():
        print(f"ERROR: Test root not found: {test_root}", file=sys.stderr)
        return False

    mirrored, missing = check_test_directory_mirrors(src_root, test_root)
    if mirrored:
        src_count = len(_get_subpackages(src_root))
        if verbose:
            print(f"OK: All {src_count} source subpackages have test directories.")
    else:
        all_passed = False
        print(
            "ERROR: The following source subpackages have no corresponding tests/unit/ directory:",
            file=sys.stderr,
        )
        for name in sorted(missing):
            print(
                f"  {src_package}/{name}  ->  tests/unit/{name}/ (missing)",
                file=sys.stderr,
            )

    # Check 2: No loose test files
    no_loose, violations = check_no_loose_test_files(test_root)
    if no_loose:
        if verbose:
            print("OK: No loose test_*.py files at tests/unit/ root.")
    else:
        all_passed = False
        print(
            "ERROR: test_*.py files found directly under tests/unit/.\n"
            "Move them into the appropriate sub-package.\n"
            "Violation(s):",
            file=sys.stderr,
        )
        for p in violations:
            print(f"  {p}", file=sys.stderr)

    return all_passed


def _detect_src_package(repo_root: Path) -> str:
    """Attempt to detect the source package name from pyproject.toml.

    Falls back to the first directory in *repo_root* that contains an
    ``__init__.py``.
    """
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8")
        # Look for packages = ["name"] in hatch build config
        import re

        match = re.search(r'packages\s*=\s*\["([^"]+)"\]', content)
        if match:
            return match.group(1)

    # Fallback: first dir with __init__.py
    for d in sorted(repo_root.iterdir()):
        if d.is_dir() and (d / "__init__.py").exists():
            if d.name not in {"tests", "scripts", "docs"}:
                return d.name

    return "src"


def main() -> int:
    """CLI entry point for test structure checking.

    Returns:
        Exit code (0 if clean, 1 if violations found).

    """
    parser = argparse.ArgumentParser(
        description="Validate unit test directory structure",
        epilog="Example: %(prog)s --src-package mypackage --verbose",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root directory (default: auto-detect)",
    )
    parser.add_argument(
        "--src-package",
        type=str,
        default=None,
        help="Source package directory name (default: auto-detect from pyproject.toml)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed output",
    )

    args = parser.parse_args()
    repo_root = args.repo_root or get_repo_root()

    passed = check_test_structure(repo_root, src_package=args.src_package, verbose=args.verbose)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
