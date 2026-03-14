#!/usr/bin/env python3
"""Tests for validation utilities."""


import pytest

from hephaestus.validation.markdown import (
    check_required_sections,
    count_markdown_issues,
    extract_markdown_links,
    find_markdown_files,
    validate_directory_exists,
    validate_file_exists,
    validate_relative_link,
)
from hephaestus.validation.structure import StructureValidator


class TestMarkdownValidation:
    """Test markdown validation functions."""

    def test_find_markdown_files_empty_dir(self, tmp_path):
        """Test finding markdown files in empty directory."""
        result = find_markdown_files(tmp_path)
        assert result == []

    def test_find_markdown_files_with_files(self, tmp_path):
        """Test finding markdown files."""
        (tmp_path / "test.md").write_text("# Test")
        (tmp_path / "README.md").write_text("# README")
        result = find_markdown_files(tmp_path)
        assert len(result) == 2

    def test_validate_file_exists(self, tmp_path):
        """Test file existence validation."""
        test_file = tmp_path / "test.txt"
        assert not validate_file_exists(test_file)
        test_file.write_text("content")
        assert validate_file_exists(test_file)

    def test_validate_directory_exists(self, tmp_path):
        """Test directory existence validation."""
        test_dir = tmp_path / "testdir"
        assert not validate_directory_exists(test_dir)
        test_dir.mkdir()
        assert validate_directory_exists(test_dir)

    def test_check_required_sections(self):
        """Test checking for required sections."""
        content = """
# Overview
## Details
### Subsection
"""
        all_found, missing = check_required_sections(content, ["Overview", "Details"])
        assert all_found
        assert missing == []

        all_found, missing = check_required_sections(content, ["Overview", "Missing"])
        assert not all_found
        assert "Missing" in missing

    def test_extract_markdown_links(self):
        """Test extracting markdown links."""
        content = """
[link1](file1.md)
[link2](file2.md)
[external](https://example.com)
"""
        links = extract_markdown_links(content)
        assert len(links) == 3
        assert ("file1.md", 2) in links
        assert ("file2.md", 3) in links
        assert ("https://example.com", 4) in links

    def test_validate_relative_link(self, tmp_path):
        """Test relative link validation."""
        source_file = tmp_path / "source.md"
        source_file.write_text("# Source")

        target_file = tmp_path / "target.md"
        target_file.write_text("# Target")

        # Test valid link
        is_valid, error = validate_relative_link("target.md", source_file, tmp_path)
        assert is_valid
        assert error is None

        # Test broken link
        is_valid, error = validate_relative_link("missing.md", source_file, tmp_path)
        assert not is_valid
        assert error is not None

        # Test external link (should be skipped)
        is_valid, error = validate_relative_link("https://example.com", source_file, tmp_path)
        assert is_valid
        assert error is None

    def test_count_markdown_issues(self):
        """Test counting markdown issues."""
        content = (
            "# Title\n"
            "\n"
            "\n"
            "Multiple blank lines above.\n"
            "\n"
            "```\n"
            "Code without language\n"
            "```\n"
            "\n"
            "This is a very long line that exceeds 120 characters and should be flagged as a long line issue for markdown linting purposes.\n"
            "\n"
            "Line with trailing spaces   \n"
        )
        issues = count_markdown_issues(content)
        assert issues["multiple_blank_lines"] > 0
        assert issues["missing_language_tags"] > 0
        assert issues["long_lines"] > 0
        assert issues["trailing_whitespace"] > 0


class TestStructureValidator:
    """Test structure validation."""

    def test_check_directory_exists(self, tmp_path):
        """Test directory existence check."""
        validator = StructureValidator([], {}, {})

        # Non-existent directory
        exists, msg = validator.check_directory_exists(tmp_path, "missing")
        assert not exists
        assert "Missing directory" in msg

        # Existing directory
        (tmp_path / "existing").mkdir()
        exists, msg = validator.check_directory_exists(tmp_path, "existing")
        assert exists
        assert "✓" in msg

    def test_check_directory_not_a_dir(self, tmp_path):
        """Returns False when path is a file, not a directory."""
        validator = StructureValidator([], {}, {})
        (tmp_path / "file.txt").write_text("content")
        exists, msg = validator.check_directory_exists(tmp_path, "file.txt")
        assert not exists
        assert "Not a directory" in msg

    def test_check_file_exists(self, tmp_path):
        """Test file existence check."""
        validator = StructureValidator([], {}, {})

        # Create test directory and file
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "file.txt").write_text("content")

        # Non-existent file
        exists, msg = validator.check_file_exists(tmp_path, "dir", "missing.txt")
        assert not exists
        assert "Missing file" in msg

        # Existing file
        exists, msg = validator.check_file_exists(tmp_path, "dir", "file.txt")
        assert exists
        assert "✓" in msg

    def test_check_file_not_a_file(self, tmp_path):
        """Returns False when path is a directory, not a file."""
        validator = StructureValidator([], {}, {})
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "subdir").mkdir()
        exists, msg = validator.check_file_exists(tmp_path, "dir", "subdir")
        assert not exists
        assert "Not a file" in msg

    def test_check_subdirectory_exists(self, tmp_path):
        """Test subdirectory existence check."""
        validator = StructureValidator([], {}, {})
        parent = tmp_path / "parent"
        parent.mkdir()

        # Non-existent subdirectory
        exists, msg = validator.check_subdirectory_exists(tmp_path, "parent", "missing")
        assert not exists
        assert "Missing subdirectory" in msg

        # Existing subdirectory
        (parent / "subdir").mkdir()
        exists, msg = validator.check_subdirectory_exists(tmp_path, "parent", "subdir")
        assert exists
        assert "✓" in msg

    def test_check_subdirectory_not_a_dir(self, tmp_path):
        """Returns False when subdir path is a file."""
        validator = StructureValidator([], {}, {})
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "notadir.txt").write_text("content")
        exists, msg = validator.check_subdirectory_exists(tmp_path, "parent", "notadir.txt")
        assert not exists
        assert "Not a directory" in msg

    def test_validate_structure(self, tmp_path):
        """Test full structure validation."""
        # Create test structure
        (tmp_path / "dir1").mkdir()
        (tmp_path / "dir2").mkdir()
        (tmp_path / "dir1" / "README.md").write_text("# README")

        validator = StructureValidator(
            required_directories=["dir1", "dir2"],
            required_files={"dir1": ["README.md"]},
            required_subdirs={},
        )

        results = validator.validate_structure(tmp_path, verbose=False)
        assert len(results["passed"]) == 3  # 2 dirs + 1 file
        assert len(results["failed"]) == 0

    def test_validate_structure_with_failures(self, tmp_path):
        """Test structure validation with missing items."""
        validator = StructureValidator(
            required_directories=["missing_dir"],
            required_files={"missing_dir": ["missing.txt"]},
            required_subdirs={"missing_dir": ["subdir"]},
        )
        results = validator.validate_structure(tmp_path)
        assert len(results["failed"]) > 0

    def test_validate_structure_verbose(self, tmp_path):
        """Test structure validation with verbose=True."""
        (tmp_path / "src").mkdir()
        validator = StructureValidator(
            required_directories=["src"],
            required_files={},
            required_subdirs={},
        )
        results = validator.validate_structure(tmp_path, verbose=True)
        assert len(results["passed"]) == 1

    def test_validate_structure_with_subdirs(self, tmp_path):
        """Test structure validation checks subdirectories."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "utils").mkdir()

        validator = StructureValidator(
            required_directories=["src"],
            required_files={},
            required_subdirs={"src": ["utils", "missing"]},
        )
        results = validator.validate_structure(tmp_path)
        assert any("utils" in m for m in results["passed"])
        assert any("missing" in m for m in results["failed"])

    def test_print_summary_no_failures(self, tmp_path):
        """print_summary doesn't crash with no failures."""
        validator = StructureValidator([], {}, {})
        results = {"passed": ["check1", "check2"], "failed": []}
        # Should not raise
        validator.print_summary(results)

    def test_print_summary_with_failures(self, tmp_path):
        """print_summary doesn't crash with failures."""
        validator = StructureValidator([], {}, {})
        results = {"passed": ["check1"], "failed": ["failed1", "failed2"]}
        # Should not raise
        validator.print_summary(results)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
