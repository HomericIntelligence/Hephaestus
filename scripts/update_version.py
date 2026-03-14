#!/usr/bin/env python3

"""Update version across project version files.

This script updates version numbers in:
- VERSION (root file)
- __init__.py (__version__ attribute)

Usage:
    python3 scripts/update_version.py <new_version>
    python3 scripts/update_version.py 0.2.0
    python3 scripts/update_version.py 0.2.0 --verify-only
"""

import argparse
import sys

from hephaestus.utils.helpers import get_repo_root
from hephaestus.version.manager import VersionManager, parse_version


def main() -> int:
    """Main entry point for version update script.

    Returns:
        0 on success, 1 on failure

    """
    parser = argparse.ArgumentParser(
        description="Update project version across all version files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "version", help="New version string (format: MAJOR.MINOR.PATCH, e.g., 0.1.0)"
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify version consistency, don't update",
    )

    args = parser.parse_args()

    try:
        # Parse and validate version
        _major, _minor, _patch = parse_version(args.version)

        # Get repository root
        repo_root = get_repo_root()
        print(f"Repository root: {repo_root}\n")

        # Create version manager
        version_manager = VersionManager(repo_root=repo_root)

        if args.verify_only:
            # Verify only
            if version_manager.verify(args.version):
                print("\n✅ All version files are consistent")
                return 0
            else:
                print("\n❌ Version files are inconsistent")
                return 1
        else:
            # Update all version files
            version_manager.update(args.version)

            # Verify updates
            if version_manager.verify(args.version):
                print("\n✅ All version files updated successfully")
                return 0
            else:
                print("\n❌ Version update incomplete")
                return 1

    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"UNEXPECTED ERROR: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
