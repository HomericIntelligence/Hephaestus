"""Tests for hephaestus.validation.python_version."""

from pathlib import Path

from hephaestus.validation.python_version import (
    _extract_via_regex,
    check_python_version_consistency,
    extract_pyproject_versions,
    get_dockerfile_python_version,
    main,
)

CONSISTENT_PYPROJECT = """\
[project]
requires-python = ">=3.10"
classifiers = [
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
]

[tool.mypy]
python_version = "3.10"

[tool.ruff]
target-version = "py310"
"""

INCONSISTENT_PYPROJECT = """\
[project]
requires-python = ">=3.10"

[tool.mypy]
python_version = "3.11"

[tool.ruff]
target-version = "py312"
"""


class TestExtractPyprojectVersions:
    """Tests for extract_pyproject_versions()."""

    def test_extracts_all_versions(self, tmp_path: Path) -> None:
        """Extracts requires-python, classifiers, mypy, and ruff versions."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(CONSISTENT_PYPROJECT)
        versions = extract_pyproject_versions(pyproject)
        assert "requires-python" in versions
        assert versions["requires-python"] == "3.10"
        assert "mypy.python_version" in versions
        assert versions["mypy.python_version"] == "3.10"
        assert "ruff.target-version" in versions
        assert versions["ruff.target-version"] == "3.10"

    def test_extracts_highest_classifier(self, tmp_path: Path) -> None:
        """Picks the highest Python classifier version."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(CONSISTENT_PYPROJECT)
        versions = extract_pyproject_versions(pyproject)
        assert versions.get("classifiers-highest") == "3.12"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Non-existent file returns empty dict."""
        versions = extract_pyproject_versions(tmp_path / "nonexistent.toml")
        assert versions == {}

    def test_minimal_pyproject(self, tmp_path: Path) -> None:
        """File with only requires-python still works."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nrequires-python = ">=3.11"\n')
        versions = extract_pyproject_versions(pyproject)
        assert versions.get("requires-python") == "3.11"


class TestGetDockerfilePythonVersion:
    """Tests for get_dockerfile_python_version()."""

    def test_extracts_version(self, tmp_path: Path) -> None:
        """Extracts version from FROM python:X.Y line."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM python:3.12-slim\nRUN pip install deps\n")
        assert get_dockerfile_python_version(dockerfile) == "3.12"

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing Dockerfile returns None."""
        assert get_dockerfile_python_version(tmp_path / "Dockerfile") is None

    def test_no_python_from(self, tmp_path: Path) -> None:
        """Dockerfile without FROM python returns None."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM ubuntu:22.04\n")
        assert get_dockerfile_python_version(dockerfile) is None

    def test_case_insensitive(self, tmp_path: Path) -> None:
        """FROM matching is case-insensitive."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("from python:3.10-slim\n")
        assert get_dockerfile_python_version(dockerfile) == "3.10"


class TestCheckPythonVersionConsistency:
    """Tests for check_python_version_consistency()."""

    def test_consistent_versions(self, tmp_path: Path) -> None:
        """Consistent versions return True."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        consistent, versions = check_python_version_consistency(tmp_path)
        assert consistent is True
        assert len(versions) >= 3

    def test_inconsistent_versions(self, tmp_path: Path) -> None:
        """Inconsistent versions return False."""
        (tmp_path / "pyproject.toml").write_text(INCONSISTENT_PYPROJECT)
        consistent, _versions = check_python_version_consistency(tmp_path)
        assert consistent is False

    def test_single_version_spec(self, tmp_path: Path) -> None:
        """Single version spec is always consistent."""
        (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.10"\n')
        consistent, _versions = check_python_version_consistency(tmp_path)
        assert consistent is True

    def test_with_dockerfile_check(self, tmp_path: Path) -> None:
        """Dockerfile version is included when check_dockerfile=True."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        docker_dir = tmp_path / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text("FROM python:3.10-slim\n")
        consistent, versions = check_python_version_consistency(tmp_path, check_dockerfile=True)
        assert consistent is True
        assert any("Dockerfile" in k for k in versions)

    def test_dockerfile_mismatch(self, tmp_path: Path) -> None:
        """Mismatched Dockerfile version is detected."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        docker_dir = tmp_path / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text("FROM python:3.13-slim\n")
        consistent, _versions = check_python_version_consistency(tmp_path, check_dockerfile=True)
        assert consistent is False

    def test_no_pyproject(self, tmp_path: Path) -> None:
        """Missing pyproject.toml returns consistent (no versions to compare)."""
        consistent, versions = check_python_version_consistency(tmp_path)
        assert consistent is True
        assert versions == {}

    def test_verbose_output(self, tmp_path: Path) -> None:
        """Verbose mode prints version details."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        consistent, _versions = check_python_version_consistency(tmp_path, verbose=True)
        assert consistent is True


class TestExtractViaRegex:
    """Tests for _extract_via_regex fallback."""

    def test_extracts_versions(self, tmp_path: Path) -> None:
        """Regex fallback extracts versions from pyproject.toml."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(CONSISTENT_PYPROJECT)
        versions = _extract_via_regex(pyproject)
        assert "requires-python" in versions
        assert "mypy.python_version" in versions
        assert "ruff.target-version" in versions


class TestMain:
    """Tests for main() CLI entry point."""

    def test_consistent_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Consistent versions exit 0."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        monkeypatch.setattr("sys.argv", ["check-python-version", "--repo-root", str(tmp_path)])
        assert main() == 0

    def test_inconsistent_returns_one(self, tmp_path: Path, monkeypatch) -> None:
        """Inconsistent versions exit 1."""
        (tmp_path / "pyproject.toml").write_text(INCONSISTENT_PYPROJECT)
        monkeypatch.setattr("sys.argv", ["check-python-version", "--repo-root", str(tmp_path)])
        assert main() == 1

    def test_no_pyproject_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Missing pyproject.toml exits 0 with warning."""
        monkeypatch.setattr("sys.argv", ["check-python-version", "--repo-root", str(tmp_path)])
        assert main() == 0

    def test_verbose_flag(self, tmp_path: Path, monkeypatch) -> None:
        """Verbose flag is accepted."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        monkeypatch.setattr(
            "sys.argv",
            ["check-python-version", "--repo-root", str(tmp_path), "--verbose"],
        )
        assert main() == 0
