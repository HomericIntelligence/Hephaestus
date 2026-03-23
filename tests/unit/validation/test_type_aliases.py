"""Tests for hephaestus.validation.type_aliases."""

from pathlib import Path

import pytest

from hephaestus.validation.type_aliases import (
    check_files,
    detect_shadowing,
    format_error,
    is_shadowing_pattern,
)


class TestIsShadowingPattern:
    """Tests for is_shadowing_pattern()."""

    def test_suffix_shadowing_detected(self) -> None:
        """Generic name that is a suffix of the target is flagged."""
        assert is_shadowing_pattern("Result", "DomainResult") is True

    def test_multi_word_suffix_shadowing(self) -> None:
        """Multi-word alias that is a suffix of the target is flagged."""
        assert is_shadowing_pattern("RunResult", "ExecutorRunResult") is True

    def test_equal_names_not_flagged(self) -> None:
        """Identical names are not shadowing."""
        assert is_shadowing_pattern("Result", "Result") is False

    def test_non_suffix_not_flagged(self) -> None:
        """Alias that is not a suffix of target is not flagged."""
        assert is_shadowing_pattern("AggregatedStats", "Statistics") is False

    def test_case_insensitive(self) -> None:
        """Comparison is case-insensitive."""
        assert is_shadowing_pattern("result", "DomainResult") is True

    def test_unrelated_names(self) -> None:
        """Completely unrelated names are not flagged."""
        assert is_shadowing_pattern("Foo", "Bar") is False


class TestDetectShadowing:
    """Tests for detect_shadowing()."""

    def test_detects_shadowing_in_file(self, tmp_path: Path) -> None:
        """Detects a simple shadowing pattern in a Python file."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Result = DomainResult\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 1
        assert violations[0][2] == "Result"
        assert violations[0][3] == "DomainResult"

    def test_ignores_non_shadowing(self, tmp_path: Path) -> None:
        """Does not flag non-shadowing assignments."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Stats = AggregatedStatistics\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_skips_suppressed_lines(self, tmp_path: Path) -> None:
        """Lines with # type: ignore[shadowing] are skipped."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Result = DomainResult  # type: ignore[shadowing]\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_skips_noqa_lines(self, tmp_path: Path) -> None:
        """Lines with # noqa: shadowing are skipped."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Result = DomainResult  # noqa: shadowing\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_skips_docstrings(self, tmp_path: Path) -> None:
        """Content inside triple-quoted strings is ignored."""
        py_file = tmp_path / "example.py"
        py_file.write_text('"""\nResult = DomainResult\n"""\nx = 1\n')
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        """Missing files return empty violations."""
        py_file = tmp_path / "nonexistent.py"
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_skips_lowercase_assignments(self, tmp_path: Path) -> None:
        """Only PascalCase identifiers are checked."""
        py_file = tmp_path / "example.py"
        py_file.write_text("result = domain_result\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_multiple_violations(self, tmp_path: Path) -> None:
        """Multiple violations in one file are all detected."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Result = DomainResult\nRunner = TaskRunner\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 2


class TestFormatError:
    """Tests for format_error()."""

    def test_includes_all_info(self) -> None:
        """Error message includes file, line, and suggestion."""
        msg = format_error(Path("foo.py"), 10, "Result = DomainResult", "Result", "DomainResult")
        assert "foo.py:10" in msg
        assert "Result = DomainResult" in msg
        assert "DomainResult" in msg
        assert "type: ignore[shadowing]" in msg


class TestCheckFiles:
    """Tests for check_files()."""

    def test_clean_directory(self, tmp_path: Path) -> None:
        """Directory with no violations returns exit code 0."""
        py_file = tmp_path / "clean.py"
        py_file.write_text("x = 1\n")
        exit_code, errors = check_files([tmp_path])
        assert exit_code == 0
        assert errors == []

    def test_directory_with_violations(self, tmp_path: Path) -> None:
        """Directory with violations returns exit code 1."""
        py_file = tmp_path / "bad.py"
        py_file.write_text("Result = DomainResult\n")
        exit_code, errors = check_files([tmp_path])
        assert exit_code == 1
        assert len(errors) == 1

    def test_skips_non_python_files(self, tmp_path: Path) -> None:
        """Non-Python files are skipped."""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Result = DomainResult\n")
        exit_code, errors = check_files([tmp_path])
        assert exit_code == 0

    def test_accepts_file_paths(self, tmp_path: Path) -> None:
        """Individual file paths work."""
        py_file = tmp_path / "single.py"
        py_file.write_text("Result = DomainResult\n")
        exit_code, errors = check_files([py_file])
        assert exit_code == 1
        assert len(errors) == 1
