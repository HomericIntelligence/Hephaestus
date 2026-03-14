#!/usr/bin/env python3

"""Fix invalid absolute path links in markdown files.

This script fixes two types of invalid links:
1. Full system paths: /home/user/repo/... -> relative paths
2. Absolute paths starting with /: /agents/... -> agents/...

Usage:
    python3 scripts/fix_invalid_links.py [--dry-run] [path]
"""

import argparse
import sys
from pathlib import Path

from hephaestus.markdown.link_fixer import LinkFixer, LinkFixerOptions
from hephaestus.utils.helpers import get_repo_root


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Fix invalid absolute path links in markdown files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help="Path to markdown file or directory (default: repository root)",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be fixed without making changes",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()

    # Default to repo root if no path provided
    if args.path is None:
        try:
            args.path = get_repo_root()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Create options
    options = LinkFixerOptions(
        verbose=args.verbose,
        dry_run=args.dry_run,
    )

    # Create fixer and process
    fixer = LinkFixer(options)
    files_modified, total_system_fixes, total_absolute_fixes = fixer.process_path(args.path)

    # Summary
    print(f"\n{'Would fix' if args.dry_run else 'Fixed'} {files_modified} file(s):")
    print(f"  - System path links: {total_system_fixes}")
    print(f"  - Absolute path links: {total_absolute_fixes}")
    print(f"  - Total fixes: {total_system_fixes + total_absolute_fixes}")

    if args.dry_run:
        print("\n[DRY RUN] No files were actually modified")

    return 0


if __name__ == "__main__":
    sys.exit(main())
