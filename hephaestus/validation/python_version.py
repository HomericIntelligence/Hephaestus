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

from hephaestus.cli.utils import add_json_arg, add_version_arg, format_output
from hephaestus.io.toml import import_tomllib
from hephaestus.utils.helpers import get_repo_root

tomllib = import_tomllib()

_CLASSIFIER_VERSION_RE = re.compile(r"Programming Language :: Python :: (\d+\.\d+)$")
_DOCKERFILE_FROM_RE = re.compile(r"^\s*FROM\s+python:(\d+\.\d+)", re.IGNORECASE | re.MULTILINE)
_CI_MATRIX_PYTHON_RE = re.compile(r"python-version:\s*\[([^\]]+)\]")
_PIXI_PYTHON_BOUND_RE = re.compile(r"<=?\s*(\d+\.\d+)")


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
    if tomllib is None:
        raise ImportError("tomllib/tomli must be available to call this function")
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
    return _extract_versions_from_text(content)


def _extract_versions_from_text(content: str) -> dict[str, str]:
    """Core regex extraction from raw pyproject.toml text.

    Uses section-bounded negative-lookahead for mypy to avoid crossing into
    adjacent [tool.*] sections (same contract as scripts_lib original).

    Args:
        content: Raw text content of a pyproject.toml file.

    Returns:
        Dict mapping source labels to version strings.

    """
    versions: dict[str, str] = {}

    match = re.search(r'requires-python\s*=\s*"([^"]+)"', content)
    if match:
        ver_match = re.search(r"(\d+\.\d+)", match.group(1))
        if ver_match:
            versions["requires-python"] = ver_match.group(1)

    # Section-bounded: does NOT cross into the next [tool.*] section.
    match = re.search(
        r'\[tool\.mypy\]\n(?:(?!\[).+\n)*?python_version\s*=\s*"([^"]+)"',
        content,
    )
    if match:
        versions["mypy.python_version"] = match.group(1)

    match = re.search(
        r'\[tool\.ruff\]\n(?:(?!\[).+\n)*?target-version\s*=\s*"py(\d)(\d+)"',
        content,
    )
    if match:
        versions["ruff.target-version"] = f"{match.group(1)}.{match.group(2)}"

    return versions


def extract_pyproject_versions_str(content: str) -> dict[str, str]:
    """Extract Python version specs from raw pyproject.toml text (string API).

    Equivalent to :func:`extract_pyproject_versions` but accepts the file
    content as a string rather than a :class:`~pathlib.Path`. Used by the
    ``scripts_lib`` shim and callers that already have the file content in
    memory.

    Args:
        content: Raw text content of a pyproject.toml file.

    Returns:
        Dict mapping source labels to version strings.

    """
    return _extract_versions_from_text(content)


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


def extract_project_version(content: str) -> str | None:
    """Extract version from the [project] section in pyproject.toml content.

    Uses a section-bounded regex with negative lookahead to prevent matching
    version keys that belong to a different TOML section.

    Args:
        content: The raw text content of a pyproject.toml file.

    Returns:
        The version string if found within [project], or None.

    """
    match = re.search(
        r'\[project\]\n(?:(?!\[).+\n)*?version\s*=\s*"([^"]+)"',
        content,
    )
    return match.group(1) if match else None


def extract_pixi_workspace_version(content: str) -> str | None:
    """Extract version from the [workspace] section in pixi.toml content.

    Uses a section-bounded regex with negative lookahead to prevent matching
    version keys that belong to a different TOML section.

    Args:
        content: The raw text content of a pixi.toml file.

    Returns:
        The version string if found within [workspace], or None.

    """
    match = re.search(
        r'\[workspace\]\n(?:(?!\[).+\n)*?version\s*=\s*"([^"]+)"',
        content,
    )
    return match.group(1) if match else None


def extract_pixi_python_ceiling(content: str) -> str | None:
    """Extract the upper-bound minor from pixi.toml ``[dependencies] python``.

    Parses e.g. ``python = ">=3.10,<3.14"`` and returns the exclusive (``<``)
    or inclusive (``<=``) upper bound ``"3.14"``. Returns ``None`` if there is
    no ``python`` key in the base ``[dependencies]`` table, or it carries no
    ``<``/``<=`` bound.

    The regex is bounded to the base ``[dependencies]`` table (it does not
    cross into the next ``[`` section header), so a ``python`` pin in a
    ``[feature.*.dependencies]`` table is intentionally not matched.

    Args:
        content: The raw text content of a pixi.toml file.

    Returns:
        The upper-bound ``"major.minor"`` string, or ``None``.

    """
    match = re.search(
        r'\[dependencies\]\n(?:(?!\[).+\n)*?python\s*=\s*"([^"]+)"',
        content,
    )
    if not match:
        return None
    bound = _PIXI_PYTHON_BOUND_RE.search(match.group(1))
    return bound.group(1) if bound else None


def extract_classifiers_python_versions(content: str) -> list[str]:
    """Extract Python X.Y version strings from pyproject.toml classifiers.

    Looks for entries of the form:
        "Programming Language :: Python :: 3.10"

    Args:
        content: The raw text content of a pyproject.toml file.

    Returns:
        Sorted list of version strings (e.g. ["3.10", "3.11"]).

    """
    versions = re.findall(
        r'"Programming Language :: Python :: (\d+\.\d+)"',
        content,
    )
    return sorted(set(versions))


def extract_ci_matrix_python_versions(content: str) -> list[str]:
    """Extract Python version strings from a GitHub Actions workflow file.

    Looks for a ``python-version`` matrix key with an inline list, e.g.::

        python-version: ["3.10", "3.11", "3.12"]

    Args:
        content: The raw YAML text of a GitHub Actions workflow file.

    Returns:
        Sorted list of version strings (e.g. ["3.10", "3.11"]), or empty list
        if no ``python-version`` matrix key is found.

    """
    match = _CI_MATRIX_PYTHON_RE.search(content)
    if not match:
        return []
    versions = re.findall(r'["\']?(\d+\.\d+)["\']?', match.group(1))
    return sorted(set(versions))


def check_project_version_consistency(repo_root: Path) -> bool:
    """Check that pyproject.toml and pixi.toml project versions agree.

    Reads the [project].version from pyproject.toml and the [workspace].version
    from pixi.toml (if it exists) and verifies they match. If pixi.toml has no
    version field the check passes — that is the expected state for this project.

    Args:
        repo_root: Repository root directory.

    Returns:
        True if versions are consistent (or pixi.toml has no version), False otherwise.

    """
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return True
    project_version = extract_project_version(pyproject_path.read_text())
    pixi_path = repo_root / "pixi.toml"
    if not pixi_path.exists():
        if project_version:
            print(f"OK: pyproject.toml project version = {project_version!r} (no pixi.toml)")
        return True
    pixi_version = extract_pixi_workspace_version(pixi_path.read_text())
    if pixi_version is None:
        if project_version:
            print(f"OK: pyproject.toml project version = {project_version!r}")
        print("OK: pixi.toml has no [workspace].version (as expected)")
        return True
    if project_version == pixi_version:
        print(f"OK: project version is consistent at {project_version!r}")
        return True
    print("ERROR: project version mismatch between pyproject.toml and pixi.toml!")
    print(f"  pyproject.toml [project].version = {project_version!r}")
    print(f"  pixi.toml [workspace].version    = {pixi_version!r}")
    print("  pyproject.toml is the single source of truth — update pixi.toml to match.")
    return False


def check_ci_matrix_coverage(repo_root: Path) -> bool:
    """Check that the CI python-version matrix covers all classifier versions.

    Parses pyproject.toml classifiers and .github/workflows/test.yml matrix
    and reports any classifier versions absent from the CI matrix.

    Args:
        repo_root: Repository root directory.

    Returns:
        True if CI matrix covers all classifier versions (or no data to compare),
        False if any classifier version is missing from the CI matrix.

    """
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return True
    classifier_versions = extract_classifiers_python_versions(pyproject_path.read_text())
    if not classifier_versions:
        return True
    ci_workflow = repo_root / ".github" / "workflows" / "test.yml"
    if not ci_workflow.exists():
        print(f"INFO: CI workflow not found at {ci_workflow} — skipping matrix check")
        return True
    matrix_versions = extract_ci_matrix_python_versions(ci_workflow.read_text())
    if not matrix_versions:
        print(f"INFO: No python-version matrix found in {ci_workflow} — skipping matrix check")
        return True
    missing = sorted(set(classifier_versions) - set(matrix_versions))
    extra = sorted(set(matrix_versions) - set(classifier_versions))
    if not missing:
        print(f"OK: CI matrix covers all classifier Python versions: {matrix_versions}")
        return True
    print("ERROR: CI matrix is missing Python versions listed in pyproject.toml classifiers!")
    print(f"  Classifiers: {classifier_versions}")
    print(f"  CI matrix:   {matrix_versions}")
    print(f"  Missing from CI matrix: {missing}")
    if extra:
        print(f"  In CI matrix but not in classifiers: {extra}")
    return False


def check_pixi_python_ceiling(repo_root: Path) -> bool:
    """Ensure pixi.toml's python upper bound stays within the support matrix.

    The locked dev/lint envs must not resolve above the highest classifier
    Python version (the support ceiling). An unbounded or too-high pin lets
    pixi resolve to an untested interpreter — e.g. Python 3.14 free-threaded
    (cp314t), which is neither in the declared support matrix nor CI-tested
    (see issue #1184). The accepted upper bound is at most one minor above the
    highest classifier version (``<3.14`` for a 3.13 ceiling).

    Args:
        repo_root: Repository root directory.

    Returns:
        True if the pixi python ceiling is present and within one minor of the
        highest classifier version (or there is nothing to compare), False if
        the pin is unbounded or its ceiling exceeds the allowed maximum.

    """
    from packaging.version import Version

    pyproject_path = repo_root / "pyproject.toml"
    pixi_path = repo_root / "pixi.toml"
    if not pyproject_path.exists() or not pixi_path.exists():
        return True
    classifiers = extract_classifiers_python_versions(pyproject_path.read_text())
    if not classifiers:
        return True
    highest_supported = max(Version(v) for v in classifiers)
    max_allowed = Version(f"{highest_supported.major}.{highest_supported.minor + 1}")
    ceiling = extract_pixi_python_ceiling(pixi_path.read_text())
    if ceiling is None:
        print(
            "ERROR: pixi.toml [dependencies] python has no upper bound — "
            "the env may resolve to an untested interpreter (see #1184).\n"
            f'  Add an upper bound, e.g. python = ">={classifiers[0]},<{max_allowed}".'
        )
        return False
    if Version(ceiling) > max_allowed:
        print(
            "ERROR: pixi.toml python upper bound is too high!\n"
            f"  pixi cap: <{ceiling}; highest classifier: {highest_supported}; "
            f"max allowed cap: <{max_allowed} (one minor above support ceiling)."
        )
        return False
    print(f"OK: pixi.toml python ceiling <{ceiling} is within support matrix (<= {max_allowed})")
    return True


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
    add_json_arg(parser)
    add_version_arg(parser)

    args = parser.parse_args()
    repo_root = args.repo_root or get_repo_root()

    consistent, versions = check_python_version_consistency(
        repo_root,
        check_dockerfile=args.check_dockerfile,
        verbose=args.verbose and not args.json,
    )

    project_version_ok = check_project_version_consistency(repo_root)
    ci_matrix_ok = check_ci_matrix_coverage(repo_root)
    pixi_ceiling_ok = check_pixi_python_ceiling(repo_root)

    all_ok = (
        (consistent or not versions) and project_version_ok and ci_matrix_ok and pixi_ceiling_ok
    )

    if args.json:
        report = {
            "consistent": consistent,
            "versions": versions,
            "ci_checks": {
                "project_version_ok": project_version_ok,
                "ci_matrix_ok": ci_matrix_ok,
                "pixi_ceiling_ok": pixi_ceiling_ok,
            },
            "passed": all_ok,
        }
        print(format_output(report, "json"))
        return 0 if all_ok else 1

    if not versions:
        print("WARNING: No Python version specs found in pyproject.toml")
        return 0 if all_ok else 1

    if consistent:
        unique = set(versions.values())
        if len(unique) == 1:
            print(f"OK: Python version is consistent at {next(iter(unique))}")
        else:
            print("OK: Python version specs are consistent")
        if args.verbose:
            for key, val in sorted(versions.items()):
                print(f"  {key}: {val}")
    else:
        print("ERROR: Python version inconsistency detected!", file=sys.stderr)
        for key, val in sorted(versions.items()):
            print(f"  {key}: {val}", file=sys.stderr)
        print(
            "\nAll version specifications must agree on the same Python version.",
            file=sys.stderr,
        )

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
