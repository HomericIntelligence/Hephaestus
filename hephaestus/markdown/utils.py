#!/usr/bin/env python3
"""Shared markdown file discovery utilities."""

from pathlib import Path

from hephaestus.constants import DEFAULT_EXCLUDE_DIRS


def find_markdown_files(
    directory: Path, exclude_dirs: set[str] | frozenset[str] | None = None
) -> list[Path]:
    """Find all markdown files in a directory recursively.

    Args:
        directory: Directory to search
        exclude_dirs: Set of directory names to exclude. Defaults to DEFAULT_EXCLUDE_DIRS.

    Returns:
        Sorted list of Path objects for markdown files.

    """
    if exclude_dirs is None:
        exclude_dirs = set(DEFAULT_EXCLUDE_DIRS)

    return sorted(
        f for f in directory.rglob("*.md") if not any(part in exclude_dirs for part in f.parts)
    )
