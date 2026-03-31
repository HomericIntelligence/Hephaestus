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

    def _fix_structural_issues(self, content: str) -> tuple[str, int]:
        """Fix structural markdown issues (MD022, MD031, MD032, MD029, MD036).

        Delegates each rule to its dedicated helper method so complexity stays
        manageable and rules can be tested independently.

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
            md036_result = self._try_fix_md036_line(line, prev_line, next_line)
            if md036_result is not None:
                fixed_lines.extend(md036_result)
                fixes += 1
                i += 1
                continue

            # MD022: Headings should be surrounded by blank lines
            if re.match(r"^#{1,6}\s+", line):
                added = self._fix_md022_heading_blank_lines(line, next_line, fixed_lines, prev_line)
                fixes += added
                i += 1
                continue

            # MD031: Code blocks should be surrounded by blank lines
            if line.strip().startswith("```"):
                added, i = self._fix_md031_code_block_blank_lines(lines, i, fixed_lines, prev_line)
                fixes += added
                continue

            # MD032/MD029: Lists should be surrounded by blank lines;
            # ordered lists should use 1. for all items
            if re.match(r"^\s*[-*+]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
                added, i = self._fix_md032_md029_list(lines, i, fixed_lines, prev_line)
                fixes += added
                continue

            # Default: copy line as-is
            fixed_lines.append(line)
            i += 1

        return "\n".join(fixed_lines), fixes

    def _try_fix_md036_line(self, line: str, prev_line: str, next_line: str) -> list[str] | None:
        """Check and convert a bold-as-heading line per MD036.

        Returns a list of output lines to emit if the line matches and should be
        converted, or None if no conversion applies.

        Args:
            line: Current line to examine.
            prev_line: Line immediately before (already emitted).
            next_line: Line immediately after.

        Returns:
            List of lines to emit, or None when rule does not apply.

        """
        if not re.match(r"^\*\*[^*]+\*\*:?\s*$", line.strip()):
            return None
        text = re.sub(r"\*\*([^*]+)\*\*:?", r"\1", line.strip())
        # Only convert short title-cased text that looks like a heading
        if len(text) >= 50 or not text[0].isupper():
            return None
        result: list[str] = []
        if prev_line.strip() != "":
            result.append("")
        result.append(f"### {text}")
        if next_line.strip() != "":
            result.append("")
        return result

    def _fix_md022_heading_blank_lines(
        self,
        line: str,
        next_line: str,
        fixed_lines: list[str],
        prev_line: str,
    ) -> int:
        """Ensure blank lines surround a heading (MD022).

        Appends the heading (and surrounding blank lines as needed) to
        fixed_lines in-place and returns the number of blank lines added.

        Args:
            line: The heading line.
            next_line: The line after the heading.
            fixed_lines: Accumulated output lines (mutated in-place).
            prev_line: The last line already appended to fixed_lines.

        Returns:
            Number of blank lines inserted.

        """
        fixes = 0
        # Add blank line before heading (except at start of document)
        if fixed_lines and prev_line.strip() != "":
            fixed_lines.append("")
            fixes += 1
        fixed_lines.append(line)
        # Add blank line after heading (unless followed by another heading or blank)
        if next_line.strip() != "" and not re.match(r"^#{1,6}\s+", next_line):
            fixed_lines.append("")
            fixes += 1
        return fixes

    def _fix_md031_code_block_blank_lines(
        self,
        lines: list[str],
        i: int,
        fixed_lines: list[str],
        prev_line: str,
    ) -> tuple[int, int]:
        """Ensure blank lines surround a fenced code block (MD031).

        Consumes lines from the source list until the closing fence, appends
        everything (with surrounding blank lines) to fixed_lines, and returns
        the updated source index and fix count.

        Args:
            lines: All source lines.
            i: Index of the opening fence line.
            fixed_lines: Accumulated output lines (mutated in-place).
            prev_line: The last line already appended to fixed_lines.

        Returns:
            Tuple of (fixes_added, new_index).

        """
        fixes = 0
        # Blank line before code block
        if fixed_lines and prev_line.strip() != "":
            fixed_lines.append("")
            fixes += 1
        # Opening fence
        fixed_lines.append(lines[i])
        i += 1
        # Code block body
        while i < len(lines) and not lines[i].strip().startswith("```"):
            fixed_lines.append(lines[i])
            i += 1
        # Closing fence
        if i < len(lines):
            fixed_lines.append(lines[i])
            i += 1
        # Blank line after code block
        next_line = lines[i] if i < len(lines) else ""
        if next_line.strip() != "":
            fixed_lines.append("")
            fixes += 1
        return fixes, i

    def _fix_md032_md029_list(
        self,
        lines: list[str],
        i: int,
        fixed_lines: list[str],
        prev_line: str,
    ) -> tuple[int, int]:
        """Ensure blank lines surround a list and fix ordered numbering (MD032/MD029).

        Consumes list items from lines, appends them to fixed_lines with the
        required surrounding blank lines, and normalises ordered-list numbers to
        ``1.`` (MD029).  Returns the updated source index and fix count.

        Args:
            lines: All source lines.
            i: Index of the first list item line.
            fixed_lines: Accumulated output lines (mutated in-place).
            prev_line: The last line already appended to fixed_lines.

        Returns:
            Tuple of (fixes_added, new_index).

        """
        fixes = 0
        line = lines[i]
        # Blank line before list (only when preceded by non-blank, non-list content)
        if fixed_lines and prev_line.strip() != "" and not self._is_list_item(prev_line):
            fixed_lines.append("")
            fixes += 1
        list_indent = len(line) - len(line.lstrip())
        # Shared mutable counter so _normalize_ordered_item can increment it
        fixes_ref: list[int] = [fixes]
        while i < len(lines):
            curr_line = lines[i]
            if not curr_line.strip():
                # Empty line may be a loose list separator
                if i + 1 < len(lines) and self._is_list_item(lines[i + 1]):
                    fixed_lines.append(curr_line)
                    i += 1
                    continue
                else:
                    break
            if self._is_list_item(curr_line):
                fixed_lines.append(self._normalize_ordered_item(curr_line, fixes_ref))
                i += 1
            elif curr_line.startswith(" " * (list_indent + 2)):
                # Indented continuation of a list item
                fixed_lines.append(curr_line)
                i += 1
            else:
                break
        # Sync ordered-item fix count back from the shared reference
        fixes = fixes_ref[0]
        # Blank line after list
        if i < len(lines) and lines[i].strip() != "":
            fixed_lines.append("")
            fixes += 1
        return fixes, i

    def _normalize_ordered_item(self, line: str, fixes_ref: list[int]) -> str:
        """Normalise an ordered list item to use ``1.`` (MD029).

        Args:
            line: The ordered list item line.
            fixes_ref: Single-element list used as a mutable counter so the
                caller's fix count can be incremented.

        Returns:
            The normalised line.

        """
        if not re.match(r"^\s*\d+\.\s+", line):
            return line
        indent = len(line) - len(line.lstrip())
        rest = re.sub(r"^\s*\d+\.", "", line)
        fixed = " " * indent + "1." + rest
        if fixed != line:
            fixes_ref[0] += 1
        return fixed

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
