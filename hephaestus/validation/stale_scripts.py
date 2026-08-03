"""Detect scripts in ``scripts/`` with no usage references.

A script is considered potentially stale if its filename does not appear in any of:

- GitHub Actions workflows and repository configuration
- root documentation and ``docs/``
- package source, tests, and other scripts (cross-references)

``scripts/README.md`` is deliberately excluded. Its complete inventory is a
documentation invariant, not evidence that an operator or automation invokes
a script. This command reports leads for lifecycle review; it does not prove
that a script is safe to remove.

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

import re
import sys
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, format_output, resolve_repo_root

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
    """Return paths relative to *scripts_dir* for all script files.

    Args:
        scripts_dir: Path to the ``scripts/`` directory.
        extensions: File suffixes to include.

    Returns:
        Sorted POSIX paths relative to *scripts_dir*.

    """
    return sorted(
        p.relative_to(scripts_dir).as_posix()
        for p in scripts_dir.rglob("*")
        if p.is_file() and p.suffix in extensions and not p.name.startswith(".")
    )


def get_reference_targets(repo_root: Path) -> list[Path]:
    """Collect files that may reference script names.

    Includes workflows, root configuration and documentation, package source,
    tests, other scripts, and documentation. The scripts catalog is excluded
    because merely listing a script must not suppress a lifecycle-review lead.

    Args:
        repo_root: Root of the repository.

    Returns:
        List of Path objects for files to search.

    """
    targets: list[Path] = []

    for name in (
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "justfile",
        "pyproject.toml",
        ".pre-commit-config.yaml",
    ):
        candidate = repo_root / name
        if candidate.is_file():
            targets.append(candidate)

    for directory in (".github", "docs", "hephaestus", "tests", "scripts"):
        candidate = repo_root / directory
        if candidate.is_dir():
            targets.extend(path for path in candidate.rglob("*") if path.is_file())

    non_usage_documents = {repo_root / "scripts" / "README.md"}
    return sorted({target for target in targets if target not in non_usage_documents})


def _script_referenced_by_name(script_path: str, targets: list[Path], own_path: Path) -> bool:
    """Return True if *script_name* appears in at least one target file (not itself).

    Args:
        script_path: Path relative to ``scripts/`` to search for.
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
        if script_path in content:
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
    for script_path in all_scripts:
        script_name = Path(script_path).name
        if _is_always_active(script_name):
            continue
        if exclude_pattern and exclude_pattern in script_path:
            continue
        own_path = (scripts_dir / script_path).resolve()
        stem = Path(script_name).stem
        referenced = _script_referenced_by_name(
            script_path, targets, own_path
        ) or _script_referenced_by_import(stem, targets, own_path)
        if not referenced:
            stale.append(script_path)

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
    parser = create_validation_parser(
        "Detect scripts/ files with no references in CI configs or other scripts",
        epilog="Example: %(prog)s --strict --verbose",
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
    repo_root = resolve_repo_root(args)

    if args.json:
        stale = find_stale_scripts(repo_root, exclude_pattern=args.exclude)
        exit_code = 1 if (stale and args.strict) else 0
        report = {
            "stale_scripts": stale,
            "stale_count": len(stale),
            "strict": args.strict,
            "exit_code": exit_code,
            "passed": not stale,
        }
        print(format_output(report, "json"))
        return exit_code

    return check_stale_scripts(
        repo_root=repo_root,
        strict=args.strict,
        verbose=args.verbose,
        exclude_pattern=args.exclude,
    )


if __name__ == "__main__":
    sys.exit(main())
