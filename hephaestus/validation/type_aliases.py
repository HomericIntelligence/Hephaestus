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

import re
import sys
from pathlib import Path

from hephaestus.cli.localization import text
from hephaestus.cli.utils import create_validation_parser, format_output

_TYPE_ALIAS_ERROR = re.compile(
    r"^(?P<path>.+):(?P<line>\d+): Type alias shadows domain-specific name\n"
    r"  (?P<source>.*)\n"
    r"  Suggestion: Use '(?P<target>.*)' directly instead of aliasing to '(?P<alias>.*)'\n"
    r"  To suppress this check, add: # type: ignore\[shadowing\]$",
    re.DOTALL,
)


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
        print(
            text("Warning: Could not read %(value0)s: %(value1)s", value0=file_path, value1=e),
            file=sys.stderr,
        )

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
        "  To suppress this check, add: # type: ignore[shadowing]"
    )


def format_human_error(error: str) -> str:
    """Translate a raw type-alias finding for the human-only CLI report."""
    match = _TYPE_ALIAS_ERROR.fullmatch(error)
    if match is None:
        return error
    values = match.groupdict()
    return "\n".join(
        [
            text(
                "%(path)s:%(line)d: Type alias shadows domain-specific name",
                path=values["path"],
                line=int(values["line"]),
            ),
            text("  %(value)s", value=values["source"]),
            text(
                "  Suggestion: Use '%(target)s' directly instead of aliasing to '%(alias)s'",
                target=values["target"],
                alias=values["alias"],
            ),
            text("  To suppress this check, add: # type: ignore[shadowing]"),
        ]
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
    parser = create_validation_parser(
        "Detect type alias shadowing patterns in Python code",
        include_repo_root=False,
        epilog="Example: %(prog)s src/ tests/ scripts/",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help=text("Files or directories to check"),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help=text("Print verbose output"),
    )

    args = parser.parse_args()

    if args.verbose and not args.json:
        print(
            text("Checking %(value0)s path(s) for type alias shadowing...", value0=len(args.paths))
        )

    exit_code, errors = check_files(args.paths)

    if args.json:
        report = {
            "paths": [str(p) for p in args.paths],
            "violations": errors,
            "violation_count": len(errors),
            "exit_code": exit_code,
            "passed": exit_code == 0,
        }
        print(format_output(report, "json"))
        return exit_code

    if errors:
        print("\n".join(format_human_error(error) for error in errors), file=sys.stderr)
        print(
            text("\nFound %(value0)s type alias shadowing violation(s)", value0=len(errors)),
            file=sys.stderr,
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
