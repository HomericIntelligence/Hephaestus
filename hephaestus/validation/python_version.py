"""Check Python version consistency across project configuration files.

Verifies that version specifications in ``pyproject.toml`` (requires-python,
classifiers, mypy python_version, ruff target-version) and optionally a
Dockerfile are all consistent.

Usage::

    hephaestus-check-python-version
    hephaestus-check-python-version --repo-root /path/to/repo --verbose
    hephaestus-check-python-version --check-dockerfile
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from hephaestus.utils.helpers import get_repo_root

try:
    import tomllib  # type: ignore[import-not-found,no-redef]
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment,no-redef]

_CLASSIFIER_VERSION_RE = re.compile(r"Programming Language :: Python :: (\d+\.\d+)$")
_DOCKERFILE_FROM_RE = re.compile(r"^\s*FROM\s+python:(\d+\.\d+)", re.IGNORECASE | re.MULTILINE)


def extract_pyproject_versions(pyproject_path: Path) -> dict[str, str]:
    """Extract Python version specifications from ``pyproject.toml``.

    Reads the file using ``tomllib`` (stdlib 3.11+) or ``tomli`` (3.10 fallback)
    for proper TOML parsing. Falls back to regex if neither is available.

    Args:
        pyproject_path: Path to ``pyproject.toml``.

    Returns:
        Dict mapping source labels to version strings, e.g.
        ``{"requires-python": "3.10", "classifiers-highest": "3.12",
        "mypy.python_version": "3.10", "ruff.target-version": "3.10"}``.

    """
    if not pyproject_path.is_file():
        return {}

    versions: dict[str, str] = {}

    if tomllib is not None:
        versions = _extract_via_tomllib(pyproject_path)
    else:
        versions = _extract_via_regex(pyproject_path)

    return versions


def _extract_via_tomllib(pyproject_path: Path) -> dict[str, str]:
    """Extract versions using proper TOML parsing."""
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    versions: dict[str, str] = {}

    # requires-python
    requires_python = data.get("project", {}).get("requires-python", "")
    if requires_python:
        ver_match = re.search(r"(\d+\.\d+)", requires_python)
        if ver_match:
            versions["requires-python"] = ver_match.group(1)

    # Classifiers — highest Python X.Y
    classifiers: list[str] = data.get("project", {}).get("classifiers", [])
    py_versions: list[tuple[int, int]] = []
    for classifier in classifiers:
        m = _CLASSIFIER_VERSION_RE.match(classifier.strip())
        if m:
            major, minor = m.group(1).split(".")
            py_versions.append((int(major), int(minor)))
    if py_versions:
        highest = max(py_versions)
        versions["classifiers-highest"] = f"{highest[0]}.{highest[1]}"

    # mypy python_version
    mypy_version = data.get("tool", {}).get("mypy", {}).get("python_version", "")
    if mypy_version:
        versions["mypy.python_version"] = str(mypy_version)

    # ruff target-version
    ruff_target = data.get("tool", {}).get("ruff", {}).get("target-version", "")
    if ruff_target:
        match = re.match(r"py(\d)(\d+)", ruff_target)
        if match:
            versions["ruff.target-version"] = f"{match.group(1)}.{match.group(2)}"

    return versions


def _extract_via_regex(pyproject_path: Path) -> dict[str, str]:
    """Fallback: extract versions using regex (no TOML parser available)."""
    content = pyproject_path.read_text(encoding="utf-8")
    versions: dict[str, str] = {}

    match = re.search(r'requires-python\s*=\s*"([^"]+)"', content)
    if match:
        ver_match = re.search(r"(\d+\.\d+)", match.group(1))
        if ver_match:
            versions["requires-python"] = ver_match.group(1)

    match = re.search(r'\[tool\.mypy\].*?python_version\s*=\s*"([^"]+)"', content, re.DOTALL)
    if match:
        versions["mypy.python_version"] = match.group(1)

    match = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', content)
    if match:
        versions["ruff.target-version"] = f"{match.group(1)}.{match.group(2)}"

    return versions


def get_dockerfile_python_version(dockerfile_path: Path) -> str | None:
    """Parse the Python major.minor version from a Dockerfile ``FROM`` line.

    Args:
        dockerfile_path: Path to the Dockerfile.

    Returns:
        The ``"X.Y"`` version string, or ``None`` if the file does not exist
        or has no ``FROM python:X.Y`` line.

    """
    if not dockerfile_path.is_file():
        return None

    content = dockerfile_path.read_text(encoding="utf-8")
    m = _DOCKERFILE_FROM_RE.search(content)
    return m.group(1) if m else None


def check_python_version_consistency(
    repo_root: Path,
    check_dockerfile: bool = False,
    verbose: bool = False,
) -> tuple[bool, dict[str, str]]:
    """Compare Python version specifications across config files.

    Args:
        repo_root: Root directory of the repository.
        check_dockerfile: If True, also check ``docker/Dockerfile`` for
            version consistency.
        verbose: If True, print parsed versions even when consistent.

    Returns:
        Tuple of ``(all_consistent, versions_dict)`` where *versions_dict*
        maps source labels to version strings.

    """
    pyproject_path = repo_root / "pyproject.toml"
    versions = extract_pyproject_versions(pyproject_path)

    if check_dockerfile:
        # Check common Dockerfile locations
        for dockerfile_rel in ["docker/Dockerfile", "Dockerfile"]:
            dockerfile_path = repo_root / dockerfile_rel
            docker_version = get_dockerfile_python_version(dockerfile_path)
            if docker_version is not None:
                versions[f"Dockerfile ({dockerfile_rel})"] = docker_version
                break

    if verbose:
        for key, val in sorted(versions.items()):
            print(f"  {key}: {val}")

    if len(versions) < 2:
        return True, versions

    # Check consistency: all values for base version keys should match
    # We compare requires-python, mypy, and ruff (the "base" version)
    base_keys = {"requires-python", "mypy.python_version", "ruff.target-version"}
    base_versions = {v for k, v in versions.items() if k in base_keys}

    # If we have Dockerfile, also check it matches the base
    if check_dockerfile:
        docker_keys = {k for k in versions if k.startswith("Dockerfile")}
        all_check_versions = {v for k, v in versions.items() if k in base_keys | docker_keys}
    else:
        all_check_versions = base_versions

    if len(all_check_versions) <= 1:
        return True, versions

    return False, versions


def main() -> int:
    """CLI entry point for Python version consistency checking.

    Returns:
        Exit code (0 if consistent, 1 if mismatch).

    """
    parser = argparse.ArgumentParser(
        description="Check Python version consistency across config files",
        epilog="Example: %(prog)s --repo-root /path/to/repo --verbose",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root directory (default: auto-detect)",
    )
    parser.add_argument(
        "--check-dockerfile",
        action="store_true",
        help="Also check Dockerfile for Python version consistency",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print parsed versions even when consistent",
    )

    args = parser.parse_args()
    repo_root = args.repo_root or get_repo_root()

    consistent, versions = check_python_version_consistency(
        repo_root, check_dockerfile=args.check_dockerfile, verbose=args.verbose
    )

    if not versions:
        print("WARNING: No Python version specs found in pyproject.toml")
        return 0

    if consistent:
        unique = set(versions.values())
        if len(unique) == 1:
            print(f"OK: Python version is consistent at {next(iter(unique))}")
        else:
            print("OK: Python version specs are consistent")
        if args.verbose:
            for key, val in sorted(versions.items()):
                print(f"  {key}: {val}")
        return 0

    print("ERROR: Python version inconsistency detected!", file=sys.stderr)
    for key, val in sorted(versions.items()):
        print(f"  {key}: {val}", file=sys.stderr)
    print(
        "\nAll version specifications must agree on the same Python version.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
