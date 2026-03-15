#!/usr/bin/env python3
"""Markdown linting fixer utilities for ProjectHephaestus.

This module provides functionality to automatically fix common markdown
linting issues across repositories.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from hephaestus.constants import DEFAULT_EXCLUDE_DIRS
from hephaestus.logging.utils import get_logger
from hephaestus.markdown.utils import find_markdown_files

logger = get_logger(__name__)


@dataclass
class FixerOptions:
    """Configuration options for the markdown fixer."""

    verbose: bool = False
    dry_run: bool = False
    exclude_patterns: set[str] | None = None


class MarkdownFixer:
    """Fixes common markdown linting issues."""

    def __init__(self, options: FixerOptions | None = None):
        """Initialize the markdown fixer.

        Args:
            options: Configuration options for the fixer

        """
        self.options = options or FixerOptions()
        self.exclude_patterns = self.options.exclude_patterns or DEFAULT_EXCLUDE_DIRS

    def fix_file(self, file_path: Path) -> tuple[bool, int]:
        """Fix markdown linting errors in a file.

        Args:
            file_path: Path to markdown file

        Returns:
            Tuple of (file_was_modified, error_count_fixed).

        """
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.error("Error reading %s: %s", file_path, e)
            return False, 0

        original_content = content
        fixes = 0

        # Apply fixes in order
        content, fix_count = self._fix_md012_multiple_blank_lines(content)
        fixes += fix_count

        content, fix_count = self._fix_md040_code_language(content)
        fixes += fix_count

        content, fix_count = self._fix_md026_heading_punctuation(content)
        fixes += fix_count

        content, fix_count = self._fix_structural_issues(content)
        fixes += fix_count

        content, fix_count = self._fix_md034_bare_urls(content)
        fixes += fix_count

        # Ensure file ends with single newline
        if content and not content.endswith("\n"):
            content += "\n"
            fixes += 1

        # Write back if changed
        if content != original_content:
            if self.options.dry_run:
                logger.info("[DRY RUN] Would fix %s: %d issues", file_path, fixes)
                return True, fixes

            try:
                file_path.write_text(content, encoding="utf-8")
                if self.options.verbose:
                    logger.info("Fixed %s: %d issues", file_path, fixes)
                return True, fixes
            except OSError as e:
                logger.error("Error writing %s: %s", file_path, e)
                return False, 0

        if self.options.verbose:
            logger.info("No changes needed for %s", file_path)
        return False, 0

    def _fix_md012_multiple_blank_lines(self, content: str) -> tuple[str, int]:
        """Fix MD012: Remove multiple consecutive blank lines."""
        fixes = 0
        while "\n\n\n" in content:
            content = content.replace("\n\n\n", "\n\n")
            fixes += 1
        return content, fixes

    def _fix_md040_code_language(self, content: str) -> tuple[str, int]:
        """Fix MD040: Add language tags to code blocks."""
        fixes = 0
        # Find ``` without a language tag
        new_content = re.sub(
            r"^```\s*$",  # ``` followed only by optional whitespace at end of line
            "```text",  # Add 'text' language tag
            content,
            flags=re.MULTILINE,
        )
        if new_content != content:
            fixes = content.count("```\n") - new_content.count("```\n")
        return new_content, fixes

    def _fix_md026_heading_punctuation(self, content: str) -> tuple[str, int]:
        """Fix MD026: Remove trailing punctuation from headings."""
        fixes = 0
        lines = content.split("\n")
        fixed_lines = []

        for line in lines:
            # Remove trailing colons, periods, etc. from headings
            if re.match(r"^#{1,6}\s+", line):
                original_line = line
                line = re.sub(r"[:.,;!?]+\s*$", "", line)
                if line != original_line:
                    fixes += 1
            fixed_lines.append(line)

        return "\n".join(fixed_lines), fixes

    def _fix_structural_issues(self, content: str) -> tuple[str, int]:  # noqa: C901
        """Fix structural markdown issues (MD022, MD031, MD032, MD029, MD036).

        - MD022: Headings surrounded by blank lines
        - MD031: Code blocks surrounded by blank lines
        - MD032: Lists surrounded by blank lines
        - MD029: Ordered list numbering
        - MD036: Bold text as headings
        """
        lines = content.split("\n")
        fixed_lines: list[str] = []
        fixes = 0
        i = 0

        while i < len(lines):
            line = lines[i]
            prev_line = fixed_lines[-1] if fixed_lines else ""
            next_line = lines[i + 1] if i + 1 < len(lines) else ""

            # MD036: Convert **Bold:** to heading
            if re.match(r"^\*\*[^*]+\*\*:?", line.strip()):
                text = re.sub(r"\*\*([^*]+)\*\*:?", r"\1", line.strip())
                # Check if this looks like a heading (short, no lowercase middle)
                if len(text) < 50 and text[0].isupper():
                    fixes += 1
                    if prev_line.strip() != "":
                        fixed_lines.append("")
                    fixed_lines.append(f"### {text}")
                    if next_line.strip() != "":
                        fixed_lines.append("")
                    i += 1
                    continue

            # MD022: Headings should be surrounded by blank lines
            if re.match(r"^#{1,6}\s+", line):
                # Add blank line before heading (except at start)
                if fixed_lines and prev_line.strip() != "":
                    fixed_lines.append("")
                    fixes += 1

                fixed_lines.append(line)
                i += 1

                # Add blank line after heading
                if next_line.strip() != "" and not re.match(r"^#{1,6}\s+", next_line):
                    fixed_lines.append("")
                    fixes += 1
                continue

            # MD031: Code blocks should be surrounded by blank lines
            if line.strip().startswith("```"):
                # Add blank line before code block
                if fixed_lines and prev_line.strip() != "":
                    fixed_lines.append("")
                    fixes += 1

                # Add opening fence
                fixed_lines.append(line)
                i += 1

                # Copy code block content
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    fixed_lines.append(lines[i])
                    i += 1

                # Add closing fence
                if i < len(lines):
                    fixed_lines.append(lines[i])
                    i += 1

                # Add blank line after code block
                next_line = lines[i] if i < len(lines) else ""
                if next_line.strip() != "":
                    fixed_lines.append("")
                    fixes += 1
                continue

            # MD032: Lists should be surrounded by blank lines
            # MD029: Ordered lists should use 1. for all items
            if re.match(r"^\s*[-*+]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
                # Add blank line before list
                if fixed_lines and prev_line.strip() != "" and not self._is_list_item(prev_line):
                    fixed_lines.append("")
                    fixes += 1

                # Process list items
                list_indent = len(line) - len(line.lstrip())
                while i < len(lines):
                    curr_line = lines[i]

                    # Check if still in list
                    if not curr_line.strip():
                        # Empty line might be inside list
                        if i + 1 < len(lines) and self._is_list_item(lines[i + 1]):
                            fixed_lines.append(curr_line)
                            i += 1
                            continue
                        else:
                            break

                    # List item or continuation
                    if self._is_list_item(curr_line):
                        # MD029: Fix ordered list numbering
                        if re.match(r"^\s*\d+\.\s+", curr_line):
                            indent = len(curr_line) - len(curr_line.lstrip())
                            rest = re.sub(r"^\s*\d+\.", "", curr_line)
                            fixed_line = " " * indent + "1." + rest
                            if fixed_line != curr_line:
                                fixes += 1
                            fixed_lines.append(fixed_line)
                        else:
                            fixed_lines.append(curr_line)
                        i += 1
                    elif curr_line.startswith(" " * (list_indent + 2)):
                        # Continuation of list item (indented)
                        fixed_lines.append(curr_line)
                        i += 1
                    else:
                        break

                # Add blank line after list
                if i < len(lines) and lines[i].strip() != "":
                    fixed_lines.append("")
                    fixes += 1
                continue

            # Default: copy line as-is
            fixed_lines.append(line)
            i += 1

        return "\n".join(fixed_lines), fixes

    def _is_list_item(self, line: str) -> bool:
        """Check if line is a list item."""
        return bool(re.match(r"^\s*[-*+]\s+", line) or re.match(r"^\s*\d+\.\s+", line))

    def _fix_md034_bare_urls(self, content: str) -> tuple[str, int]:
        """Fix MD034: Convert bare URLs to angle-bracket format.

        Wraps bare HTTP/HTTPS URLs in angle brackets to comply with markdown
        linting rules, while avoiding URLs already in markdown link syntax.

        Args:
            content: Markdown content to fix

        Returns:
            Tuple of (fixed_content, fix_count)

        """
        fixes = 0

        def replace_url(match: re.Match[str]) -> str:
            nonlocal fixes
            url = match.group(0)
            # Check if URL is already in a markdown link syntax
            start = match.start()
            if start >= 2 and content[start - 2 : start] == "](":
                return url
            fixes += 1
            return f"<{url}>"

        # Match http/https URLs
        pattern = r"https?://[a-zA-Z0-9][-a-zA-Z0-9@:%._\+~#=]{0,256}\.[a-zA-Z0-9]{1,6}\b[-a-zA-Z0-9@:%_\+.~#?&/=]*"

        fixed_content = re.sub(pattern, replace_url, content)
        return fixed_content, fixes

    def process_path(self, path: Path) -> tuple[int, int]:
        """Process a file or directory.

        Args:
            path: Path to file or directory

        Returns:
            Tuple of (files_modified, total_fixes).

        """
        if not path.exists():
            logger.error("Error: %s does not exist", path)
            return 0, 0

        files_to_fix = []
        if path.is_file():
            if path.suffix == ".md":
                files_to_fix.append(path)
            else:
                logger.warning("Warning: %s is not a markdown file", path)
                return 0, 0
        else:
            files_to_fix = find_markdown_files(path, self.exclude_patterns)

        if not files_to_fix:
            logger.info("No markdown files found in %s", path)
            return 0, 0

        logger.info("Found %d markdown file(s)", len(files_to_fix))

        files_modified = 0
        total_fixes = 0

        for file_path in sorted(files_to_fix):
            modified, fixes = self.fix_file(file_path)
            if modified:
                files_modified += 1
                total_fixes += fixes

        return files_modified, total_fixes


def main() -> None:
    """Serve as the main entry point for the markdown fixer."""
    parser = argparse.ArgumentParser(
        description="Fix common markdown linting errors automatically",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("path", type=Path, help="Path to markdown file or directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be fixed without making changes",
    )

    args = parser.parse_args()

    options = FixerOptions(verbose=args.verbose, dry_run=args.dry_run)

    fixer = MarkdownFixer(options)
    files_modified, total_fixes = fixer.process_path(args.path)

    print("\nSummary:")
    print(f"  Files modified: {files_modified}")
    print(f"  Total fixes: {total_fixes}")

    if args.dry_run:
        print("\n[DRY RUN] No files were actually modified")

    sys.exit(0)


if __name__ == "__main__":
    main()
