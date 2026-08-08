"""Tests for README validation functions in hephaestus.validation.markdown."""

from __future__ import annotations

from pathlib import Path

from hephaestus.validation.markdown import (
    ReadmeValidationResult,
    find_readmes,
    validate_all_readmes,
    validate_readme,
)

# ---------------------------------------------------------------------------
# ReadmeValidationResult dataclass
# ---------------------------------------------------------------------------


class TestReadmeValidationResult:
    """Tests for ReadmeValidationResult dataclass."""

    def test_default_fields(self, tmp_path: Path) -> None:
        """Default missing_sections and formatting_issues are empty lists."""
        result = ReadmeValidationResult(file=tmp_path / "README.md", passed=True)
        assert result.missing_sections == []
        assert result.formatting_issues == []

    def test_passed_false(self, tmp_path: Path) -> None:
        """Can construct a failed result with issues."""
        result = ReadmeValidationResult(
            file=tmp_path / "README.md",
            passed=False,
            missing_sections=["Overview"],
            formatting_issues=["File must end with newline"],
        )
        assert result.passed is False
        assert "Overview" in result.missing_sections
        assert len(result.formatting_issues) == 1


# ---------------------------------------------------------------------------
# find_readmes (imported from markdown module)
# ---------------------------------------------------------------------------


class TestFindReadmes:
    """Tests for find_readmes() via the markdown module."""

    def test_finds_readme_files(self, tmp_path: Path) -> None:
        """Finds README.md files recursively."""
        (tmp_path / "README.md").write_text("# Root\n")
        sub = tmp_path / "docs"
        sub.mkdir()
        (sub / "README.md").write_text("# Docs\n")
        result = find_readmes(tmp_path)
        assert len(result) == 2

    def test_ignores_non_readme_md_files(self, tmp_path: Path) -> None:
        """Does not return non-README markdown files."""
        (tmp_path / "guide.md").write_text("# Guide\n")
        result = find_readmes(tmp_path)
        assert result == []

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Returns empty list when no READMEs found."""
        result = find_readmes(tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# validate_readme
# ---------------------------------------------------------------------------


class TestValidateReadme:
    """Tests for validate_readme()."""

    def test_valid_readme_passes(self, tmp_path: Path) -> None:
        """Complete README with all required sections passes validation."""
        readme = tmp_path / "README.md"
        readme.write_text(
            "# Title\n\n## Overview\n\nText.\n\n## Installation\n\nSteps.\n\n## Usage\n\nUsage.\n"
        )
        result = validate_readme(readme)
        assert result.passed is True
        assert result.missing_sections == []
        assert result.formatting_issues == []

    def test_readme_missing_sections_fails(self, tmp_path: Path) -> None:
        """README missing required sections fails validation."""
        readme = tmp_path / "README.md"
        readme.write_text("# Title\n\nNo sections here.\n")
        result = validate_readme(readme)
        assert result.passed is False
        assert len(result.missing_sections) > 0

    def test_specific_missing_sections_reported(self, tmp_path: Path) -> None:
        """Missing section names are listed in the result."""
        readme = tmp_path / "README.md"
        readme.write_text("# Title\n\n## Overview\n\nText.\n")
        result = validate_readme(readme, required_sections=["Overview", "Installation", "Usage"])
        assert result.passed is False
        assert "Installation" in result.missing_sections
        assert "Usage" in result.missing_sections
        assert "Overview" not in result.missing_sections

    def test_readme_without_trailing_newline_fails(self, tmp_path: Path) -> None:
        """README not ending with newline fails validation."""
        readme = tmp_path / "README.md"
        readme.write_bytes(
            b"# Title\n\n## Overview\n\n## Installation\n\n## Usage\n\nno trailing newline"
        )
        result = validate_readme(readme)
        assert result.passed is False
        assert any("newline" in issue.lower() for issue in result.formatting_issues)

    def test_result_has_file_path(self, tmp_path: Path) -> None:
        """Result contains the file path."""
        readme = tmp_path / "README.md"
        readme.write_text("# T\n")
        result = validate_readme(readme)
        assert result.file == readme

    def test_custom_required_sections(self, tmp_path: Path) -> None:
        """Custom required_sections override the defaults."""
        readme = tmp_path / "README.md"
        readme.write_text("# Title\n\n## Quickstart\n\nSteps.\n")
        result = validate_readme(readme, required_sections=["Quickstart"])
        assert result.passed is True
        assert result.missing_sections == []

    def test_nonexistent_file_fails(self, tmp_path: Path) -> None:
        """Non-existent file produces a failed result."""
        readme = tmp_path / "MISSING.md"
        result = validate_readme(readme)
        assert result.passed is False
        assert len(result.formatting_issues) > 0

    def test_case_insensitive_section_matching(self, tmp_path: Path) -> None:
        """Section matching is case-insensitive."""
        readme = tmp_path / "README.md"
        readme.write_text("# Title\n\n## overview\n\n## installation\n\n## usage\n\nText.\n")
        result = validate_readme(readme, required_sections=["Overview", "Installation", "Usage"])
        assert "Overview" not in result.missing_sections
        assert "Installation" not in result.missing_sections
        assert "Usage" not in result.missing_sections

    def test_default_sections_used_when_none(self, tmp_path: Path) -> None:
        """Default sections (Overview, Installation, Usage) apply when required_sections is None."""
        readme = tmp_path / "README.md"
        readme.write_text(
            "# Title\n\n## Overview\n\nText.\n\n## Installation\n\nInstall.\n\n## Usage\n\nUse.\n"
        )
        result = validate_readme(readme, required_sections=None)
        assert result.passed is True

    def test_code_block_without_language_fails(self, tmp_path: Path) -> None:
        """README with code block missing language specification fails."""
        readme = tmp_path / "README.md"
        readme.write_text(
            "# Title\n\n## Overview\n\n## Installation\n\n## Usage\n\n```\ncode here\n```\n"
        )
        result = validate_readme(readme)
        assert result.passed is False
        assert any(
            "language" in issue.lower() or "code block" in issue.lower()
            for issue in result.formatting_issues
        )


# ---------------------------------------------------------------------------
# validate_all_readmes
# ---------------------------------------------------------------------------


class TestValidateAllReadmes:
    """Tests for validate_all_readmes()."""

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        """Directory with no READMEs returns an empty list."""
        results = validate_all_readmes(tmp_path)
        assert results == []

    def test_returns_result_per_readme(self, tmp_path: Path) -> None:
        """Returns one result per README found."""
        (tmp_path / "README.md").write_text(
            "# Root\n\n## Overview\n\n## Installation\n\n## Usage\n\nText.\n"
        )
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "README.md").write_text("# Sub\n\nMissing sections.\n")
        results = validate_all_readmes(tmp_path)
        assert len(results) == 2

    def test_all_pass_when_valid(self, tmp_path: Path) -> None:
        """All results pass when READMEs are valid."""
        good_content = (
            "# Title\n\n## Overview\n\nText.\n\n## Installation\n\nInstall.\n\n## Usage\n\nUse.\n"
        )
        (tmp_path / "README.md").write_text(good_content)
        results = validate_all_readmes(tmp_path)
        assert all(r.passed for r in results)

    def test_failed_readme_in_results(self, tmp_path: Path) -> None:
        """Failed READMEs appear in results with passed=False."""
        (tmp_path / "README.md").write_text("# Only title\n")
        results = validate_all_readmes(tmp_path)
        assert len(results) == 1
        assert results[0].passed is False

    def test_custom_required_sections_propagated(self, tmp_path: Path) -> None:
        """Custom required_sections are passed through to each validate_readme call."""
        (tmp_path / "README.md").write_text("# Title\n\n## API Reference\n\nDocs.\n")
        results = validate_all_readmes(tmp_path, required_sections=["API Reference"])
        assert len(results) == 1
        assert results[0].passed is True

    def test_results_are_readme_validation_result_instances(self, tmp_path: Path) -> None:
        """Each element in the return value is a ReadmeValidationResult."""
        (tmp_path / "README.md").write_text("# T\n")
        results = validate_all_readmes(tmp_path)
        assert all(isinstance(r, ReadmeValidationResult) for r in results)
