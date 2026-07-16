"""Smoke coverage for the Python-version compatibility shim."""

from hephaestus.scripts_lib.check_python_version_consistency import extract_pyproject_versions


def test_shim_exports_project_version_parser() -> None:
    """The legacy module continues to expose the canonical parser."""
    assert extract_pyproject_versions('requires-python = ">=3.10"')["requires-python"] == "3.10"
