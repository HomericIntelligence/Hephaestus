#!/usr/bin/env python3
"""Unit tests for hephaestus.validation.structure."""

from pathlib import Path

import pytest

from hephaestus.validation.structure import StructureValidator


@pytest.fixture
def validator() -> StructureValidator:
    """Return a basic StructureValidator for testing."""
    return StructureValidator(
        required_directories=["src", "tests"],
        required_files={"src": ["main.py"], ".": ["README.md"]},
        required_subdirs={"src": ["utils"]},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a minimal valid repo layout."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main")
    (tmp_path / "src" / "utils").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "." / "README.md").parent.mkdir(exist_ok=True)
    (tmp_path / "README.md").write_text("# README")
    return tmp_path


class TestCheckDirectoryExists:
    """Tests for StructureValidator.check_directory_exists."""

    def test_existing_directory_passes(self, tmp_path: Path, validator: StructureValidator) -> None:
        """Present directory returns (True, message)."""
        (tmp_path / "src").mkdir()
        ok, msg = validator.check_directory_exists(tmp_path, "src")
        assert ok is True
        assert "src" in msg

    def test_missing_directory_fails(self, tmp_path: Path, validator: StructureValidator) -> None:
        """Absent directory returns (False, message)."""
        ok, msg = validator.check_directory_exists(tmp_path, "missing_dir")
        assert ok is False
        assert "missing_dir" in msg.lower() or "Missing" in msg

    def test_file_not_treated_as_directory(
        self, tmp_path: Path, validator: StructureValidator
    ) -> None:
        """A file at the expected directory path returns (False, message)."""
        (tmp_path / "src").write_text("I am a file")
        ok, msg = validator.check_directory_exists(tmp_path, "src")
        assert ok is False
        assert "Not a directory" in msg


class TestCheckFileExists:
    """Tests for StructureValidator.check_file_exists."""

    def test_existing_file_passes(self, tmp_path: Path, validator: StructureValidator) -> None:
        """Present file returns (True, message)."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("# code")
        ok, msg = validator.check_file_exists(tmp_path, "src", "main.py")
        assert ok is True
        assert "main.py" in msg

    def test_missing_file_fails(self, tmp_path: Path, validator: StructureValidator) -> None:
        """Absent file returns (False, message)."""
        (tmp_path / "src").mkdir()
        ok, msg = validator.check_file_exists(tmp_path, "src", "missing.py")
        assert ok is False
        assert "missing.py" in msg

    def test_directory_not_treated_as_file(
        self, tmp_path: Path, validator: StructureValidator
    ) -> None:
        """A directory at the expected file path returns (False, message)."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").mkdir()
        ok, msg = validator.check_file_exists(tmp_path, "src", "main.py")
        assert ok is False
        assert "Not a file" in msg


class TestCheckSubdirectoryExists:
    """Tests for StructureValidator.check_subdirectory_exists."""

    def test_existing_subdir_passes(self, tmp_path: Path, validator: StructureValidator) -> None:
        """Present subdirectory returns (True, message)."""
        (tmp_path / "src" / "utils").mkdir(parents=True)
        ok, msg = validator.check_subdirectory_exists(tmp_path, "src", "utils")
        assert ok is True
        assert "utils" in msg

    def test_missing_subdir_fails(self, tmp_path: Path, validator: StructureValidator) -> None:
        """Absent subdirectory returns (False, message)."""
        (tmp_path / "src").mkdir()
        ok, msg = validator.check_subdirectory_exists(tmp_path, "src", "missing_sub")
        assert ok is False
        assert "missing_sub" in msg


class TestValidateStructure:
    """Tests for StructureValidator.validate_structure."""

    def test_valid_repo_all_pass(self, repo: Path, validator: StructureValidator) -> None:
        """Fully valid repo has no failures."""
        results = validator.validate_structure(repo)
        assert not results["failed"]
        assert results["passed"]

    def test_missing_directory_adds_failure(
        self, tmp_path: Path, validator: StructureValidator
    ) -> None:
        """Missing required directory is recorded in 'failed'."""
        # Only create 'tests', not 'src'
        (tmp_path / "tests").mkdir()
        results = validator.validate_structure(tmp_path)
        assert any("src" in msg for msg in results["failed"])

    def test_missing_file_adds_failure(self, tmp_path: Path, validator: StructureValidator) -> None:
        """Missing required file is recorded in 'failed'."""
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        # README.md is absent
        results = validator.validate_structure(tmp_path)
        assert any("README.md" in msg or "main.py" in msg for msg in results["failed"])

    def test_returns_dict_with_passed_and_failed(
        self, repo: Path, validator: StructureValidator
    ) -> None:
        """Return value always has 'passed' and 'failed' keys."""
        results = validator.validate_structure(repo)
        assert "passed" in results
        assert "failed" in results

    def test_verbose_mode_does_not_raise(self, repo: Path, validator: StructureValidator) -> None:
        """verbose=True completes without error."""
        results = validator.validate_structure(repo, verbose=True)
        assert isinstance(results, dict)


class TestPrintSummary:
    """Tests for StructureValidator.print_summary."""

    def test_print_summary_no_failures(
        self, validator: StructureValidator, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Summary with no failures does not raise and writes to logger."""
        results: dict[str, list[str]] = {"passed": ["✓ src/", "✓ tests/"], "failed": []}
        validator.print_summary(results)  # Should not raise

    def test_print_summary_with_failures(
        self, validator: StructureValidator, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Summary with failures does not raise."""
        results: dict[str, list[str]] = {
            "passed": ["✓ tests/"],
            "failed": ["Missing directory: src/"],
        }
        validator.print_summary(results)  # Should not raise
