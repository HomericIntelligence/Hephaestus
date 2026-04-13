"""Detect scripts in ``scripts/`` with no references in CI configs or other scripts.

A script is considered potentially stale if its filename does not appear in any of:

- ``.github/**/*.yml`` (GitHub Actions workflows)
- ``justfile``
- ``.pre-commit-config.yaml``
- other ``scripts/*.py`` files (cross-references)
- ``docs/**/*.md`` (documentation)

Known utility/library scripts (``common.py``, ``conftest.py``, ``__init__.py``) are
excluded from consideration.

Usage::

    hephaestus-check-stale-scripts
    hephaestus-check-stale-scripts --repo-root /path/to/repo --strict
    hephaestus-check-stale-scripts --exclude test_ --verbose

Exit codes:
    0  No stale scripts found (or warnings only without ``--strict``)
    1  Stale scripts detected (only with ``--strict``)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Scripts that are imported by other scripts (not invoked directly) — always active.
_ALWAYS_ACTIVE: frozenset[str] = frozenset(
    {
        "common.py",
        "conftest.py",
        "__init__.py",
        "setup.py",
    }
)

# Name prefixes/substrings that mark a script as always-active (e.g. pytest test files).
_ACTIVE_PATTERNS: tuple[str, ...] = ("test_", "conftest")


def _is_always_active(script_name: str) -> bool:
    """Return True if *script_name* should always be considered active.

    Args:
        script_name: Basename of the script file.

    Returns:
        True if the script is in the known-utilities set or matches an active pattern.

    """
    if script_name in _ALWAYS_ACTIVE:
        return True
    return any(pat in script_name for pat in _ACTIVE_PATTERNS)


def get_all_scripts(
    scripts_dir: Path,
    extensions: tuple[str, ...] = (".py", ".sh", ".mojo"),
) -> list[str]:
    """Return basenames of all script files in *scripts_dir*.

    Args:
        scripts_dir: Path to the ``scripts/`` directory.
        extensions: File suffixes to include.

    Returns:
        Sorted list of basenames.

    """
    return sorted(
        p.name
        for p in scripts_dir.rglob("*")
        if p.is_file() and p.suffix in extensions and not p.name.startswith(".")
    )


def get_reference_targets(repo_root: Path) -> list[Path]:
    """Collect files that may reference script names.

    Includes GitHub Actions workflows, justfile, pre-commit config, other scripts,
    and documentation.

    Args:
        repo_root: Root of the repository.

    Returns:
        List of Path objects for files to search.

    """
    targets: list[Path] = []

    github_dir = repo_root / ".github"
    if github_dir.is_dir():
        targets.extend(github_dir.rglob("*.yml"))

    for name in ("justfile", ".pre-commit-config.yaml"):
        candidate = repo_root / name
        if candidate.is_file():
            targets.append(candidate)

    scripts_dir = repo_root / "scripts"
    if scripts_dir.is_dir():
        targets.extend(scripts_dir.rglob("*.py"))

    docs_dir = repo_root / "docs"
    if docs_dir.is_dir():
        targets.extend(docs_dir.rglob("*.md"))

    return targets


def _script_referenced_by_name(script_name: str, targets: list[Path], own_path: Path) -> bool:
    """Return True if *script_name* appears in at least one target file (not itself).

    Args:
        script_name: Basename to search for.
        targets: Files to search through.
        own_path: Resolved path of the script itself (excluded from search).

    Returns:
        True if an external reference exists.

    """
    for target in targets:
        if target.resolve() == own_path:
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if script_name in content:
            return True
    return False


def _script_referenced_by_import(script_stem: str, targets: list[Path], own_path: Path) -> bool:
    """Return True if *script_stem* appears as a Python import or path reference.

    Catches patterns like ``from scripts.check_stale_scripts import`` or
    ``run scripts/check_stale_scripts``.

    Args:
        script_stem: Stem (name without suffix) of the script.
        targets: Files to search through.
        own_path: Resolved path of the script itself (excluded from search).

    Returns:
        True if an import reference exists.

    """
    pattern = re.compile(r"(?:from|import|run)\s+(?:\w+/)*" + re.escape(script_stem) + r"\b")
    for target in targets:
        if target.resolve() == own_path:
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(content):
            return True
    return False


def find_stale_scripts(
    repo_root: Path,
    exclude_pattern: str | None = None,
) -> list[str]:
    """Return basenames of scripts with no external references.

    Scripts in ``_ALWAYS_ACTIVE`` or matching ``_ACTIVE_PATTERNS`` are excluded.
    If *exclude_pattern* is given, any script whose name contains that substring is
    also excluded.

    Args:
        repo_root: Root of the repository.
        exclude_pattern: Optional substring; matching scripts are excluded.

    Returns:
        Sorted list of possibly-stale script basenames.

    """
    scripts_dir = repo_root / "scripts"
    if not scripts_dir.is_dir():
        return []

    all_scripts = get_all_scripts(scripts_dir)
    targets = get_reference_targets(repo_root)

    stale: list[str] = []
    for script_name in all_scripts:
        if _is_always_active(script_name):
            continue
        if exclude_pattern and exclude_pattern in script_name:
            continue
        own_path = (scripts_dir / script_name).resolve()
        stem = Path(script_name).stem
        referenced = _script_referenced_by_name(
            script_name, targets, own_path
        ) or _script_referenced_by_import(stem, targets, own_path)
        if not referenced:
            stale.append(script_name)

    return stale


def check_stale_scripts(
    repo_root: Path,
    strict: bool = False,
    verbose: bool = False,
    exclude_pattern: str | None = None,
) -> int:
    """Run stale-script detection and return an exit code.

    Args:
        repo_root: Root of the repository.
        strict: If True, return exit code 1 when stale scripts are found.
        verbose: If True, print summary counts before results.
        exclude_pattern: Optional substring; scripts containing it are excluded.

    Returns:
        0 if no stale scripts (or in warning mode), 1 if stale scripts found and
        *strict* is True.

    """
    scripts_dir = repo_root / "scripts"

    stale = find_stale_scripts(repo_root, exclude_pattern=exclude_pattern)

    if verbose and scripts_dir.is_dir():
        all_scripts = get_all_scripts(scripts_dir)
        print(f"Total scripts: {len(all_scripts)}")
        print(f"Stale candidates: {len(stale)}\n")

    if stale:
        prefix = "ERROR" if strict else "WARNING"
        print(f"{prefix}: Found {len(stale)} possibly stale script(s):\n")
        for script_name in stale:
            print(f"  scripts/{script_name}")
        print("\nConsider removing these scripts if they are no longer needed.")
        return 1 if strict else 0

    print("No stale script candidates found.")
    return 0


def main() -> int:
    """CLI entry point for stale-script detection.

    Returns:
        Exit code (0 unless ``--strict`` and stale scripts are found).

    """
    parser = argparse.ArgumentParser(
        description="Detect scripts/ files with no references in CI configs or other scripts",
        epilog="Example: %(prog)s --strict --verbose",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detected via git)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when stale scripts are found (default: warn only, exit 0)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print summary counts before results",
    )
    parser.add_argument(
        "--exclude",
        metavar="PATTERN",
        default=None,
        help="Exclude scripts whose name contains PATTERN (e.g. 'test_')",
    )

    args = parser.parse_args()

    if args.repo_root is not None:
        repo_root: Path = args.repo_root
    else:
        from hephaestus.utils.helpers import get_repo_root

        repo_root = get_repo_root()

    return check_stale_scripts(
        repo_root=repo_root,
        strict=args.strict,
        verbose=args.verbose,
        exclude_pattern=args.exclude,
    )


if __name__ == "__main__":
    sys.exit(main())
