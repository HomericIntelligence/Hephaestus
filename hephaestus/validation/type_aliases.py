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

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, format_output


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


@dataclass
class _ScanResult:
    """Keep findings and read failures separate."""

    violations: list[tuple[int, str, str, str]]
    read_errors: list[str]


@dataclass
class _BatchResult:
    """Collect diagnostics for all selected files."""

    violations: list[str]
    read_errors: list[str]

    @property
    def exit_code(self) -> int:
        return int(bool(self.violations or self.read_errors))


def detect_shadowing(file_path: Path) -> list[tuple[int, str, str, str]]:
    """Find type alias shadowing violations in a Python file.

    Args:
        file_path: Path to the Python file to check.

    Returns:
        Tuples of line number, line content, alias, and target. A read failure
        prints a warning and returns findings collected before the failure.
        A missing file returns an empty list.

    """
    result = _scan_file(file_path)
    for error in result.read_errors:
        print(f"Warning: {error}", file=sys.stderr)
    return result.violations


def _scan_file(file_path: Path) -> _ScanResult:
    """Scan one file and retain findings if a read fails."""
    violations: list[tuple[int, str, str, str]] = []
    read_errors: list[str] = []
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
        read_errors.append(f"Could not read {file_path}: {e}")

    return _ScanResult(violations, read_errors)


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
        Tuple of ``(exit_code, error_messages)``. Exit code 1 means that a
        violation or read failure occurred. Diagnostics include both kinds.

    """
    result = _check_files(file_paths)
    return result.exit_code, result.violations + result.read_errors


def _record_read_error(result: _BatchResult, path: Path, error: OSError) -> None:
    """Record one filesystem access failure."""
    result.read_errors.append(f"Could not read {path}: {error}")


def _has_python_suffix(name: str) -> bool:
    """Use platform case rules to identify a Python file name."""
    return os.path.normcase(name).endswith(os.path.normcase(".py"))


def _collect_python_files(directory: Path, result: _BatchResult) -> list[Path]:
    """Find regular Python files without following directory links."""
    files: list[Path] = []
    pending = [directory]

    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    try:
                        if entry.is_junction():
                            pending.append(entry_path)
                            continue
                        mode = entry.stat(follow_symlinks=False).st_mode
                        if stat.S_ISDIR(mode):
                            pending.append(entry_path)
                        elif stat.S_ISREG(mode) and _has_python_suffix(entry.name):
                            files.append(entry_path)
                        elif stat.S_ISLNK(mode) and _has_python_suffix(entry.name):
                            try:
                                target_mode = entry.stat().st_mode
                            except OSError as error:
                                _record_read_error(result, entry_path, error)
                                continue
                            if stat.S_ISREG(target_mode):
                                files.append(entry_path)
                    except OSError as error:
                        _record_read_error(result, entry_path, error)
        except OSError as error:
            _record_read_error(result, current, error)

    return files


def _select_missing_path(path: Path, result: _BatchResult, error: FileNotFoundError) -> list[Path]:
    """Select a missing Python path or record an incomplete explicit input."""
    if path.suffix == ".py":
        return [path]
    _record_read_error(result, path, error)
    return []


def _select_path(path: Path, result: _BatchResult) -> list[Path]:
    """Select Python files from one explicit input path."""
    try:
        if path.is_junction():
            return _collect_python_files(path, result)
        mode = path.stat(follow_symlinks=False).st_mode
    except FileNotFoundError as error:
        return _select_missing_path(path, result, error)
    except OSError as error:
        _record_read_error(result, path, error)
        return []

    if stat.S_ISDIR(mode):
        return _collect_python_files(path, result)
    if stat.S_ISREG(mode) and path.suffix == ".py":
        return [path]
    if not stat.S_ISLNK(mode):
        return []

    try:
        target_mode = path.stat().st_mode
    except FileNotFoundError as error:
        return _select_missing_path(path, result, error)
    except OSError as error:
        _record_read_error(result, path, error)
        return []
    if stat.S_ISDIR(target_mode):
        return _collect_python_files(path, result)
    if stat.S_ISREG(target_mode) and path.suffix == ".py":
        return [path]
    return []


def _check_files(file_paths: list[Path]) -> _BatchResult:
    """Scan each selected file once and collect all diagnostics."""
    result = _BatchResult([], [])

    files_to_check: list[Path] = []
    for path in file_paths:
        files_to_check.extend(_select_path(path, result))

    for file_path in files_to_check:
        scan = _scan_file(file_path)
        result.read_errors.extend(scan.read_errors)
        for line_num, line, alias, target in scan.violations:
            error_msg = format_error(file_path, line_num, line, alias, target)
            result.violations.append(error_msg)

    return result


def main() -> int:
    """CLI entry point for type alias shadowing detection.

    Returns:
        Exit code 0 for a complete scan without violations, or 1 for a
        violation or read failure.

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
        help="Files or directories to check",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print verbose output",
    )

    args = parser.parse_args()

    if args.verbose and not args.json:
        print(f"Checking {len(args.paths)} path(s) for type alias shadowing...")

    result = _check_files(args.paths)
    exit_code = result.exit_code
    errors = result.violations

    if args.json:
        report = {
            "paths": [str(p) for p in args.paths],
            "violations": errors,
            "violation_count": len(errors),
            "read_errors": result.read_errors,
            "read_error_count": len(result.read_errors),
            "scan_complete": not result.read_errors,
            "exit_code": exit_code,
            "passed": exit_code == 0,
        }
        print(format_output(report, "json"))
        return exit_code

    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"\nFound {len(errors)} type alias shadowing violation(s)", file=sys.stderr)

    if result.read_errors:
        print("\n".join(result.read_errors), file=sys.stderr)
        print(f"Scan incomplete: {len(result.read_errors)} read error(s)", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
