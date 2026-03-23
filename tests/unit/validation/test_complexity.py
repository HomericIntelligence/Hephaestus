"""Tests for hephaestus.validation.complexity."""

from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.validation.complexity import (
    check_max_complexity,
    run_ruff_complexity_check,
)


class TestRunRuffComplexityCheck:
    """Tests for run_ruff_complexity_check()."""

    def test_no_violations(self, tmp_path: Path) -> None:
        """Simple code returns no violations."""
        py_file = tmp_path / "simple.py"
        py_file.write_text("def hello():\n    return 1\n")
        violations = run_ruff_complexity_check(str(py_file), 10, tmp_path)
        assert violations == []

    def test_complex_function_flagged(self, tmp_path: Path) -> None:
        """Function exceeding threshold is detected."""
        # Create a function with high cyclomatic complexity
        branches = "\n".join(
            f"    if x == {i}:\n        return {i}" for i in range(15)
        )
        py_file = tmp_path / "complex.py"
        py_file.write_text(f"def complex_func(x):\n{branches}\n    return -1\n")
        violations = run_ruff_complexity_check(str(py_file), 5, tmp_path)
        assert len(violations) >= 1
        assert violations[0]["code"] == "C901"

    def test_threshold_respected(self, tmp_path: Path) -> None:
        """Higher threshold allows more complex functions."""
        branches = "\n".join(
            f"    if x == {i}:\n        return {i}" for i in range(8)
        )
        py_file = tmp_path / "moderate.py"
        py_file.write_text(f"def moderate_func(x):\n{branches}\n    return -1\n")
        # Should fail at threshold 5
        violations_low = run_ruff_complexity_check(str(py_file), 5, tmp_path)
        # Should pass at threshold 20
        violations_high = run_ruff_complexity_check(str(py_file), 20, tmp_path)
        assert len(violations_low) >= 1
        assert len(violations_high) == 0


class TestCheckMaxComplexity:
    """Tests for check_max_complexity()."""

    def test_clean_code_passes(self, tmp_path: Path) -> None:
        """Simple code passes complexity check."""
        py_file = tmp_path / "clean.py"
        py_file.write_text("def clean():\n    return True\n")
        result = check_max_complexity(str(py_file), 10, repo_root=tmp_path)
        assert result is True

    def test_complex_code_fails(self, tmp_path: Path) -> None:
        """Complex code fails complexity check."""
        branches = "\n".join(
            f"    if x == {i}:\n        return {i}" for i in range(15)
        )
        py_file = tmp_path / "bad.py"
        py_file.write_text(f"def bad_func(x):\n{branches}\n    return -1\n")
        result = check_max_complexity(str(py_file), 5, repo_root=tmp_path)
        assert result is False
