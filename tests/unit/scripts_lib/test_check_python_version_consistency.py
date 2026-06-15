"""Delegation stub — all tests moved to tests/unit/validation/test_python_version.py.

pytest discovers test classes imported at module scope, so running
``pixi run pytest tests/unit/scripts_lib/`` still collects all tests here.
``pythonpath = ["."]`` in pyproject.toml makes the ``tests.unit`` import work.
"""

from tests.unit.validation.test_python_version import (  # noqa: F401
    TestCheckCiMatrixCoverage,
    TestCheckPixiPythonCeiling,
    TestCheckProjectVersionConsistency,
    TestExtractCiMatrixPythonVersions,
    TestExtractClassifiersPythonVersions,
    TestExtractPixiPythonCeiling,
    TestExtractPixiWorkspaceVersion,
    TestExtractProjectVersion,
    TestExtractPyprojectVersionsStr,
    TestSmokeAgainstRealFiles,
)
