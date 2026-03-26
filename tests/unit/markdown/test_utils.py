#!/usr/bin/env python3
"""Tests for hephaestus.markdown.utils."""

from pathlib import Path

from hephaestus.constants import DEFAULT_EXCLUDE_DIRS
from hephaestus.markdown.utils import find_markdown_files


class TestFindMarkdownFiles:
    """Tests for find_markdown_files."""

    def test_finds_md_files_in_directory(self, tmp_path: Path) -> None:
        """Finds markdown files in a directory."""
        (tmp_path / "README.md").write_text("# Readme")
        (tmp_path / "CONTRIBUTING.md").write_text("# Contributing")
        (tmp_path / "script.py").write_text("pass")

        result = find_markdown_files(tmp_path)

        assert len(result) == 2
        assert all(f.suffix == ".md" for f in result)

    def test_finds_md_files_recursively(self, tmp_path: Path) -> None:
        """Finds markdown files in nested directories."""
        sub = tmp_path / "docs"
        sub.mkdir()
        (sub / "guide.md").write_text("# Guide")
        (tmp_path / "README.md").write_text("# Readme")

        result = find_markdown_files(tmp_path)

        assert len(result) == 2

    def test_excludes_default_dirs(self, tmp_path: Path) -> None:
        """Excludes files in default excluded directories."""
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "readme.md").write_text("# Excluded")
        (tmp_path / "README.md").write_text("# Included")

        result = find_markdown_files(tmp_path)

        assert len(result) == 1
        assert result[0].name == "README.md"

    def test_excludes_custom_dirs(self, tmp_path: Path) -> None:
        """Excludes files in custom excluded directories."""
        skip_dir = tmp_path / "skip"
        skip_dir.mkdir()
        (skip_dir / "file.md").write_text("# Skipped")
        (tmp_path / "README.md").write_text("# Included")

        result = find_markdown_files(tmp_path, exclude_dirs={"skip"})

        assert len(result) == 1
        assert result[0].name == "README.md"

    def test_returns_sorted_list(self, tmp_path: Path) -> None:
        """Returns files in sorted order."""
        (tmp_path / "z_file.md").write_text("z")
        (tmp_path / "a_file.md").write_text("a")
        (tmp_path / "m_file.md").write_text("m")

        result = find_markdown_files(tmp_path)

        names = [f.name for f in result]
        assert names == sorted(names)

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        """Returns empty list for directory with no markdown files."""
        (tmp_path / "script.py").write_text("pass")

        result = find_markdown_files(tmp_path)

        assert result == []

    def test_uses_default_exclude_dirs_when_none(self, tmp_path: Path) -> None:
        """Uses DEFAULT_EXCLUDE_DIRS when exclude_dirs is None."""
        (tmp_path / "README.md").write_text("# Readme")

        result = find_markdown_files(tmp_path, exclude_dirs=None)

        assert len(result) == 1

    def test_excludes_all_default_dirs(self, tmp_path: Path) -> None:
        """Excludes files in every entry of DEFAULT_EXCLUDE_DIRS."""
        (tmp_path / "README.md").write_text("# Root")
        for dirname in DEFAULT_EXCLUDE_DIRS:
            excluded = tmp_path / dirname
            excluded.mkdir()
            (excluded / "file.md").write_text("# Excluded")

        result = find_markdown_files(tmp_path)

        assert result == [tmp_path / "README.md"]

    def test_accepts_frozenset_exclude_dirs(self, tmp_path: Path) -> None:
        """Accepts frozenset as exclude_dirs parameter."""
        skip_dir = tmp_path / "skip"
        skip_dir.mkdir()
        (skip_dir / "file.md").write_text("# Skipped")
        (tmp_path / "README.md").write_text("# Included")

        result = find_markdown_files(tmp_path, exclude_dirs=frozenset({"skip"}))

        assert len(result) == 1
        assert result[0].name == "README.md"

    def test_empty_directory_no_files(self, tmp_path: Path) -> None:
        """Returns empty list for a truly empty directory."""
        result = find_markdown_files(tmp_path)

        assert result == []

    def test_excludes_deeply_nested_dir(self, tmp_path: Path) -> None:
        """Excludes files when excluded directory name appears deep in path."""
        nested = tmp_path / "a" / "b" / "node_modules" / "c"
        nested.mkdir(parents=True)
        (nested / "file.md").write_text("# Deep excluded")
        (tmp_path / "README.md").write_text("# Included")

        result = find_markdown_files(tmp_path)

        assert result == [tmp_path / "README.md"]

    def test_returns_only_md_files_among_mixed(self, tmp_path: Path) -> None:
        """Returns only .md files when mixed file types are present."""
        (tmp_path / "doc.md").write_text("# Doc")
        (tmp_path / "notes.txt").write_text("notes")
        (tmp_path / "guide.rst").write_text("guide")
        (tmp_path / "script.py").write_text("pass")
        (tmp_path / "data.json").write_text("{}")

        result = find_markdown_files(tmp_path)

        assert len(result) == 1
        assert result[0].name == "doc.md"
