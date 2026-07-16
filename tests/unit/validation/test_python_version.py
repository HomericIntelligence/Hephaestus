"""Tests for Python-version consistency validation."""

from __future__ import annotations

from pathlib import Path

from hephaestus.validation.python_version import (
    check_ci_matrix_coverage,
    check_python_version_consistency,
    extract_ci_matrix_python_versions,
    extract_pyproject_versions,
)

PYPROJECT = """[project]
requires-python = ">=3.10"
classifiers = ["Programming Language :: Python :: 3.10"]
[tool.mypy]
python_version = "3.10"
[tool.ruff]
target-version = "py310"
"""


def test_project_version_declarations_are_compared(tmp_path: Path) -> None:
    """Matching project tool declarations form one supported base version."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    consistent, versions = check_python_version_consistency(tmp_path)
    assert consistent is True
    assert versions["requires-python"] == "3.10"


def test_project_version_mismatch_is_detected(tmp_path: Path) -> None:
    """A tool targeting a different base Python fails consistency validation."""
    mismatched = PYPROJECT.replace('python_version = "3.10"', 'python_version = "3.11"')
    (tmp_path / "pyproject.toml").write_text(mismatched)
    consistent, _ = check_python_version_consistency(tmp_path)
    assert consistent is False


def test_ci_matrix_parser_returns_all_configured_versions() -> None:
    """The CI guard reads every explicitly configured Python version."""
    assert extract_ci_matrix_python_versions('python-version: ["3.10", "3.13"]') == ["3.10", "3.13"]


def test_ci_matrix_must_cover_declared_classifiers(tmp_path: Path) -> None:
    """A classifier missing from CI makes the repository contract fail."""
    missing_classifier = PYPROJECT.replace(
        '3.10"]', '3.10", "Programming Language :: Python :: 3.11"]'
    )
    (tmp_path / "pyproject.toml").write_text(missing_classifier)
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "test.yml").write_text('python-version: ["3.10"]')
    assert check_ci_matrix_coverage(tmp_path) is False


def test_pyproject_reader_handles_missing_file(tmp_path: Path) -> None:
    """A missing metadata file produces no declarations rather than an error."""
    assert extract_pyproject_versions(tmp_path / "pyproject.toml") == {}
