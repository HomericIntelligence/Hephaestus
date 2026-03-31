"""Tests for hephaestus/git/changelog.py."""

import textwrap
from pathlib import Path

from hephaestus.git.changelog import (
    changelog_has_version,
    extract_version_from_pyproject,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_pyproject(tmp_path: Path, content: str) -> Path:
    """Write a pyproject.toml to *tmp_path* and return its path."""
    path = tmp_path / "pyproject.toml"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def write_changelog(tmp_path: Path, content: str) -> Path:
    """Write a CHANGELOG.md to *tmp_path* and return its path."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


PYPROJECT_VERSION_010 = """\
    [project]
    name = "hephaestus"
    version = "0.1.0"
"""

PYPROJECT_VERSION_200 = """\
    [project]
    name = "hephaestus"
    version = "2.0.0"
"""

PYPROJECT_NO_VERSION = """\
    [project]
    name = "hephaestus"
"""

CHANGELOG_WITH_010 = """\
    # Changelog

    ## [Unreleased]

    ## [0.1.0] - 2026-03-25

    ### Added
    - Initial release
"""

CHANGELOG_BARE_010 = """\
    # Changelog

    ## 0.1.0

    ### Added
    - Initial release
"""

CHANGELOG_NO_MATCH = """\
    # Changelog

    ## [Unreleased]

    ## [0.2.0] - 2026-04-01

    ### Added
    - Something else
"""

CHANGELOG_EMPTY = ""


# ---------------------------------------------------------------------------
# extract_version_from_pyproject
# ---------------------------------------------------------------------------


class TestExtractVersionFromPyproject:
    """Tests for extract_version_from_pyproject()."""

    def test_reads_version(self, tmp_path: Path) -> None:
        """Should return the version string."""
        write_pyproject(tmp_path, PYPROJECT_VERSION_010)
        assert extract_version_from_pyproject(tmp_path / "pyproject.toml") == "0.1.0"

    def test_reads_different_version(self, tmp_path: Path) -> None:
        """Should return a different version string."""
        write_pyproject(tmp_path, PYPROJECT_VERSION_200)
        assert extract_version_from_pyproject(tmp_path / "pyproject.toml") == "2.0.0"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """Should return None when pyproject.toml is absent."""
        result = extract_version_from_pyproject(tmp_path / "pyproject.toml")
        assert result is None

    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        """Should return None when [project].version is missing."""
        write_pyproject(tmp_path, PYPROJECT_NO_VERSION)
        result = extract_version_from_pyproject(tmp_path / "pyproject.toml")
        assert result is None


# ---------------------------------------------------------------------------
# changelog_has_version
# ---------------------------------------------------------------------------


class TestChangelogHasVersion:
    """Tests for changelog_has_version()."""

    def test_bracketed_version_found(self, tmp_path: Path) -> None:
        """Should return True for '## [0.1.0]' format."""
        write_changelog(tmp_path, CHANGELOG_WITH_010)
        assert changelog_has_version(tmp_path / "CHANGELOG.md", "0.1.0") is True

    def test_bare_version_found(self, tmp_path: Path) -> None:
        """Should return True for '## 0.1.0' format (no brackets)."""
        write_changelog(tmp_path, CHANGELOG_BARE_010)
        assert changelog_has_version(tmp_path / "CHANGELOG.md", "0.1.0") is True

    def test_version_not_found(self, tmp_path: Path) -> None:
        """Should return False when the version is not present."""
        write_changelog(tmp_path, CHANGELOG_NO_MATCH)
        assert changelog_has_version(tmp_path / "CHANGELOG.md", "0.1.0") is False

    def test_empty_changelog(self, tmp_path: Path) -> None:
        """Should return False for an empty CHANGELOG.md."""
        write_changelog(tmp_path, CHANGELOG_EMPTY)
        assert changelog_has_version(tmp_path / "CHANGELOG.md", "0.1.0") is False

    def test_missing_changelog_returns_false(self, tmp_path: Path) -> None:
        """Should return False when CHANGELOG.md is absent."""
        result = changelog_has_version(tmp_path / "CHANGELOG.md", "0.1.0")
        assert result is False

    def test_no_partial_match(self, tmp_path: Path) -> None:
        """Should not match '0.1.0' inside '0.1.0-beta' or '10.1.0'."""
        write_changelog(tmp_path, "## [0.1.0-beta]\n## [10.1.0]\n")
        assert changelog_has_version(tmp_path / "CHANGELOG.md", "0.1.0") is False
