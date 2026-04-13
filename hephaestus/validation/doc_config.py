"""Enforce consistency between documentation metric values and authoritative config sources.

Checks that values documented in CLAUDE.md and README.md match what is configured in
``pyproject.toml``:

1. Coverage threshold in CLAUDE.md matches ``fail_under`` in ``[tool.coverage.report]``.
2. ``--cov=<path>`` in README.md matches ``addopts`` in ``[tool.pytest.ini_options]``.
3. If ``--cov-fail-under=N`` is present in ``addopts``, it must match ``fail_under`` in
   ``[tool.coverage.report]``.  Absent is OK — ``[tool.coverage.report].fail_under`` is
   the single source of truth.
4. Test count in README.md is within 10% of actual ``pytest --collect-only`` count.

Usage::

    hephaestus-check-doc-config
    hephaestus-check-doc-config --repo-root /path/to/repo --verbose

Exit codes:
    0  All checks pass
    1  One or more checks failed
"""

from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

_tomllib = None
for _mod_name in ("tomllib", "tomli"):
    try:
        _tomllib = importlib.import_module(_mod_name)
        break
    except ModuleNotFoundError:
        continue


def _load_pyproject(repo_root: Path) -> dict[str, Any]:
    """Load ``pyproject.toml`` using tomllib/tomli.

    Args:
        repo_root: Root directory of the repository.

    Returns:
        Parsed TOML data as a nested dict.

    Raises:
        SystemExit: With code 1 if the file is missing, unreadable, or tomllib is
            unavailable.

    """
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        print(f"ERROR: pyproject.toml not found: {pyproject_path}", file=sys.stderr)
        sys.exit(1)

    if _tomllib is None:
        print(
            "ERROR: tomllib/tomli is required to parse pyproject.toml. "
            "Install tomli for Python < 3.11: pip install tomli",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with open(pyproject_path, "rb") as f:
            return cast(dict[str, Any], _tomllib.load(f))
    except Exception as exc:
        print(f"ERROR: Could not parse {pyproject_path}: {exc}", file=sys.stderr)
        sys.exit(1)


def load_coverage_threshold(repo_root: Path) -> int:
    """Read ``fail_under`` from ``[tool.coverage.report]`` in ``pyproject.toml``.

    Args:
        repo_root: Repository root containing ``pyproject.toml``.

    Returns:
        Integer value of ``fail_under``.

    Raises:
        SystemExit: If the key is absent or ``pyproject.toml`` is unreadable.

    """
    data = _load_pyproject(repo_root)
    try:
        threshold = data["tool"]["coverage"]["report"]["fail_under"]
    except KeyError:
        print(
            "ERROR: [tool.coverage.report].fail_under not found in pyproject.toml",
            file=sys.stderr,
        )
        sys.exit(1)
    return int(threshold)


def extract_cov_path(repo_root: Path) -> str:
    """Read the ``--cov=<path>`` value from ``[tool.pytest.ini_options].addopts``.

    Args:
        repo_root: Repository root containing ``pyproject.toml``.

    Returns:
        Package path string (e.g. ``"hephaestus"``).

    Raises:
        SystemExit: If the key is missing or no ``--cov=`` flag is found.

    """
    data = _load_pyproject(repo_root)
    addopts = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", [])
    addopts_items: list[str] = addopts.split() if isinstance(addopts, str) else list(addopts)

    for item in addopts_items:
        m = re.match(r"^--cov=(.+)$", item)
        if m:
            return m.group(1)

    print(
        "ERROR: No --cov=<path> found in [tool.pytest.ini_options].addopts",
        file=sys.stderr,
    )
    sys.exit(1)


def extract_cov_fail_under_from_addopts(repo_root: Path) -> int | None:
    """Read ``--cov-fail-under=N`` from ``[tool.pytest.ini_options].addopts``, if present.

    Args:
        repo_root: Repository root containing ``pyproject.toml``.

    Returns:
        Integer threshold if the flag is present, ``None`` otherwise.

    """
    data = _load_pyproject(repo_root)
    addopts = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", [])
    addopts_items: list[str] = addopts.split() if isinstance(addopts, str) else list(addopts)

    for item in addopts_items:
        m = re.match(r"^--cov-fail-under=(\d+)$", item)
        if m:
            return int(m.group(1))
    return None


def check_claude_md_threshold(repo_root: Path, expected: int) -> list[str]:
    """Check that CLAUDE.md documents the correct coverage threshold.

    Searches for ``<N>%+ test coverage`` (or ``<N>% test coverage``) and verifies
    the integer matches *expected*.

    Args:
        repo_root: Repository root.
        expected: Authoritative threshold value from ``pyproject.toml``.

    Returns:
        List of error strings (empty if all checks pass).

    """
    claude_md = repo_root / "CLAUDE.md"
    if not claude_md.exists():
        return [f"CLAUDE.md not found at {claude_md}"]

    text = claude_md.read_text(encoding="utf-8")
    matches = re.findall(r"(\d+)%\+?\s+test coverage", text)
    if not matches:
        return [
            "CLAUDE.md: No coverage threshold mention found "
            "(expected pattern: '<N>%+ test coverage')"
        ]

    errors: list[str] = []
    for raw in matches:
        found = int(raw)
        if found != expected:
            errors.append(
                f"CLAUDE.md: Coverage threshold mismatch — "
                f"CLAUDE.md says {found}%, pyproject.toml says {expected}%"
            )
    return errors


def check_readme_cov_path(repo_root: Path, expected_path: str) -> list[str]:
    """Check that all ``--cov=<path>`` occurrences in README.md match *expected_path*.

    Args:
        repo_root: Repository root.
        expected_path: Authoritative ``--cov`` path from ``pyproject.toml``.

    Returns:
        List of error strings (empty if all checks pass or README has no ``--cov``).

    """
    readme = repo_root / "README.md"
    if not readme.exists():
        return [f"README.md not found at {readme}"]

    text = readme.read_text(encoding="utf-8")
    occurrences = re.findall(r"--cov=(\S+)", text)
    if not occurrences:
        return []

    errors: list[str] = []
    for path in occurrences:
        if path != expected_path:
            errors.append(
                f"README.md: --cov path mismatch — "
                f"README.md has '--cov={path}', pyproject.toml uses '--cov={expected_path}'"
            )
    return errors


def check_addopts_cov_fail_under(repo_root: Path, expected: int) -> list[str]:
    """Check that ``--cov-fail-under`` in addopts matches ``fail_under`` (if present).

    If absent, the check passes — ``[tool.coverage.report].fail_under`` is the single
    source of truth and pytest-cov reads it directly.

    Args:
        repo_root: Repository root.
        expected: Authoritative threshold from ``[tool.coverage.report].fail_under``.

    Returns:
        List of error strings (empty if consistent or flag is absent).

    """
    addopts_threshold = extract_cov_fail_under_from_addopts(repo_root)
    if addopts_threshold is None:
        return []
    if addopts_threshold != expected:
        return [
            f"pyproject.toml: --cov-fail-under mismatch — "
            f"addopts has --cov-fail-under={addopts_threshold}, "
            f"but [tool.coverage.report].fail_under={expected}"
        ]
    return []


def collect_actual_test_count(repo_root: Path) -> int | None:
    """Run ``pytest --collect-only -q`` and return the number of collected tests.

    Args:
        repo_root: Repository root where pytest should be invoked.

    Returns:
        Integer test count, or ``None`` if collection fails or is unparseable.

    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None

    output = result.stdout + result.stderr
    m = re.search(r"(\d+)\s+(?:tests?\s+)?(?:selected|collected)", output)
    if m:
        count = int(m.group(1))
        return count if count > 0 else None
    return None


def check_readme_test_count(
    repo_root: Path, actual_count: int, tolerance: float = 0.10
) -> list[str]:
    """Check that test count claims in README.md are within *tolerance* of *actual_count*.

    Searches for patterns like ``3,500+ tests`` or ``3172 tests``.

    Args:
        repo_root: Repository root.
        actual_count: Authoritative test count from ``pytest --collect-only``.
        tolerance: Fractional tolerance (default 0.10 = 10%).

    Returns:
        List of error strings (empty if consistent or no hardcoded count found).

    """
    readme = repo_root / "README.md"
    if not readme.exists():
        return [f"README.md not found at {readme}"]

    text = readme.read_text(encoding="utf-8")
    raw_matches = re.findall(r"(\d[\d,]*)\+?\s+tests?", text, re.IGNORECASE)
    if not raw_matches:
        return []

    errors: list[str] = []
    for raw in raw_matches:
        doc_count = int(raw.replace(",", ""))
        if abs(doc_count - actual_count) / actual_count > tolerance:
            errors.append(
                f"README.md: Test count mismatch — "
                f"README.md says {doc_count}, actual pytest count is {actual_count} "
                f"(tolerance: {int(tolerance * 100)}%)"
            )
    return errors


def check_doc_config_consistency(
    repo_root: Path,
    verbose: bool = False,
    skip_test_count: bool = False,
) -> int:
    """Run all doc/config consistency checks and return an exit code.

    Checks:
    1. CLAUDE.md coverage threshold vs ``[tool.coverage.report].fail_under``.
    2. README.md ``--cov=<path>`` vs ``[tool.pytest.ini_options].addopts``.
    3. ``--cov-fail-under`` in addopts (if present) vs ``fail_under``.
    4. README.md hardcoded test count vs ``pytest --collect-only`` (skipped if
       *skip_test_count* is True or pytest is unavailable).

    Args:
        repo_root: Repository root containing ``pyproject.toml``.
        verbose: If True, print passing check names as well.
        skip_test_count: If True, skip the live ``pytest --collect-only`` check.

    Returns:
        0 if all checks pass, 1 if any fail.

    """
    all_errors: list[str] = []

    expected_threshold = load_coverage_threshold(repo_root)
    all_errors.extend(_run_threshold_check(repo_root, expected_threshold, verbose))
    all_errors.extend(_run_cov_path_check(repo_root, verbose))
    all_errors.extend(_run_addopts_check(repo_root, expected_threshold, verbose))

    if not skip_test_count:
        all_errors.extend(_run_test_count_check(repo_root, verbose))

    if all_errors:
        for error in all_errors:
            print(error, file=sys.stderr)
        print(
            f"\nFound {len(all_errors)} doc/config consistency violation(s).",
            file=sys.stderr,
        )
        return 1
    return 0


def _run_threshold_check(repo_root: Path, expected: int, verbose: bool) -> list[str]:
    """Run CLAUDE.md coverage threshold check and print verbose result.

    Args:
        repo_root: Repository root.
        expected: Expected threshold integer.
        verbose: Print pass message if True.

    Returns:
        List of error strings (empty if passing).

    """
    errors = check_claude_md_threshold(repo_root, expected)
    if not errors and verbose:
        print(f"PASS: CLAUDE.md coverage threshold matches pyproject.toml ({expected}%)")
    return errors


def _run_cov_path_check(repo_root: Path, verbose: bool) -> list[str]:
    """Run README.md --cov path check and print verbose result.

    Args:
        repo_root: Repository root.
        verbose: Print pass message if True.

    Returns:
        List of error strings (empty if passing).

    """
    expected_cov_path = extract_cov_path(repo_root)
    errors = check_readme_cov_path(repo_root, expected_cov_path)
    if not errors and verbose:
        print(f"PASS: README.md --cov path matches pyproject.toml (--cov={expected_cov_path})")
    return errors


def _run_addopts_check(repo_root: Path, expected: int, verbose: bool) -> list[str]:
    """Run addopts --cov-fail-under consistency check and print verbose result.

    Args:
        repo_root: Repository root.
        expected: Expected threshold integer.
        verbose: Print pass message if True.

    Returns:
        List of error strings (empty if passing).

    """
    errors = check_addopts_cov_fail_under(repo_root, expected)
    if not errors and verbose:
        addopts_val = extract_cov_fail_under_from_addopts(repo_root)
        if addopts_val is not None:
            print(
                f"PASS: addopts --cov-fail-under matches "
                f"[tool.coverage.report].fail_under ({expected}%)"
            )
        else:
            print(
                f"PASS: No --cov-fail-under in addopts — "
                f"[tool.coverage.report].fail_under ({expected}%) is single source of truth"
            )
    return errors


def _run_test_count_check(repo_root: Path, verbose: bool) -> list[str]:
    """Run pytest test-count check and print verbose result.

    Args:
        repo_root: Repository root.
        verbose: Print pass/skip message if True.

    Returns:
        List of error strings (empty if passing or skipped).

    """
    actual_count = collect_actual_test_count(repo_root)
    if actual_count is None:
        if verbose:
            print("SKIP: Could not collect actual test count (pytest unavailable)")
        return []
    errors = check_readme_test_count(repo_root, actual_count)
    if not errors and verbose:
        print(f"PASS: README.md test count is within 10% of actual ({actual_count})")
    return errors


def main() -> int:
    """CLI entry point for doc/config consistency checking.

    Returns:
        Exit code (0 if all checks pass, 1 if any fail).

    """
    parser = argparse.ArgumentParser(
        description="Enforce consistency between doc metric values and pyproject.toml",
        epilog="Example: %(prog)s --repo-root /path/to/repo --verbose",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detected via git)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print passing check names",
    )
    parser.add_argument(
        "--skip-test-count",
        action="store_true",
        help="Skip the live pytest --collect-only test count check",
    )

    args = parser.parse_args()

    if args.repo_root is not None:
        repo_root: Path = args.repo_root
    else:
        from hephaestus.utils.helpers import get_repo_root

        repo_root = get_repo_root()

    return check_doc_config_consistency(
        repo_root=repo_root,
        verbose=args.verbose,
        skip_test_count=args.skip_test_count,
    )


if __name__ == "__main__":
    sys.exit(main())
