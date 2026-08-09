#!/usr/bin/env python3
"""Shared markdown file discovery utilities."""

import os
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

    files: list[Path] = []
    for root, dirs, filenames in os.walk(directory):
        dirs[:] = sorted(dirname for dirname in dirs if dirname not in exclude_dirs)
        root_path = Path(root)
        files.extend(root_path / name for name in filenames if name.endswith(".md"))

    return sorted(files)
