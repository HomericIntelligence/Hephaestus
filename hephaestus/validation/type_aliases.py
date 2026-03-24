"""Detect type alias shadowing patterns in Python code.

Detects anti-patterns where a type alias shadows a more specific domain name,
making code less explicit and harder to understand.

Examples of flagged patterns::

    Result = DomainResult        # Generic name shadows specific domain name
    RunResult = ExecutorRunResult  # Removes domain context

Examples of allowed patterns::

    AggregatedStats = Statistics  # Different name, legitimate abbreviation
    Result = MetricsResult       # Not a suffix relationship
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def is_shadowing_pattern(alias: str, target: str) -> bool:
    """Check if alias name shadows the target name.

    A shadowing pattern occurs when the alias name is a suffix of the target name,
    indicating that meaningful context is being removed.

    Args:
        alias: The alias name (left side of assignment).
        target: The target name (right side of assignment).

    Returns:
        True if the alias shadows the target, False otherwise.

    """
    target_lower = target.lower()
    alias_lower = alias.lower()

    if target_lower == alias_lower:
        return False

    return target_lower.endswith(alias_lower)


def _update_string_state(
    stripped: str, in_string: bool, string_delimiter: str | None
) -> tuple[bool, str | None]:
    """Track whether we are inside a triple-quoted string."""
    for delim in ('"""', "'''"):
        if delim in stripped:
            if in_string and string_delimiter == delim:
                return False, None
            if not in_string:
                return True, delim
    return in_string, string_delimiter


def detect_shadowing(file_path: Path) -> list[tuple[int, str, str, str]]:
    """Find type alias shadowing violations in a Python file.

    Args:
        file_path: Path to Python file to check.

    Returns:
        List of tuples ``(line_number, line_content, alias, target)`` for each
        violation.

    """
    violations: list[tuple[int, str, str, str]] = []
    pattern = re.compile(r"^([A-Z][a-zA-Z0-9_]*)\s*=\s*([A-Z][a-zA-Z0-9_]*)\s*(?:#.*)?$")

    try:
        with open(file_path, encoding="utf-8") as f:
            in_string = False
            string_delimiter: str | None = None

            for line_num, line in enumerate(f, start=1):
                stripped = line.strip()
                in_string, string_delimiter = _update_string_state(
                    stripped, in_string, string_delimiter
                )

                if in_string:
                    continue

                if "# type: ignore[shadowing]" in line or "# noqa: shadowing" in line:
                    continue

                match = pattern.match(stripped)
                if match:
                    alias = match.group(1)
                    target = match.group(2)
                    if is_shadowing_pattern(alias, target):
                        violations.append((line_num, stripped, alias, target))

    except (OSError, UnicodeDecodeError) as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)

    return violations


def format_error(file_path: Path, line_num: int, line: str, alias: str, target: str) -> str:
    """Format a violation as an error message.

    Args:
        file_path: Path to file containing violation.
        line_num: Line number of violation.
        line: Full line content.
        alias: Alias name.
        target: Target name.

    Returns:
        Formatted error message string.

    """
    return (
        f"{file_path}:{line_num}: Type alias shadows domain-specific name\n"
        f"  {line}\n"
        f"  Suggestion: Use '{target}' directly instead of aliasing to '{alias}'\n"
        f"  To suppress this check, add: # type: ignore[shadowing]"
    )


def check_files(file_paths: list[Path]) -> tuple[int, list[str]]:
    """Check multiple files for type alias shadowing.

    Args:
        file_paths: List of file or directory paths to check.

    Returns:
        Tuple of ``(exit_code, error_messages)``.

    """
    all_violations: list[str] = []

    files_to_check: list[Path] = []
    for path in file_paths:
        if path.is_dir():
            files_to_check.extend(path.rglob("*.py"))
        elif path.suffix == ".py":
            files_to_check.append(path)

    for file_path in files_to_check:
        violations = detect_shadowing(file_path)
        for line_num, line, alias, target in violations:
            error_msg = format_error(file_path, line_num, line, alias, target)
            all_violations.append(error_msg)

    if all_violations:
        return 1, all_violations
    return 0, []


def main() -> int:
    """CLI entry point for type alias shadowing detection.

    Returns:
        Exit code (0 if clean, 1 if violations found).

    """
    parser = argparse.ArgumentParser(
        description="Detect type alias shadowing patterns in Python code",
        epilog="Example: %(prog)s src/ tests/ scripts/",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Files or directories to check",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print verbose output",
    )

    args = parser.parse_args()

    if args.verbose:
        print(f"Checking {len(args.paths)} path(s) for type alias shadowing...")

    exit_code, errors = check_files(args.paths)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"\nFound {len(errors)} type alias shadowing violation(s)", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
