#!/usr/bin/env python3

"""Version consistency checks and atomic version bumping.

The project uses hatch-vcs dynamic versioning: the **canonical version is
derived from git tags**, not stored in any file. These checks therefore compare
secondary version declarations against the git-derived canonical version.

Provides three operations:

1. ``check_version_consistency`` — verify that a canonical git-derived version
   can be resolved.

2. ``check_package_version_consistency`` — broader multi-source scan of
   ``__init__.py`` ``__version__`` and optional skill markdown files.

3. ``bump_version`` — compute the next semver string by incrementing a chosen part
   (major/minor/patch), then delegate writes to :class:`~hephaestus.version.manager.VersionManager`.

Usage:
    hephaestus-check-version-consistency [--repo-root PATH] [--verbose]
    hephaestus-check-package-versions [--repo-root PATH] [--scan-skills] [--verbose]
    hephaestus-bump-version {major,minor,patch} [--repo-root PATH] [--dry-run] [--verbose]
"""

import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

from hephaestus.cli.utils import (
    create_validation_parser,
    emit_json_status,
    format_output,
    resolve_repo_root,
)
from hephaestus.version.manager import VersionManager, parse_version
from hephaestus.version.parsing import parse_version_tuple

# PyPI distribution name used for the importlib.metadata fallback.
_DIST_NAME = "HomericIntelligence-Hephaestus"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches semver-ish: v1.5.0, 1.5.0, etc.
# Negative lookbehinds exclude URL paths (/en/1.0.0/) and GH Action pins (@v0.8.1).
_VERSION_RE = re.compile(r"(?<!/)(?<!@)\bv?(\d+\.\d+\.\d+)\b")

# Matches inline code spans so we skip versions inside backticks.
_INLINE_CODE_RE = re.compile(r"``[^`]+``|`[^`]+`")


def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Parse ``"X.Y.Z"`` into a comparable tuple of ints.

    Splits on ``.`` and requires every segment to be an integer; raises on any
    non-numeric segment. Inputs here are always pre-validated ``X.Y.Z`` strings.

    Args:
        version_str: A semver string like ``"1.2.3"``.

    Returns:
        A tuple such as ``(1, 2, 3)``.

    """
    return parse_version_tuple(version_str, on_non_numeric="raise")


def _version_from_git_tag(repo_root: Path) -> str | None:
    """Return the latest semver git tag (without a leading ``v``), or None.

    This is the same authority hatch-vcs uses to compute the dynamic version.

    Args:
        repo_root: Repository root directory.

    Returns:
        A ``"X.Y.Z"`` string, or ``None`` if no matching tag exists or git is
        unavailable.

    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    tag = result.stdout.strip().lstrip("v")
    match = re.match(r"^\d+\.\d+\.\d+", tag)
    return match.group(0) if match else None


def _version_from_metadata() -> str | None:
    """Return the installed distribution's base version (no dev/local suffix), or None.

    Returns:
        A ``"X.Y.Z"`` string, or ``None`` if the package is not installed.

    """
    try:
        raw = _dist_version(_DIST_NAME)
    except PackageNotFoundError:
        return None
    match = re.match(r"^\d+\.\d+\.\d+", raw)
    return match.group(0) if match else None


def _get_canonical_version(repo_root: Path) -> str:
    """Return the canonical project version.

    Under hatch-vcs dynamic versioning the canonical version is derived from git
    tags. This prefers the latest ``v*`` git tag and falls back to the installed
    distribution metadata.

    Args:
        repo_root: Repository root directory.

    Returns:
        The canonical version string, e.g. ``"0.9.0"``.

    Raises:
        SystemExit: With code 1 if no canonical version can be determined.

    """
    version = _version_from_git_tag(repo_root) or _version_from_metadata()
    if version is None:
        print(
            "ERROR: could not determine the canonical version.\n"
            "  This project uses hatch-vcs dynamic versioning; a vX.Y.Z git tag\n"
            "  or an installed distribution is required.",
            file=sys.stderr,
        )
        sys.exit(1)
    return version


def _strip_inline_code(line: str) -> str:
    """Replace inline code spans with whitespace so embedded versions are ignored.

    Args:
        line: A single line of text.

    Returns:
        The line with inline code contents replaced by spaces.

    """
    return _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)


def _find_aspirational_versions(
    file_path: Path,
    canonical_tuple: tuple[int, ...],
    label: str,
) -> list[str]:
    """Find version references in a file that exceed the canonical version.

    Skips versions inside fenced code blocks and inline code spans; those
    typically reference external tool versions rather than the project's own.

    Args:
        file_path: Path to scan.
        canonical_tuple: The canonical version as a comparable int tuple.
        label: Human-readable label for error messages.

    Returns:
        List of error strings, one per aspirational version found.

    """
    content = file_path.read_text(encoding="utf-8")
    errors: list[str] = []
    in_code_block = False

    for line_num, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        scannable = _strip_inline_code(line)
        for match in _VERSION_RE.finditer(scannable):
            version_str = match.group(1)
            version_tuple = _parse_version_tuple(version_str)
            if version_tuple > canonical_tuple:
                canonical_str = ".".join(str(p) for p in canonical_tuple)
                errors.append(
                    f"{label}:{line_num}: aspirational version reference "
                    f"'v{version_str}' exceeds canonical version '{canonical_str}'"
                )

    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_version_consistency(repo_root: Path, verbose: bool = False) -> int:
    """Verify the project can resolve its canonical git-derived version.

    Args:
        repo_root: Root directory of the repository.
        verbose: If True, print versions even when they match.

    Returns:
        0 when the canonical version can be resolved.

    """
    canonical_version = _get_canonical_version(repo_root)
    if verbose:
        print(f"Canonical version (git tag / metadata): {canonical_version}")
    return 0


def _check_init_version_errors(
    package_init: Path | None, canonical: str, verbose: bool
) -> list[str]:
    """Return errors if package __init__.py __version__ differs from canonical.

    Args:
        package_init: Path to the package ``__init__.py``, or None to skip.
        canonical: The canonical version from pyproject.toml.
        verbose: If True, print PASS/INFO lines.

    Returns:
        List of error strings (empty if consistent or check skipped).

    """
    if package_init is None:
        return []
    if not package_init.is_file():
        if verbose:
            print(f"INFO: {package_init} not found — skipping __version__ check")
        return []
    content = package_init.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    if m and m.group(1) != canonical:
        return [f"{package_init}: __version__ is '{m.group(1)}', expected '{canonical}'"]
    if verbose and m:
        print(f"PASS: {package_init} __version__ matches ({canonical})")
    return []


def _check_skill_version_errors(
    repo_root: Path, canonical_tuple: tuple[int, ...], verbose: bool
) -> list[str]:
    """Return errors for aspirational versions in skill markdown files.

    Args:
        repo_root: Repository root directory.
        canonical_tuple: The canonical version as a comparable tuple.
        verbose: If True, print PASS when no errors found.

    Returns:
        List of error strings (empty if all files are clean).

    """
    skip_dirs = {"worktrees"}
    scan_dirs = [
        repo_root / ".claude-plugin" / "skills",
        repo_root / ".claude",
    ]
    errors: list[str] = []
    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for md_file in sorted(scan_dir.rglob("*.md")):
            if any(part in skip_dirs for part in md_file.parts):
                continue
            rel = md_file.relative_to(repo_root)
            errors.extend(_find_aspirational_versions(md_file, canonical_tuple, str(rel)))
    if not errors and verbose:
        print("PASS: skill markdown files have no aspirational version references")
    return errors


def check_package_version_consistency(
    repo_root: Path,
    package_init: Path | None = None,
    scan_skills: bool = False,
    verbose: bool = False,
) -> int:
    """Run multi-source package version consistency checks.

    Checks:
    1. ``<package>/__init__.py`` ``__version__`` matches the canonical version.
    2. (opt-in) Skill markdown files have no aspirational version references.

    Args:
        repo_root: Root directory of the repository.
        package_init: Explicit path to a ``__init__.py`` to check.  If ``None``,
            the check is skipped (auto-detection is not performed to keep this
            function fast and side-effect free).
        scan_skills: If True, also scan ``{.claude-plugin/skills,  .claude}/`` markdown.
        verbose: If True, print passing check names.

    Returns:
        0 if all checks pass, 1 if any fail.

    """
    canonical = _get_canonical_version(repo_root)
    if verbose:
        print(f"Canonical version (git tag / metadata): {canonical}")

    all_errors: list[str] = []
    all_errors.extend(_check_init_version_errors(package_init, canonical, verbose))

    canonical_tuple = _parse_version_tuple(canonical)
    if scan_skills:
        all_errors.extend(_check_skill_version_errors(repo_root, canonical_tuple, verbose))

    if all_errors:
        for error in all_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(
            f"\nFound {len(all_errors)} package version consistency violation(s).",
            file=sys.stderr,
        )
        return 1

    if verbose:
        print(f"\nOK: all package version checks passed ({canonical})")
    return 0


def bump_version(
    repo_root: Path,
    part: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Increment the project version by one semver step.

    Reads the current canonical version (latest git tag), computes the new
    version by incrementing ``part`` (major/minor/patch), and delegates writes of
    secondary files (``VERSION``, ``__init__.py``) to
    :class:`~hephaestus.version.manager.VersionManager`.

    Note: under hatch-vcs the authoritative version is set by creating a git tag
    (``vX.Y.Z``). This helper computes and records the next version in secondary
    files; the release is finalised by tagging — see ``docs/RELEASING.md``.

    Args:
        repo_root: Root directory of the repository.
        part: Which part to increment — ``"major"``, ``"minor"``, or ``"patch"``.
        dry_run: If True, print what would change without writing.
        verbose: If True, print additional details.

    Returns:
        0 on success, 1 on failure.

    """
    current_str = _get_canonical_version(repo_root)
    try:
        current = parse_version(current_str)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if part == "major":
        new = (current[0] + 1, 0, 0)
    elif part == "minor":
        new = (current[0], current[1] + 1, 0)
    elif part == "patch":
        new = (current[0], current[1], current[2] + 1)
    else:
        print(
            f"ERROR: invalid part '{part}': must be 'major', 'minor', or 'patch'",
            file=sys.stderr,
        )
        return 1

    new_str = f"{new[0]}.{new[1]}.{new[2]}"

    if dry_run:
        print(f"Would bump version: {current_str} -> {new_str}")
        return 0

    if verbose:
        print(f"Bumping version: {current_str} -> {new_str}")

    manager = VersionManager(repo_root=repo_root)
    manager.update(new_str, verbose=verbose)

    # Validate consistency after writing
    result = check_version_consistency(repo_root, verbose=verbose)
    if result != 0:
        print(
            "ERROR: post-bump consistency check failed; files may be in an inconsistent state.",
            file=sys.stderr,
        )
        return 1

    print(f"Version bumped: {current_str} -> {new_str}")
    print()
    print("Next steps (hatch-vcs derives the published version from the git tag):")
    print(f'  1. git tag -s v{new_str} -m "Release v{new_str}"')
    print(f"  2. git push origin v{new_str}")
    print("  See docs/RELEASING.md for the full release workflow.")
    return 0


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def check_version_consistency_main() -> int:
    """CLI entry point for hephaestus-check-version-consistency.

    Returns:
        Exit code (0 if consistent, 1 on mismatch).

    """
    parser = create_validation_parser(
        "Verify the canonical git-derived project version",
        epilog="Example: %(prog)s --repo-root /path/to/repo --verbose",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print parsed versions even when they match",
    )
    args = parser.parse_args()
    root = resolve_repo_root(args)
    if args.json:
        canonical = _version_from_git_tag(root) or _version_from_metadata()
        payload = {
            "canonical_version": canonical,
            "consistent": canonical is not None,
        }
        print(format_output(payload, "json"))
        return 0 if canonical is not None else 1
    return check_version_consistency(root, verbose=args.verbose)


def check_package_versions_main() -> int:
    """CLI entry point for hephaestus-check-package-versions.

    Returns:
        Exit code (0 if all checks pass, 1 otherwise).

    """
    parser = create_validation_parser(
        "Enforce package version consistency across all version declaration sites",
        epilog="Example: %(prog)s --scan-skills --verbose",
    )
    parser.add_argument(
        "--package-init",
        type=Path,
        default=None,
        help=(
            "Path to the package __init__.py to check for __version__. "
            "Example: hephaestus/__init__.py"
        ),
    )
    parser.add_argument(
        "--scan-skills",
        action="store_true",
        help="Also scan .claude-plugin/skills/ and .claude/ markdown files",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print passing check names and canonical version",
    )
    args = parser.parse_args()
    root = resolve_repo_root(args)
    init_path: Path | None = args.package_init
    if init_path is not None and not init_path.is_absolute():
        init_path = root / init_path
    if args.json:
        canonical = _get_canonical_version(root)
        errors: list[str] = []
        errors.extend(_check_init_version_errors(init_path, canonical, verbose=False))
        if args.scan_skills:
            errors.extend(
                _check_skill_version_errors(root, _parse_version_tuple(canonical), verbose=False)
            )
        payload = {
            "canonical_version": canonical,
            "ok": not errors,
            "error_count": len(errors),
            "errors": errors,
        }
        print(format_output(payload, "json"))
        return 0 if not errors else 1
    return check_package_version_consistency(
        root,
        package_init=init_path,
        scan_skills=args.scan_skills,
        verbose=args.verbose,
    )


def bump_version_main() -> int:
    """CLI entry point for hephaestus-bump-version.

    Returns:
        Exit code (0 on success, 1 on failure).

    """
    parser = create_validation_parser(
        "Bump project version in pyproject.toml (and secondary files) atomically",
        epilog="Example: %(prog)s patch --verbose",
    )
    parser.add_argument(
        "part",
        choices=["major", "minor", "patch"],
        help="Which version part to bump",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print additional details",
    )
    args = parser.parse_args()
    root = resolve_repo_root(args)
    if args.json:
        exit_code = bump_version(root, part=args.part, dry_run=args.dry_run, verbose=False)
        emit_json_status(
            exit_code,
            message=("dry run complete" if args.dry_run else "version bumped"),
            part=args.part,
            dry_run=args.dry_run,
        )
        return exit_code
    return bump_version(root, part=args.part, dry_run=args.dry_run, verbose=args.verbose)
