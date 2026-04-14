"""Validate and synchronize dependency declarations across project config files.

Provides two main operations:

**Check** (``hephaestus-check-dep-sync``): Validates that every package pinned in
``requirements*.txt`` has a corresponding entry in ``pixi.toml`` and that the
pinned version falls within the ``pixi.toml`` range constraint.  Also reports if
``pyproject.toml`` still carries ``[project.dependencies]`` (which should be
managed in ``pixi.toml`` instead).

**Sync** (``hephaestus-sync-requirements``): Re-generates ``requirements.txt`` and
``requirements-dev.txt`` from the pixi-resolved environment (``pixi list --json``),
so that pip-only contexts (Docker, CI fallback) stay in sync with the authoritative
pixi lock.

Usage::

    hephaestus-check-dep-sync
    hephaestus-check-dep-sync --repo-root /path/to/repo

    hephaestus-sync-requirements
    hephaestus-sync-requirements --check   # verify without writing
    hephaestus-sync-requirements --repo-root /path/to/repo

Exit codes:
    0  All checks pass (or sync succeeded)
    1  Violations detected (check) or sync failed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# Header written at the top of every auto-generated requirements file.
_GENERATED_HEADER = """\
# AUTO-GENERATED from pixi.toml — do not edit manually.
# Regenerate with: hephaestus-sync-requirements
# These files exist for pip-only contexts (Docker, CI fallback).
"""


# ---------------------------------------------------------------------------
# Version comparison helpers
# ---------------------------------------------------------------------------


class VersionRange(NamedTuple):
    """A single version constraint (operator + version tuple)."""

    op: str
    version: tuple[int, ...]


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints.

    Args:
        v: Version string like ``"1.2.3"``.

    Returns:
        Tuple of integers, e.g. ``(1, 2, 3)``.

    """
    return tuple(int(x) for x in re.split(r"[.\-]", v) if x.isdigit())


def _parse_constraints(spec: str) -> list[VersionRange]:
    """Parse a pixi.toml version spec into a list of :class:`VersionRange`.

    Args:
        spec: Version specification string, e.g. ``">=1.2.0,<2"``.

    Returns:
        List of parsed constraints.

    """
    constraints: list[VersionRange] = []
    spec = spec.strip().strip('"').strip("'")
    for part in spec.split(","):
        part = part.strip()
        m = re.match(r"(>=|<=|>|<|==|!=|~=)(.+)", part)
        if m:
            op, ver = m.group(1), m.group(2)
            constraints.append(VersionRange(op=op, version=_parse_version(ver)))
    return constraints


def _version_satisfies(version: tuple[int, ...], constraints: list[VersionRange]) -> bool:
    """Return True if *version* satisfies all *constraints*.

    Args:
        version: Parsed version tuple.
        constraints: List of constraints to check against.

    Returns:
        True if all constraints are satisfied.

    """
    for c in constraints:
        v = version + (0,) * max(0, len(c.version) - len(version))
        cv = c.version + (0,) * max(0, len(version) - len(c.version))
        ops = {
            ">=": v >= cv,
            "<=": v <= cv,
            ">": v > cv,
            "<": v < cv,
            "==": v == cv,
            "!=": v != cv,
        }
        satisfied = ops.get(c.op, True)
        if not satisfied:
            return False
    return True


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_pixi_toml(path: Path) -> dict[str, str]:
    """Extract package→version-spec from ``pixi.toml`` dependency sections.

    Reads ``[dependencies]`` and ``[pypi-dependencies]`` sections using a
    simple line-by-line parser (avoids a TOML dependency for 3.10 compat).

    Args:
        path: Path to ``pixi.toml``.

    Returns:
        Dict mapping package name to version spec string.

    """
    deps: dict[str, str] = {}
    in_deps = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[dependencies]") or stripped.startswith("[pypi-dependencies]"):
            in_deps = True
            continue
        if stripped.startswith("[") and in_deps:
            in_deps = False
            continue
        if in_deps and "=" in stripped and not stripped.startswith("#"):
            line_no_comment = stripped.split("#")[0].strip()
            m = re.match(r'(\S+)\s*=\s*"([^"]+)"', line_no_comment)
            if m:
                deps[m.group(1)] = m.group(2)
    return deps


def parse_requirements(path: Path) -> dict[str, str]:
    """Extract package→pinned-version from a requirements file.

    Args:
        path: Path to the requirements file.

    Returns:
        Dict mapping package name (lowercased) to pinned version string.

    """
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-r"):
            continue
        stripped = stripped.split("#")[0].strip()
        m = re.match(r"([a-zA-Z0-9_.-]+)==(.+)", stripped)
        if m:
            pins[m.group(1).lower()] = m.group(2).strip()
    return pins


# ---------------------------------------------------------------------------
# Check operations
# ---------------------------------------------------------------------------


def check_pyproject_no_deps(path: Path) -> list[str]:
    """Return errors if ``pyproject.toml`` carries managed dependency sections.

    Flags ``[project.dependencies]`` and ``[project.optional-dependencies]``
    which should be managed in ``pixi.toml`` instead.

    Args:
        path: Path to ``pyproject.toml``.

    Returns:
        List of error strings.

    """
    errors: list[str] = []
    if not path.exists():
        return errors
    text = path.read_text(encoding="utf-8")
    if re.search(r"^\[project\.dependencies\]", text, re.MULTILINE):
        errors.append("pyproject.toml contains [project.dependencies] — remove it")
    if "[project.optional-dependencies]" in text:
        errors.append("pyproject.toml contains [project.optional-dependencies] — remove it")
    return errors


def check_requirements_against_pixi(
    repo_root: Path,
    pixi_deps_lower: dict[str, str],
) -> list[str]:
    """Check that requirements files are consistent with ``pixi.toml``.

    Args:
        repo_root: Repository root directory.
        pixi_deps_lower: Lowercased package→spec dict from ``pixi.toml``.

    Returns:
        List of error strings.

    """
    errors: list[str] = []
    for req_name in ("requirements.txt", "requirements-dev.txt"):
        req_path = repo_root / req_name
        if not req_path.exists():
            continue
        pins = parse_requirements(req_path)
        for pkg, pinned_ver in pins.items():
            if pkg not in pixi_deps_lower:
                errors.append(f"{req_name}: {pkg}=={pinned_ver} has no matching entry in pixi.toml")
                continue
            constraints = _parse_constraints(pixi_deps_lower[pkg])
            if constraints:
                ver_tuple = _parse_version(pinned_ver)
                if not _version_satisfies(ver_tuple, constraints):
                    errors.append(
                        f"{req_name}: {pkg}=={pinned_ver} falls outside "
                        f"pixi.toml constraint '{pixi_deps_lower[pkg]}'"
                    )
    return errors


def check_dep_sync(repo_root: Path | None = None) -> list[str]:
    """Run all dependency consistency checks.

    Args:
        repo_root: Repository root.  Defaults to auto-detection via git.

    Returns:
        List of error messages (empty = all checks passed).

    """
    if repo_root is None:
        from hephaestus.utils.helpers import get_repo_root

        repo_root = get_repo_root()

    errors: list[str] = []
    pixi_path = repo_root / "pixi.toml"

    if not pixi_path.exists():
        return ["pixi.toml not found"]

    pixi_deps = parse_pixi_toml(pixi_path)
    pixi_deps_lower = {k.lower(): v for k, v in pixi_deps.items()}

    errors.extend(check_requirements_against_pixi(repo_root, pixi_deps_lower))
    errors.extend(check_pyproject_no_deps(repo_root / "pyproject.toml"))
    return errors


# ---------------------------------------------------------------------------
# Sync operations
# ---------------------------------------------------------------------------


def get_pixi_packages() -> dict[str, str]:
    """Return ``{package_name: version}`` from ``pixi list --json``.

    Returns:
        Dict mapping package names to resolved version strings.

    Raises:
        SystemExit: With code 1 if ``pixi list`` fails.

    """
    result = subprocess.run(
        ["pixi", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"ERROR: pixi list --json failed: {result.stderr}",
            file=sys.stderr,
        )
        sys.exit(1)
    data: list[dict[str, str]] = json.loads(result.stdout)
    return {pkg["name"]: pkg["version"] for pkg in data}


def generate_requirements_content(
    packages: list[str],
    resolved: dict[str, str],
    include_base: str | None = None,
    section_comments: dict[str, str] | None = None,
) -> str:
    """Build a requirements file string with exact pins.

    Args:
        packages: Package names to include.
        resolved: Mapping of package name to resolved version.
        include_base: Optional ``-r <file>`` line to prepend.
        section_comments: Optional per-package inline comments.

    Returns:
        Full file content as a string.

    """
    comments = section_comments or {}
    lines: list[str] = [_GENERATED_HEADER]
    if include_base:
        lines += [include_base, ""]

    for pkg in packages:
        version = resolved.get(pkg)
        if version is None:
            print(
                f"WARNING: {pkg} not found in pixi environment, skipping",
                file=sys.stderr,
            )
            continue
        comment = comments.get(pkg, "")
        suffix = f"  {comment}" if comment else ""
        lines.append(f"{pkg}=={version}{suffix}")

    lines.append("")
    return "\n".join(lines)


def sync_requirements(
    repo_root: Path,
    resolved: dict[str, str],
    core_packages: list[str],
    dev_packages: list[str],
    core_comments: dict[str, str] | None = None,
    dev_comments: dict[str, str] | None = None,
) -> list[Path]:
    """Write ``requirements.txt`` and ``requirements-dev.txt`` under *repo_root*.

    Args:
        repo_root: Repository root directory.
        resolved: Package-to-version mapping from pixi.
        core_packages: Packages for ``requirements.txt``.
        dev_packages: Packages for ``requirements-dev.txt``.
        core_comments: Per-package comments for ``requirements.txt``.
        dev_comments: Per-package comments for ``requirements-dev.txt``.

    Returns:
        List of paths written.

    """
    req_txt = generate_requirements_content(core_packages, resolved, section_comments=core_comments)
    req_dev_txt = generate_requirements_content(
        dev_packages,
        resolved,
        include_base="-r requirements.txt",
        section_comments=dev_comments,
    )
    paths: list[Path] = []
    for name, content in [
        ("requirements.txt", req_txt),
        ("requirements-dev.txt", req_dev_txt),
    ]:
        path = repo_root / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)
        print(f"Wrote {path}")
    return paths


def check_requirements_up_to_date(
    repo_root: Path,
    resolved: dict[str, str],
    core_packages: list[str],
    dev_packages: list[str],
    core_comments: dict[str, str] | None = None,
    dev_comments: dict[str, str] | None = None,
) -> bool:
    """Return True if existing requirements files match expected content.

    Args:
        repo_root: Repository root directory.
        resolved: Package-to-version mapping from pixi.
        core_packages: Packages for ``requirements.txt``.
        dev_packages: Packages for ``requirements-dev.txt``.
        core_comments: Per-package comments for ``requirements.txt``.
        dev_comments: Per-package comments for ``requirements-dev.txt``.

    Returns:
        True if both files are current, False if any are missing or out of date.

    """
    expected = {
        "requirements.txt": generate_requirements_content(
            core_packages, resolved, section_comments=core_comments
        ),
        "requirements-dev.txt": generate_requirements_content(
            dev_packages,
            resolved,
            include_base="-r requirements.txt",
            section_comments=dev_comments,
        ),
    }
    ok = True
    for name, expected_content in expected.items():
        path = repo_root / name
        if not path.exists():
            print(f"FAIL: {name} does not exist", file=sys.stderr)
            ok = False
            continue
        if path.read_text(encoding="utf-8") != expected_content:
            print(
                f"FAIL: {name} is out of date — regenerate with: hephaestus-sync-requirements",
                file=sys.stderr,
            )
            ok = False
    return ok


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def check_dep_sync_main() -> int:
    """CLI entry point for dependency consistency checking.

    Returns:
        Exit code (0 if all checks pass, 1 if violations found).

    """
    parser = argparse.ArgumentParser(
        description="Validate dependency consistency across project config files",
        epilog="Example: %(prog)s --repo-root /path/to/repo",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect via git)",
    )
    args = parser.parse_args()

    errors = check_dep_sync(args.repo_root)
    if errors:
        print("Dependency sync check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("OK: all dependency declarations are consistent")
    return 0


def sync_requirements_main() -> int:
    """CLI entry point for requirements-file synchronisation.

    Reads pixi-resolved packages and writes (or checks) requirements files.
    Callers should pass ``--core`` / ``--dev`` flags to specify which packages
    belong in each file, or rely on the calling repo's wrapper script.

    Returns:
        Exit code (0 on success, 1 on failure).

    """
    parser = argparse.ArgumentParser(
        description="Sync requirements*.txt from pixi-resolved environment",
        epilog="Example: %(prog)s --check",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify files are up-to-date without writing (exit 1 if not)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect via git)",
    )
    parser.add_argument(
        "--core",
        nargs="*",
        default=[],
        metavar="PKG",
        help="Core packages for requirements.txt",
    )
    parser.add_argument(
        "--dev",
        nargs="*",
        default=[],
        metavar="PKG",
        help="Dev-only packages for requirements-dev.txt",
    )

    args = parser.parse_args()

    if args.repo_root is not None:
        repo_root: Path = args.repo_root
    else:
        from hephaestus.utils.helpers import get_repo_root

        repo_root = get_repo_root()

    resolved = get_pixi_packages()

    if args.check:
        ok = check_requirements_up_to_date(
            repo_root,
            resolved,
            core_packages=args.core,
            dev_packages=args.dev,
        )
        if ok:
            print("OK: requirements files are up-to-date")
            return 0
        return 1

    sync_requirements(
        repo_root,
        resolved,
        core_packages=args.core,
        dev_packages=args.dev,
    )
    return 0


if __name__ == "__main__":
    sys.exit(check_dep_sync_main())
