#!/usr/bin/env python3
"""Markdown validation utilities for HomericIntelligence projects.

Provides functions for validating markdown files, checking links, and ensuring
documentation quality across projects.

Usage::

    hephaestus-validate-links docs/
    hephaestus-validate-links --verbose --directory .
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hephaestus.logging.utils import get_logger
from hephaestus.markdown.utils import find_markdown_files

logger = get_logger(__name__)

__all__ = ["find_markdown_files"]


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
                logger.debug("%s: Missing section '%s'", file_path, section)

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


def _count_multiple_blank_lines(lines: list[str]) -> int:
    """Count occurrences of multiple consecutive blank lines."""
    count = 0
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count > 1:
                count += 1
        else:
            blank_count = 0
    return count


def _count_missing_language_tags(lines: list[str]) -> int:
    """Count code blocks without language tags."""
    count = 0
    in_code_block = False
    for line in lines:
        if line.strip().startswith("```"):
            if not in_code_block:
                if line.strip() == "```":
                    count += 1
                in_code_block = True
            else:
                in_code_block = False
    return count


def count_markdown_issues(content: str) -> dict[str, int]:
    """Count common markdown issues in content.

    Args:
        content: Markdown file content

    Returns:
        Dictionary of issue counts

    """
    lines = content.split("\n")

    long_lines = sum(
        1
        for line in lines
        if len(line) > 120 and not (line.strip().startswith("http") or line.strip().startswith("`"))
    )
    trailing_ws = sum(1 for line in lines if line and line != line.rstrip())

    return {
        "multiple_blank_lines": _count_multiple_blank_lines(lines),
        "missing_language_tags": _count_missing_language_tags(lines),
        "long_lines": long_lines,
        "trailing_whitespace": trailing_ws,
    }


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


# ---------------------------------------------------------------------------
# Link validation pipeline
# ---------------------------------------------------------------------------


def _is_url(link: str) -> bool:
    """Check if link is an external URL (http/https)."""
    try:
        result = urlparse(link)
        return result.scheme in ("http", "https")
    except (ValueError, TypeError):
        return False


def validate_internal_link(link: str, source_file: Path, repo_root: Path) -> tuple[bool, str]:
    """Validate an internal (file) link in a markdown document.

    Handles both relative paths and absolute-from-root paths (``/docs/foo.md``).

    Args:
        link: Link target from markdown ``[text](link)``.
        source_file: File containing the link.
        repo_root: Repository root directory.

    Returns:
        Tuple of ``(is_valid, error_message)``. Error message is empty if valid.

    """
    link_path = link.split("#")[0]
    if not link_path:
        return True, ""

    if link_path.startswith("/"):
        target_path = repo_root / link_path.lstrip("/")
    else:
        target_path = (source_file.parent / link_path).resolve()

    if not target_path.exists():
        return False, f"File not found: {link_path}"
    return True, ""


def validate_file_links(file_path: Path, repo_root: Path, verbose: bool = False) -> dict[str, Any]:
    """Validate all links in a single markdown file.

    Args:
        file_path: Path to the markdown file.
        repo_root: Repository root directory.
        verbose: If True, log passing files.

    Returns:
        Dictionary with validation results including ``total_links``,
        ``valid_links``, ``broken_links``, and ``skipped_urls``.

    """
    try:
        rel_path = str(file_path.relative_to(repo_root))
    except ValueError:
        rel_path = str(file_path.resolve())

    result: dict[str, Any] = {
        "path": rel_path,
        "total_links": 0,
        "valid_links": 0,
        "broken_links": [],
        "skipped_urls": 0,
    }

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as e:
        result["error"] = str(e)
        return result

    links = extract_markdown_links(content)
    result["total_links"] = len(links)

    for link_target, line_num in links:
        if link_target.startswith("#"):
            result["valid_links"] += 1
            continue
        if _is_url(link_target):
            result["skipped_urls"] += 1
            continue

        is_valid, error = validate_internal_link(link_target, file_path, repo_root)
        if is_valid:
            result["valid_links"] += 1
        else:
            result["broken_links"].append({"line": line_num, "target": link_target, "error": error})

    return result


def validate_all_links(directory: Path, repo_root: Path, verbose: bool = False) -> dict[str, Any]:
    """Validate links in all markdown files under a directory.

    Args:
        directory: Directory to scan for markdown files.
        repo_root: Repository root directory.
        verbose: If True, log individual file results.

    Returns:
        Summary dictionary with ``passed``, ``failed``, ``total_links``,
        and ``broken_links`` counts.

    """
    results: dict[str, Any] = {
        "passed": [],
        "failed": [],
        "total_links": 0,
        "broken_links": 0,
    }

    md_files = find_markdown_files(directory)
    if not md_files:
        return results

    for md_file in md_files:
        file_result = validate_file_links(md_file, repo_root, verbose)
        results["total_links"] += file_result["total_links"]
        results["broken_links"] += len(file_result["broken_links"])

        if not file_result["broken_links"]:
            results["passed"].append(file_result["path"])
        else:
            results["failed"].append(file_result)

    return results


def print_link_summary(results: dict[str, Any]) -> None:
    """Print a summary of link validation results.

    Args:
        results: Results dictionary from :func:`validate_all_links`.

    """
    total_files = len(results["passed"]) + len(results["failed"])
    print("\n" + "=" * 70)
    print("LINK VALIDATION SUMMARY")
    print("=" * 70)
    print(f"Total files: {total_files}")
    print(f"Files with valid links: {len(results['passed'])}")
    print(f"Files with broken links: {len(results['failed'])}")
    print(f"\nTotal links checked: {results['total_links']}")
    print(f"Broken links: {results['broken_links']}")

    if results["failed"]:
        print(f"\nFiles with broken links ({len(results['failed'])}):")
        for file_result in results["failed"]:
            print(f"  {file_result['path']} - {len(file_result['broken_links'])} broken")
            for broken in file_result["broken_links"]:
                print(f"    Line {broken['line']}: {broken['target']}")
                print(f"      -> {broken['error']}")

    print("=" * 70)


def main() -> int:
    """CLI entry point for markdown link validation.

    Returns:
        Exit code (0 if all links valid, 1 if broken links found).

    """
    from hephaestus.utils.helpers import get_repo_root

    parser = argparse.ArgumentParser(
        description="Validate markdown links in documentation",
        epilog="Example: %(prog)s docs/ --verbose",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=None,
        help="Directory to scan (default: repo root)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root directory (default: auto-detect)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print verbose output",
    )

    args = parser.parse_args()
    repo_root = args.repo_root or get_repo_root()
    directory = args.directory or repo_root

    if not directory.exists():
        print(f"ERROR: Directory not found: {directory}", file=sys.stderr)
        return 1

    results = validate_all_links(directory, repo_root, verbose=args.verbose)
    print_link_summary(results)

    return 0 if not results["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
