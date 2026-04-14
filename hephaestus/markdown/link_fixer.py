#!/usr/bin/env python3

"""Fix or validate invalid absolute path links in markdown files.

This module provides functionality to fix (or check) two types of invalid links:

1. Full system paths: ``/home/user/repo/...`` → relative paths
2. Absolute paths starting with ``/``: ``/agents/...`` → ``agents/...``

Use ``--check`` (``-n``) to validate without writing changes — exits 1 if
any invalid links are found.

Usage::

    hephaestus-check-links docs/          # validate only (exit 1 on issues)
    hephaestus-check-links file.md        # validate a single file
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from hephaestus.constants import DEFAULT_EXCLUDE_DIRS
from hephaestus.logging.utils import get_logger
from hephaestus.markdown.utils import find_markdown_files
from hephaestus.utils.helpers import get_repo_root

logger = get_logger(__name__)


@dataclass
class LinkFixerOptions:
    """Configuration options for the link fixer."""

    verbose: bool = False
    dry_run: bool = False
    exclude_patterns: set[str] | None = None
    system_path_pattern: str | None = None


class LinkFixer:
    """Fixes invalid absolute path links in markdown files."""

    def __init__(self, options: LinkFixerOptions | None = None):
        """Initialize the link fixer.

        Args:
            options: Configuration options for the fixer

        """
        self.options = options or LinkFixerOptions()
        self.exclude_patterns = self.options.exclude_patterns or DEFAULT_EXCLUDE_DIRS
        # Default pattern matches <home-dir>/<user>/<repo-name>
        # (captures up to but not including the final slash before the file path)
        home_dir = re.escape(str(Path.home().parent))
        self.system_path_pattern = self.options.system_path_pattern or rf"{home_dir}/[^/]+/[^/]+"

    def fix_system_path_links(self, content: str) -> tuple[str, int]:
        """Fix links with full system paths like /home/user/worktree/...

        These are converted to relative paths without the system path prefix.

        Args:
            content: Markdown content to fix

        Returns:
            Tuple of (fixed_content, fix_count)

        """
        # Match ](<system_path>/<rest>) and capture just <rest> after the system path
        # Pattern: ]\(<system_path>/(<captured_path>)\)
        pattern = rf"\]\({self.system_path_pattern}/([^)]+)\)"
        replacement = r"](\1)"

        new_content, count = re.subn(pattern, replacement, content)
        return new_content, count

    def fix_absolute_path_links(self, content: str, file_path: Path) -> tuple[str, int]:
        """Fix absolute paths like /agents/... to relative paths.

        Calculate the correct relative path based on the file's location.

        Args:
            content: Markdown content to fix
            file_path: Path to the markdown file (relative to repo root)

        Returns:
            Tuple of (fixed_content, fix_count)

        """
        # Count slashes in file path to determine directory depth
        # e.g., notes/issues/863/README.md -> depth 3, need ../../../
        depth = len(file_path.parent.parts)
        prefix = "../" * depth if depth > 0 else ""

        # Fix links starting with / (but not //)
        pattern = r"\]\(/(?!/)"
        replacement = f"]({prefix}"

        new_content, count = re.subn(pattern, replacement, content)
        return new_content, count

    def fix_file(self, file_path: Path) -> tuple[bool, int, int]:
        """Process a single markdown file to fix invalid links.

        Args:
            file_path: Path to markdown file

        Returns:
            Tuple of (file_was_modified, system_path_fixes, absolute_path_fixes).

        """
        try:
            content = file_path.read_text(encoding="utf-8")
            original_content = content

            # Fix system path links
            content, system_fixes = self.fix_system_path_links(content)

            # Get relative path from repo root for depth calculation
            try:
                repo_root = get_repo_root()
                relative_path = file_path.relative_to(repo_root)
            except (OSError, ValueError):
                # If we can't determine repo root, use file_path as-is
                relative_path = file_path

            # Fix absolute path links
            content, absolute_fixes = self.fix_absolute_path_links(content, relative_path)

            if content != original_content:
                if self.options.dry_run:
                    logger.info(
                        "[DRY RUN] Would fix %s: %d system paths, %d absolute paths",
                        file_path,
                        system_fixes,
                        absolute_fixes,
                    )
                    return True, system_fixes, absolute_fixes

                file_path.write_text(content, encoding="utf-8")
                if self.options.verbose:
                    logger.info(
                        "Fixed %s: %d system paths, %d absolute paths",
                        file_path,
                        system_fixes,
                        absolute_fixes,
                    )
                return True, system_fixes, absolute_fixes

            if self.options.verbose:
                logger.info("No changes needed for %s", file_path)
            return False, 0, 0

        except (OSError, UnicodeDecodeError) as e:
            logger.error("Error processing %s: %s", file_path, e)
            return False, 0, 0

    def process_path(self, path: Path) -> tuple[int, int, int]:
        """Process a file or directory.

        Args:
            path: Path to file or directory

        Returns:
            Tuple of (files_modified, total_system_fixes, total_absolute_fixes).

        """
        if not path.exists():
            logger.error("Error: %s does not exist", path)
            return 0, 0, 0

        files_to_fix = []
        if path.is_file():
            if path.suffix == ".md":
                files_to_fix.append(path)
            else:
                logger.warning("Warning: %s is not a markdown file", path)
                return 0, 0, 0
        else:
            files_to_fix = find_markdown_files(path, self.exclude_patterns)

        if not files_to_fix:
            logger.info("No markdown files found in %s", path)
            return 0, 0, 0

        logger.info("Found %d markdown file(s)", len(files_to_fix))

        files_modified = 0
        total_system_fixes = 0
        total_absolute_fixes = 0

        for file_path in sorted(files_to_fix):
            modified, system_fixes, absolute_fixes = self.fix_file(file_path)
            if modified:
                files_modified += 1
                total_system_fixes += system_fixes
                total_absolute_fixes += absolute_fixes

        return files_modified, total_system_fixes, total_absolute_fixes


def check_links(
    path: Path,
    verbose: bool = False,
) -> tuple[int, int, int]:
    """Check for invalid absolute-path links without writing any changes.

    Runs the link fixer in dry-run mode and returns counts of files and
    fixes that *would* have been applied.

    Args:
        path: File or directory to scan.
        verbose: If True, print details for each file that would be fixed.

    Returns:
        Tuple of ``(files_with_issues, system_path_issues, absolute_path_issues)``.

    """
    options = LinkFixerOptions(verbose=verbose, dry_run=True)
    fixer = LinkFixer(options)
    return fixer.process_path(path)


def main() -> int:
    """CLI entry point: validate (``--check``) or fix absolute-path links.

    Returns:
        0 if no issues found (or fixes applied); 1 if issues detected in
        ``--check`` mode.

    """
    parser = argparse.ArgumentParser(
        description="Check or fix absolute-path links in markdown files",
        epilog="Example: %(prog)s --check docs/ -v",
    )
    parser.add_argument("path", type=Path, help="Markdown file or directory to process")
    parser.add_argument(
        "--check",
        "-n",
        action="store_true",
        help="Validate only — report issues and exit 1 if any found (no writes)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-file details",
    )

    args = parser.parse_args()

    if args.check:
        files_with_issues, system_issues, abs_issues = check_links(args.path, verbose=args.verbose)
        total = system_issues + abs_issues
        if total > 0:
            print(
                f"Found {files_with_issues} file(s) with {total} invalid link(s) "
                f"({system_issues} system-path, {abs_issues} absolute-path).",
                file=sys.stderr,
            )
            return 1
        if args.verbose:
            print("No invalid absolute-path links found.")
        return 0

    # Fix mode
    options = LinkFixerOptions(verbose=args.verbose)
    fixer = LinkFixer(options)
    files_modified, system_fixes, abs_fixes = fixer.process_path(args.path)
    total = system_fixes + abs_fixes
    print(f"\nSummary: {files_modified} file(s) modified, {total} link(s) fixed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
