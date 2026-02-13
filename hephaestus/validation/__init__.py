"""Validation utilities for ProjectHephaestus.

Provides validation tools for markdown files, repository structure, and configuration files.
"""

from .markdown import (
    find_markdown_files,
    validate_file_exists,
    validate_directory_exists,
    check_required_sections,
    extract_markdown_links,
    validate_relative_link,
    count_markdown_issues,
)

__all__ = [
    "find_markdown_files",
    "validate_file_exists",
    "validate_directory_exists",
    "check_required_sections",
    "extract_markdown_links",
    "validate_relative_link",
    "count_markdown_issues",
]
