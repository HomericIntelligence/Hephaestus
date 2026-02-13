#!/usr/bin/env python3

"""
README Command Validation Script

Extracts and validates commands from README.md code blocks to ensure
documented commands actually work.

Usage:
    python scripts/validate_readme_commands.py [--level quick|comprehensive] README.md
    python scripts/validate_readme_commands.py --level quick README.md
    python scripts/validate_readme_commands.py --level comprehensive --output report.md README.md

Validation Levels:
    quick:         Syntax check and binary availability (nightly)
    comprehensive: Full command execution with timeout (weekly)

Exit codes:
    0: All validations passed
    1: One or more validation failures
"""

import argparse
import sys
from pathlib import Path

from hephaestus.validation.readme_commands import ReadmeValidator


def main() -> int:
    """
    Main entry point for README command validation.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    parser = argparse.ArgumentParser(
        description="Validate README.md commands",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "readme",
        type=Path,
        help="Path to README.md file",
    )
    parser.add_argument(
        "--level",
        choices=["quick", "comprehensive"],
        default="quick",
        help="Validation level (default: quick)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("validation-report.md"),
        help="Output report path (default: validation-report.md)",
    )

    args = parser.parse_args()

    if not args.readme.exists():
        print(f"Error: README file not found: {args.readme}", file=sys.stderr)
        return 1

    # Create validator
    validator = ReadmeValidator()

    # Extract code blocks
    print(f"Extracting code blocks from {args.readme}...")
    blocks = validator.extract_code_blocks(args.readme)
    print(f"Found {len(blocks)} code blocks")

    # Filter to executable blocks
    from hephaestus.validation.readme_commands import EXECUTE_LANGUAGES
    executable_blocks = [b for b in blocks if b.language in EXECUTE_LANGUAGES]
    print(f"Found {len(executable_blocks)} executable blocks (bash/shell/sh)")

    # Run validation
    print(f"Running {args.level} validation...")
    if args.level == "quick":
        report = validator.validate_quick(blocks)
    else:
        report = validator.validate_comprehensive(blocks)

    # Generate report
    validator.generate_report(report, args.output)
    print(f"Report written to {args.output}")

    # Summary
    print()
    print("=" * 50)
    print(f"Validation Level: {report.level.title()}")
    print(f"Commands found: {report.total_commands}")
    print(f"Commands validated: {report.passed + report.failed}")
    print(f"Commands skipped: {report.skipped_commands}")
    print(f"Passed: {report.passed}")
    print(f"Failed: {report.failed}")
    print("=" * 50)

    if report.failed > 0:
        print()
        print("VALIDATION FAILED - see report for details")
        return 1

    print()
    print("VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
