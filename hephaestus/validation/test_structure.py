"""Validate unit test directory structure.

Provides six complementary checks:

1. **Mirror check**: Every subpackage in the source directory has a corresponding
   directory under ``tests/unit/``.
2. **No-loose-files check**: No ``test_*.py`` files exist directly under
   ``tests/unit/`` root — they must live in sub-packages that mirror the source
   layout.
3. **No-unsanctioned-dirs check**: Every directory under ``tests/unit/`` either
   mirrors a source subpackage or is in the ``SANCTIONED_EXTRA_TEST_DIRS``
   allowlist (for non-package targets like top-level ``scripts/``/``docs/``).
4. **No-ghost-packages check**: No ``tests/unit/<name>/`` dir mirrors a
   ``hephaestus/<name>/`` subpackage where both are content-free (source has
   no module beyond ``__init__.py`` AND the test dir has no ``test_*.py``) —
   the name-only mirror check would otherwise treat the pair as valid.
5. **No-phantom-test-dirs check**: No direct ``tests/unit/<name>/`` dir is
   content-free, such as a stale directory containing only ``__pycache__``.
6. **No-stray-tests-root-files check**: No ``test_*.py`` files exist directly
   under ``tests/`` root. Such files fall outside
   ``testpaths = ["tests/unit", "tests/integration"]`` and are silently never
   collected by pytest (issue #1467).

Usage::

    hephaestus-check-test-structure
    hephaestus-check-test-structure --src-package mypackage
    hephaestus-check-test-structure --repo-root /path/to/repo --verbose
"""

from __future__ import annotations

import sys
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, emit_json_status, resolve_repo_root

ALLOWED_ROOT_FILES: frozenset[str] = frozenset({"__init__.py", "conftest.py"})

# Test subdirectories that intentionally have NO hephaestus/ subpackage
# counterpart because they cover non-package targets. Each must name its target.
# (Distinct axis from _detect_src_package's `skip` set, which excludes top-level
# dirs during SOURCE-package detection; this set allowlists TEST dirs.)
SANCTIONED_EXTRA_TEST_DIRS: frozenset[str] = frozenset(
    {
        "constants",  # -> hephaestus/constants.py (module, not a subpackage)
        "docs",  # -> top-level docs/ tree
        "plugins",  # -> top-level Codex plugin marketplace wrapper
        "scripts",  # -> top-level scripts/*.py
        "shell",  # -> shell installer scripts
    }
)


def _has_python_source(directory: Path) -> bool:
    """Return True if *directory* directly contains a Python source file.

    A package directory must hold an ``__init__.py`` or at least one ``*.py``
    module. A directory whose only remaining content is ``__pycache__`` (stale
    ``.pyc`` files left after the source was deleted) is a *ghost* package and
    must NOT be treated as a subpackage — doing so implies an importable
    subpackage that does not exist (POLA).
    """
    if (directory / "__init__.py").exists():
        return True
    return any(directory.glob("*.py"))


def _get_subpackages(root: Path) -> set[str]:
    """Return the set of direct subpackage names under *root*.

    Ignores directories starting with ``_`` or ``.``, and ghost directories
    that contain no Python source file (only ``__pycache__``/``.pyc``).
    """
    if not root.is_dir():
        return set()
    return {
        d.name
        for d in root.iterdir()
        if d.is_dir()
        and not d.name.startswith("_")
        and not d.name.startswith(".")
        and _has_python_source(d)
    }


def _has_source_modules(pkg_dir: Path) -> bool:
    """Return True if *pkg_dir* holds at least one module beyond ``__init__.py``."""
    if not pkg_dir.is_dir():
        return False
    return any(p.name != "__init__.py" for p in pkg_dir.glob("*.py"))


def _has_test_files(test_dir: Path) -> bool:
    """Return True if *test_dir* holds at least one ``test_*.py`` file."""
    if not test_dir.is_dir():
        return False
    return any(test_dir.glob("test_*.py"))


def check_no_ghost_packages(
    src_root: Path,
    test_root: Path,
) -> tuple[bool, set[str]]:
    """Flag *ghost* mirror pairs the name-only mirror check misses.

    A name-only mirror (:func:`check_test_directory_mirrors`) passes when a
    ``tests/unit/<name>/`` dir and a ``<src>/<name>/`` dir share a name — even
    if BOTH are content-free. Such a pair (source has no module beyond
    ``__init__.py`` AND the test dir has no ``test_*.py``) is a false-positive
    "valid mirror" that hides that neither side has any content. This detects
    that case directly.

    Args:
        src_root: Path to the source package (e.g. ``hephaestus/``).
        test_root: Path to the unit test root (e.g. ``tests/unit/``).

    Returns:
        Tuple of ``(ok, ghosts)`` where *ghosts* is the set of subpackage names
        whose source dir has no module AND whose test dir has no ``test_*.py``.

    """
    shared = _get_subpackages(src_root) & _get_subpackages(test_root)
    ghosts = {
        name
        for name in shared
        if not _has_source_modules(src_root / name) and not _has_test_files(test_root / name)
    }
    return len(ghosts) == 0, ghosts


def check_no_phantom_test_dirs(test_root: Path) -> tuple[bool, set[str]]:
    """Flag direct ``tests/unit`` dirs with no Python source at all.

    This catches stale skeletons such as ``tests/unit/git/`` containing only
    ``__pycache__`` after the corresponding tests were removed.
    """
    if not test_root.is_dir():
        return True, set()

    phantoms = {
        d.name
        for d in test_root.iterdir()
        if d.is_dir()
        and not d.name.startswith("_")
        and not d.name.startswith(".")
        and not _has_python_source(d)
    }
    return len(phantoms) == 0, phantoms


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


def check_no_stray_tests_root_files(
    tests_root: Path,
    allowed_names: frozenset[str] = ALLOWED_ROOT_FILES,
) -> tuple[bool, list[Path]]:
    """Check that no ``test_*.py`` files exist directly under ``tests/`` root.

    A ``test_*.py`` file at ``tests/`` (above ``tests/unit/``) falls outside
    ``testpaths = ["tests/unit", "tests/integration"]`` and is silently never
    collected by pytest — the exact regression in issue #1467. Such files must
    live under ``tests/unit/`` or ``tests/integration/`` in a mirroring
    sub-package.

    Args:
        tests_root: Root of the ``tests/`` directory to inspect.
        allowed_names: Filenames that are allowed at the ``tests/`` root level.

    Returns:
        Tuple of ``(no_violations, violating_paths)`` where *violating_paths*
        is a sorted list of files that should be moved under ``tests/unit/`` or
        ``tests/integration/``.

    """
    if not tests_root.is_dir():
        return True, []

    violations = sorted(p for p in tests_root.glob("test_*.py") if p.name not in allowed_names)
    return len(violations) == 0, violations


def check_no_unsanctioned_test_dirs(
    src_root: Path,
    test_root: Path,
    sanctioned: frozenset[str] = SANCTIONED_EXTRA_TEST_DIRS,
) -> tuple[bool, set[str]]:
    """Check that every tests/unit/ subdir mirrors a source subpackage or is allowlisted.

    Guards the *reverse* of :func:`check_test_directory_mirrors`: a test
    directory with no corresponding source subpackage breaks the mirror
    invariant unless it is in *sanctioned* (covering a non-package target).

    Args:
        src_root: Path to the source package (e.g. ``mypackage/``).
        test_root: Path to the unit test root (e.g. ``tests/unit/``).
        sanctioned: Allowlist of test directory names that intentionally have no
            source subpackage counterpart.

    Returns:
        Tuple of ``(ok, unsanctioned)`` where *unsanctioned* is the set of test
        directory names that neither mirror a source subpackage nor are allowlisted.

    """
    src_packages = _get_subpackages(src_root)
    test_packages = _get_subpackages(test_root)
    unsanctioned = test_packages - src_packages - sanctioned
    return len(unsanctioned) == 0, unsanctioned


def check_scripts_coverage(
    scripts_root: Path,
    test_root: Path,
) -> tuple[bool, list[str]]:
    """Check the ``scripts/*.py`` smoke harness is present and auto-discovering.

    The ``tests/unit/scripts/`` smoke harness globs ``scripts/*.py`` so every
    script is exercised via a single ``--help`` test. This verifies the harness
    files exist and the glob marker is intact, so a refactor that silently breaks
    auto-discovery is caught.

    Args:
        scripts_root: Path to the top-level ``scripts/`` directory.
        test_root: Path to the unit test root (e.g. ``tests/unit/``).

    Returns:
        Tuple of ``(ok, error_lines)`` where *error_lines* describes each
        coverage gap (empty when the harness is healthy).

    """
    errors: list[str] = []
    smoke_dir = test_root / "scripts"
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
        return False, errors

    conftest_text = conftest.read_text(encoding="utf-8")
    if 'glob("*.py")' not in conftest_text and "glob('*.py')" not in conftest_text:
        errors.append(
            "  tests/unit/scripts/conftest.py no longer globs scripts/*.py — "
            "auto-coverage is broken."
        )
    if not any(scripts_root.glob("*.py")):
        errors.append("  No scripts/*.py files found — unexpected.")
    return len(errors) == 0, errors


def _report_mirror_check(
    src_root: Path,
    test_root: Path,
    src_package: str,
    verbose: bool,
) -> bool:
    mirrored, missing = check_test_directory_mirrors(src_root, test_root)
    if mirrored:
        if verbose:
            src_count = len(_get_subpackages(src_root))
            print(f"OK: All {src_count} source subpackages have test directories.")
        return True
    print(
        "ERROR: The following source subpackages have no corresponding tests/unit/ directory:",
        file=sys.stderr,
    )
    for name in sorted(missing):
        print(f"  {src_package}/{name}  ->  tests/unit/{name}/ (missing)", file=sys.stderr)
    return False


def _report_loose_files_check(test_root: Path, verbose: bool) -> bool:
    no_loose, violations = check_no_loose_test_files(test_root)
    if no_loose:
        if verbose:
            print("OK: No loose test_*.py files at tests/unit/ root.")
        return True
    print(
        "ERROR: test_*.py files found directly under tests/unit/.\n"
        "Move them into the appropriate sub-package.\n"
        "Violation(s):",
        file=sys.stderr,
    )
    for p in violations:
        print(f"  {p}", file=sys.stderr)
    return False


def _report_unsanctioned_dirs_check(
    src_root: Path,
    test_root: Path,
    verbose: bool,
) -> bool:
    ok_extra, unsanctioned = check_no_unsanctioned_test_dirs(src_root, test_root)
    if ok_extra:
        if verbose:
            print("OK: No unsanctioned extra test directories under tests/unit/.")
        return True
    print(
        "ERROR: tests/unit/ has directories with no source subpackage and no\n"
        "allowlist entry. Add a SANCTIONED_EXTRA_TEST_DIRS entry (with a target\n"
        "comment) in hephaestus/validation/test_structure.py, or remove the dir.\n"
        "Unsanctioned:",
        file=sys.stderr,
    )
    for name in sorted(unsanctioned):
        print(f"  tests/unit/{name}/", file=sys.stderr)
    return False


def _report_ghost_packages_check(
    src_root: Path,
    test_root: Path,
    verbose: bool,
) -> bool:
    ok, ghosts = check_no_ghost_packages(src_root, test_root)
    if ok:
        if verbose:
            print("OK: No ghost (content-free) mirror directories.")
        return True
    print(
        "ERROR: tests/unit/ has directories mirroring a source subpackage where\n"
        "BOTH the source package (no module beyond __init__.py) and the test dir\n"
        "(no test_*.py) are content-free. Remove both ghost dirs, or add real\n"
        "content.\nGhost(s):",
        file=sys.stderr,
    )
    for name in sorted(ghosts):
        print(
            f"  hephaestus/{name}/ (no modules)  <->  tests/unit/{name}/ (no tests)",
            file=sys.stderr,
        )
    return False


def _report_phantom_test_dirs_check(test_root: Path, verbose: bool) -> bool:
    ok, phantoms = check_no_phantom_test_dirs(test_root)
    if ok:
        if verbose:
            print("OK: No phantom test directories under tests/unit/.")
        return True

    print(
        "ERROR: tests/unit/ has content-free directories. Remove stale dirs, "
        "or add real test files if the directory is intentional.\nPhantom(s):",
        file=sys.stderr,
    )
    for name in sorted(phantoms):
        print(f"  tests/unit/{name}/ (no Python source)", file=sys.stderr)
    return False


def _report_stray_tests_root_files_check(tests_root: Path, verbose: bool) -> bool:
    no_stray, violations = check_no_stray_tests_root_files(tests_root)
    if no_stray:
        if verbose:
            print("OK: No stray test_*.py files at tests/ root.")
        return True
    print(
        "ERROR: test_*.py files found directly under tests/ (outside testpaths).\n"
        "These are silently never collected by pytest. Move them into\n"
        "tests/unit/ or tests/integration/ under a mirroring sub-package.\n"
        "Violation(s):",
        file=sys.stderr,
    )
    for p in violations:
        print(f"  {p}", file=sys.stderr)
    return False


def check_test_structure(
    repo_root: Path,
    src_package: str | None = None,
    verbose: bool = False,
) -> bool:
    """Run all test structure checks.

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
    tests_root = repo_root / "tests"
    test_root = tests_root / "unit"

    if not src_root.is_dir():
        print(f"ERROR: Source root not found: {src_root}", file=sys.stderr)
        return False

    if not test_root.is_dir():
        print(f"ERROR: Test root not found: {test_root}", file=sys.stderr)
        return False

    results = [
        _report_mirror_check(src_root, test_root, src_package, verbose),
        _report_loose_files_check(test_root, verbose),
        _report_unsanctioned_dirs_check(src_root, test_root, verbose),
        _report_ghost_packages_check(src_root, test_root, verbose),
        _report_phantom_test_dirs_check(test_root, verbose),
        _report_stray_tests_root_files_check(tests_root, verbose),
    ]
    return all(results)


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
    skip = {"tests", "scripts", "docs"}
    for d in sorted(repo_root.iterdir()):
        if d.is_dir() and (d / "__init__.py").exists() and d.name not in skip:
            return d.name

    return "src"


def main() -> int:
    """CLI entry point for test structure checking.

    Returns:
        Exit code (0 if clean, 1 if violations found).

    """
    parser = create_validation_parser(
        "Validate unit test directory structure",
        epilog="Example: %(prog)s --src-package mypackage --verbose",
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
    repo_root = resolve_repo_root(args)

    passed = check_test_structure(repo_root, src_package=args.src_package, verbose=args.verbose)
    exit_code = 0 if passed else 1
    if args.json:
        emit_json_status(exit_code, passed=passed)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
