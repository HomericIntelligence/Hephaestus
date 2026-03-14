#!/usr/bin/env python3
"""Markdown validation utilities for HomericIntelligence projects.

Provides functions for validating markdown files, checking links, and ensuring
documentation quality across projects.
"""

import re
from pathlib import Path

from hephaestus.constants import DEFAULT_EXCLUDE_DIRS
from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)


def find_markdown_files(directory: Path, exclude_dirs: set[str] | None = None) -> list[Path]:
    """Find all markdown files in a directory recursively.

    Args:
        directory: Directory to search
        exclude_dirs: Set of directory names to exclude from search

    Returns:
        Sorted list of Path objects for markdown files

    """
    if exclude_dirs is None:
        exclude_dirs = DEFAULT_EXCLUDE_DIRS

    markdown_files = []
    for md_file in directory.rglob("*.md"):
        # Check if any parent directory is in exclude list
        if any(part in exclude_dirs for part in md_file.parts):
            continue
        markdown_files.append(md_file)

    return sorted(markdown_files)


def validate_file_exists(file_path: Path) -> bool:
    """Validate that a file exists and is a regular file.

    Args:
        file_path: Path to check

    Returns:
        True if file exists and is a regular file

    """
    return file_path.exists() and file_path.is_file()


def validate_directory_exists(dir_path: Path) -> bool:
    """Validate that a directory exists and is a directory.

    Args:
        dir_path: Path to check

    Returns:
        True if directory exists and is a directory

    """
    return dir_path.exists() and dir_path.is_dir()


def check_required_sections(
    content: str, required_sections: list[str], file_path: Path | None = None
) -> tuple[bool, list[str]]:
    """Check if markdown content has all required sections.

    Args:
        content: Markdown file content
        required_sections: List of required heading names
        file_path: Optional path for logging

    Returns:
        Tuple of (all_found, missing_sections).

    """
    missing = []

    for section in required_sections:
        # Match heading at various levels (##, ###, etc.)
        # Double braces {{}} are needed in f-strings to create literal braces for regex
        pattern = rf"^#{{1,6}}\s+{re.escape(section)}\s*$"
        if not re.search(pattern, content, re.MULTILINE):
            missing.append(section)
            if file_path:
                logger.debug(f"{file_path}: Missing section '{section}'")

    return len(missing) == 0, missing


def extract_markdown_links(content: str) -> list[tuple[str, int]]:
    """Extract all markdown links from content.

    Args:
        content: Markdown file content

    Returns:
        List of (link_target, line_number) tuples

    """
    links = []
    lines = content.split("\n")

    for line_num, line in enumerate(lines, 1):
        # Match [text](link) format
        for match in re.finditer(r"\[([^\]]+)\]\(([^\)]+)\)", line):
            link_target = match.group(2)
            links.append((link_target, line_num))

    return links


def validate_relative_link(
    link: str, source_file: Path, repo_root: Path
) -> tuple[bool, str | None]:
    """Validate a relative markdown link.

    Args:
        link: Link target (can include anchor #section)
        source_file: File containing the link
        repo_root: Repository root directory

    Returns:
        Tuple of (is_valid, error_message).

    """
    # Skip external links
    if link.startswith(("http://", "https://", "mailto:")):
        return True, None

    # Skip anchors within same file
    if link.startswith("#"):
        return True, None

    # Split link and anchor
    if "#" in link:
        file_part, _anchor = link.split("#", 1)
    else:
        file_part, _anchor = link, None

    # Skip empty links
    if not file_part:
        return True, None

    # Resolve relative path
    link_path = (source_file.parent / file_part).resolve()

    # Check if file exists
    if not link_path.exists():
        return False, f"Broken link: {link} (file not found)"

    # If anchor specified, could validate it exists in target
    # (skipped for now to keep validation fast)

    return True, None


def count_markdown_issues(content: str) -> dict:
    """Count common markdown issues in content.

    Args:
        content: Markdown file content

    Returns:
        Dictionary of issue counts

    """
    issues = {
        "multiple_blank_lines": 0,
        "missing_language_tags": 0,
        "long_lines": 0,
        "trailing_whitespace": 0,
    }

    lines = content.split("\n")

    # Check for multiple consecutive blank lines
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count > 1:
                issues["multiple_blank_lines"] += 1
        else:
            blank_count = 0

    # Check for code blocks without language tags
    in_code_block = False
    for line in lines:
        if line.strip().startswith("```"):
            if not in_code_block:
                # Starting code block
                if line.strip() == "```":
                    issues["missing_language_tags"] += 1
                in_code_block = True
            else:
                # Ending code block
                in_code_block = False

    # Check for long lines (> 120 characters)
    for line in lines:
        if len(line) > 120:
            # Skip lines that are URLs or code
            if not (line.strip().startswith("http") or line.strip().startswith("`")):
                issues["long_lines"] += 1

    # Check for trailing whitespace
    for line in lines:
        if line and line != line.rstrip():
            issues["trailing_whitespace"] += 1

    return issues


def find_readmes(directory: Path) -> list[Path]:
    """Find all README.md files in directory tree.

    Args:
        directory: Directory to search

    Returns:
        List of README.md file paths

    """
    return list(directory.rglob("README.md"))


def extract_sections(content: str) -> list[str]:
    """Extract section headings from markdown content.

    Args:
        content: Markdown file content

    Returns:
        List of section heading names

    """
    heading_pattern = r"^#{1,6}\s+(.+)$"
    sections = []

    for line in content.split("\n"):
        match = re.match(heading_pattern, line)
        if match:
            sections.append(match.group(1).strip())

    return sections


def check_markdown_formatting(content: str) -> list[str]:
    """Check markdown formatting issues.

    Args:
        content: Markdown file content

    Returns:
        List of formatting issue descriptions

    """
    issues = []

    # Check for code blocks without language
    if re.search(r"```\s*\n", content):
        issues.append("Code blocks missing language specification")

    # Check for lists without blank lines (simplified check)
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if i > 0 and line.strip().startswith(("-", "*", "1.")):
            prev_line = lines[i - 1].strip()
            if prev_line and not prev_line.startswith(("#", "-", "*", "1.")):
                issues.append(f"Line {i + 1}: List without blank line before")
                break  # Only report first occurrence

    # Check for headings without blank lines (simplified check)
    for i, line in enumerate(lines):
        if i > 0 and line.strip().startswith("#"):
            prev_line = lines[i - 1].strip()
            if prev_line and not prev_line.startswith("#"):
                issues.append(f"Line {i + 1}: Heading without blank line before")
                break  # Only report first occurrence

    return issues
