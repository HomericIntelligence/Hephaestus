#!/usr/bin/env python3

"""Fix invalid absolute path links in markdown files.

This module provides functionality to fix two types of invalid links:
1. Full system paths: /home/user/repo/... -> relative paths
2. Absolute paths starting with /: /agents/... -> agents/...
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from hephaestus.utils.helpers import get_repo_root


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
        self.exclude_patterns = self.options.exclude_patterns or {
            "node_modules", ".git", "venv", "__pycache__", ".tox", ".pixi"
        }
        # Default pattern matches /home/<user>/<repo-name>
        # (captures up to but not including the final slash before the file path)
        self.system_path_pattern = self.options.system_path_pattern or r"/home/[^/]+/[^/]+"

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
            except Exception:
                # If we can't determine repo root, use file_path as-is
                relative_path = file_path

            # Fix absolute path links
            content, absolute_fixes = self.fix_absolute_path_links(content, relative_path)

            total_fixes = system_fixes + absolute_fixes

            if content != original_content:
                if self.options.dry_run:
                    print(f"[DRY RUN] Would fix {file_path}: {system_fixes} system paths, {absolute_fixes} absolute paths")
                    return True, system_fixes, absolute_fixes

                file_path.write_text(content, encoding="utf-8")
                if self.options.verbose:
                    print(f"Fixed {file_path}: {system_fixes} system paths, {absolute_fixes} absolute paths")
                return True, system_fixes, absolute_fixes

            if self.options.verbose:
                print(f"No changes needed for {file_path}")
            return False, 0, 0

        except Exception as e:
            print(f"Error processing {file_path}: {e}", file=sys.stderr)
            return False, 0, 0

    def process_path(self, path: Path) -> tuple[int, int, int]:
        """Process a file or directory.

        Args:
            path: Path to file or directory

        Returns:
            Tuple of (files_modified, total_system_fixes, total_absolute_fixes).

        """
        if not path.exists():
            print(f"Error: {path} does not exist", file=sys.stderr)
            return 0, 0, 0

        files_to_fix = []
        if path.is_file():
            if path.suffix == ".md":
                files_to_fix.append(path)
            else:
                print(f"Warning: {path} is not a markdown file", file=sys.stderr)
                return 0, 0, 0
        else:
            files_to_fix = [
                f for f in path.rglob("*.md")
                if not any(part in self.exclude_patterns for part in f.parts)
            ]

        if not files_to_fix:
            print(f"No markdown files found in {path}")
            return 0, 0, 0

        print(f"Found {len(files_to_fix)} markdown file(s)")

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
