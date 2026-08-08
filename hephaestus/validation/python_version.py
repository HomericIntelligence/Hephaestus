"""Check Python-version consistency across project configuration and CI."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, format_output, resolve_repo_root
from hephaestus.io.toml import import_tomllib

tomllib = import_tomllib()

_CLASSIFIER_VERSION_RE = re.compile(r"Programming Language :: Python :: (\d+\.\d+)$")
_DOCKERFILE_FROM_RE = re.compile(r"^\s*FROM\s+python:(\d+\.\d+)", re.IGNORECASE | re.MULTILINE)
_CI_MATRIX_PYTHON_RE = re.compile(r"python-version:\s*\[([^\]]+)\]")


def extract_pyproject_versions(pyproject_path: Path) -> dict[str, str]:
    """Extract the declared Python versions from ``pyproject.toml``."""
    if not pyproject_path.is_file():
        return {}
    if tomllib is not None:
        with pyproject_path.open("rb") as fh:
            data = tomllib.load(fh)
        versions: dict[str, str] = {}
        requires_python = str(data.get("project", {}).get("requires-python", ""))
        match = re.search(r"(\d+\.\d+)", requires_python)
        if match:
            versions["requires-python"] = match.group(1)
        classifiers = data.get("project", {}).get("classifiers", [])
        supported = [
            tuple(map(int, match.group(1).split(".")))
            for classifier in classifiers
            if (match := _CLASSIFIER_VERSION_RE.match(classifier.strip()))
        ]
        if supported:
            major, minor = max(supported)
            versions["classifiers-highest"] = f"{major}.{minor}"
        mypy = data.get("tool", {}).get("mypy", {}).get("python_version")
        if mypy:
            versions["mypy.python_version"] = str(mypy)
        ruff = data.get("tool", {}).get("ruff", {}).get("target-version")
        if isinstance(ruff, str) and (match := re.match(r"py(\d)(\d+)", ruff)):
            versions["ruff.target-version"] = f"{match.group(1)}.{match.group(2)}"
        return versions
    return extract_pyproject_versions_str(pyproject_path.read_text(encoding="utf-8"))


def extract_pyproject_versions_str(content: str) -> dict[str, str]:
    """Extract Python version declarations from raw project metadata."""
    versions: dict[str, str] = {}
    if match := re.search(r'requires-python\s*=\s*"[^"\d]*(\d+\.\d+)', content):
        versions["requires-python"] = match.group(1)
    if match := re.search(
        r'\[tool\.mypy\](?:(?!\[).)*?python_version\s*=\s*"(\d+\.\d+)"', content, re.DOTALL
    ):
        versions["mypy.python_version"] = match.group(1)
    if match := re.search(
        r'\[tool\.ruff\](?:(?!\[).)*?target-version\s*=\s*"py(\d)(\d+)"', content, re.DOTALL
    ):
        versions["ruff.target-version"] = f"{match.group(1)}.{match.group(2)}"
    return versions


def get_dockerfile_python_version(dockerfile_path: Path) -> str | None:
    """Return the Python major.minor from a Dockerfile base image, if present."""
    if not dockerfile_path.is_file():
        return None
    match = _DOCKERFILE_FROM_RE.search(dockerfile_path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def extract_classifiers_python_versions(content: str) -> list[str]:
    """Return sorted Python X.Y classifier values from project metadata."""
    return sorted(set(re.findall(r'"Programming Language :: Python :: (\d+\.\d+)"', content)))


def extract_ci_matrix_python_versions(content: str) -> list[str]:
    """Return sorted Python X.Y values from the CI matrix."""
    match = _CI_MATRIX_PYTHON_RE.search(content)
    if not match:
        return []
    return sorted(set(re.findall(r'["\']?(\d+\.\d+)["\']?', match.group(1))))


def check_ci_matrix_coverage(repo_root: Path) -> bool:
    """Verify CI tests every Python version advertised by project classifiers."""
    pyproject_path = repo_root / "pyproject.toml"
    workflow_path = repo_root / ".github" / "workflows" / "test.yml"
    if not pyproject_path.is_file() or not workflow_path.is_file():
        return True
    advertised = extract_classifiers_python_versions(pyproject_path.read_text(encoding="utf-8"))
    tested = extract_ci_matrix_python_versions(workflow_path.read_text(encoding="utf-8"))
    missing = sorted(set(advertised) - set(tested))
    if missing:
        print(f"ERROR: CI matrix is missing classifier Python versions: {missing}")
        return False
    return True


def check_python_version_consistency(
    repo_root: Path, check_dockerfile: bool = False, verbose: bool = False
) -> tuple[bool, dict[str, str]]:
    """Compare the project's base Python declarations, optionally including Docker."""
    versions = extract_pyproject_versions(repo_root / "pyproject.toml")
    if check_dockerfile:
        for relative_path in ("docker/Dockerfile", "Dockerfile"):
            version = get_dockerfile_python_version(repo_root / relative_path)
            if version is not None:
                versions[f"Dockerfile ({relative_path})"] = version
                break
    if verbose:
        for key, value in sorted(versions.items()):
            print(f"  {key}: {value}")
    checked = {
        value
        for key, value in versions.items()
        if key in {"requires-python", "mypy.python_version", "ruff.target-version"}
        or (check_dockerfile and key.startswith("Dockerfile"))
    }
    return len(checked) <= 1, versions


def main() -> int:
    """Run Python-version consistency checks for local hooks and CI."""
    parser = create_validation_parser(
        "Check Python version consistency across project configuration"
    )
    parser.add_argument("--check-dockerfile", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    repo_root = resolve_repo_root(args)
    consistent, versions = check_python_version_consistency(
        repo_root, check_dockerfile=args.check_dockerfile, verbose=args.verbose and not args.json
    )
    matrix_ok = check_ci_matrix_coverage(repo_root)
    passed = (consistent or not versions) and matrix_ok
    if args.json:
        print(
            format_output(
                {"consistent": consistent, "versions": versions, "passed": passed}, "json"
            )
        )
    elif consistent:
        print("OK: Python version specifications are consistent")
    else:
        print("ERROR: Python version inconsistency detected", file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
