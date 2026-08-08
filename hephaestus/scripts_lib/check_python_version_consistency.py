"""Compatibility shim for the canonical Python-version validator."""

from hephaestus.validation.python_version import (
    check_ci_matrix_coverage,
    check_python_version_consistency,
    extract_ci_matrix_python_versions,
    extract_classifiers_python_versions,
    extract_pyproject_versions_str as extract_pyproject_versions,
    main,
)

__all__ = [
    "check_ci_matrix_coverage",
    "check_python_version_consistency",
    "extract_ci_matrix_python_versions",
    "extract_classifiers_python_versions",
    "extract_pyproject_versions",
    "main",
]
