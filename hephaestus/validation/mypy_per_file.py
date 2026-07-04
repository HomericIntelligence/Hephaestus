"""Run mypy on each file individually to avoid duplicate-module-name errors.

When multiple files in different subdirectories share the same basename (e.g.
``examples/alexnet/download.py`` and ``examples/resnet/download.py``), passing them
all to a single ``mypy`` invocation causes a ``Duplicate module named`` error.

This wrapper separates mypy flags from file paths, then runs mypy once per file and
aggregates exit codes.

Usage::

    hephaestus-mypy-each-file [mypy-flags...] file1.py file2.py ...
    hephaestus-mypy-each-file --ignore-missing-imports examples/**/*.py

Exit codes:
    0  All per-file mypy runs passed
    Non-zero  At least one file failed — last non-zero exit code is returned
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, emit_json_status
from hephaestus.utils.helpers import NETWORK_TIMEOUT

# mypy flags that consume the next argument as their value.
_FLAGS_WITH_VALUE: frozenset[str] = frozenset(
    {
        "--python-version",
        "--config-file",
        "--shadow-file",
        "--exclude",
        "--package",
        "--module",
    }
)


def split_flags_and_files(args: list[str]) -> tuple[list[str], list[str]]:
    """Separate mypy flags from file paths.

    Flags start with ``-``.  Flags listed in ``_FLAGS_WITH_VALUE`` consume the
    next positional argument as their value.

    Args:
        args: Raw argument list (excluding the program name).

    Returns:
        A ``(flags, files)`` tuple.

    """
    flags: list[str] = []
    files: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            flags.append(arg)
            if arg in _FLAGS_WITH_VALUE:
                i += 1
                if i < len(args):
                    flags.append(args[i])
        else:
            files.append(arg)
        i += 1
    return flags, files


def run_mypy_per_file(
    files: list[str],
    flags: list[str] | None = None,
    python_executable: str | None = None,
) -> int:
    """Run mypy once per file and aggregate exit codes.

    Args:
        files: File paths to type-check.
        flags: Extra mypy flags to pass to every invocation.
        python_executable: Python interpreter to invoke mypy with (default: ``sys.executable``).

    Returns:
        0 if all runs passed, otherwise the last non-zero return code.

    """
    if not files:
        print("mypy-each-file: no files to check", file=sys.stderr)
        return 0

    executable = python_executable or sys.executable
    extra_flags: list[str] = flags or []

    overall_rc = 0
    for filepath in files:
        cmd = [executable, "-m", "mypy", *extra_flags, filepath]
        try:
            result = subprocess.run(cmd, capture_output=False, timeout=NETWORK_TIMEOUT)
            rc = result.returncode
        except subprocess.TimeoutExpired as exc:
            # A hung mypy run must not stall the whole check; treat it as a
            # failure for this file so the aggregate rc is non-zero (#684).
            print(
                f"mypy-each-file: {filepath} timed out after {exc.timeout}s",
                file=sys.stderr,
            )
            rc = 124
        if rc != 0:
            overall_rc = rc

    return overall_rc


def check_mypy_per_file(
    files: list[str | Path],
    flags: list[str] | None = None,
) -> int:
    """Public API: run mypy per-file and return an exit code.

    Args:
        files: File paths to type-check (strings or :class:`Path` objects).
        flags: Extra mypy flags.

    Returns:
        0 if all runs passed, otherwise the last non-zero return code.

    """
    return run_mypy_per_file([str(f) for f in files], flags=flags)


def main() -> int:
    """CLI entry point.

    Accepts mypy flags and file paths.  All flags (arguments starting with ``-``)
    are forwarded to mypy; everything else is treated as a file path.

    Returns:
        Aggregated exit code from all mypy runs.

    """
    parser = create_validation_parser(
        "Run mypy on each file individually (avoids duplicate-module-name errors)",
        include_repo_root=False,
        usage="%(prog)s [mypy-flags...] file1.py file2.py ...",
    )
    # We parse just --help / -h / --json normally; all remaining args are passed through.
    parser.parse_known_args(sys.argv[1:])

    raw_args = sys.argv[1:]
    json_mode = "--json" in raw_args
    # Strip --help / --json handled above so they don't end up as a file path or
    # get forwarded to mypy (which does not accept --json).
    raw_args = [a for a in raw_args if a not in ("-h", "--help", "--json")]

    flags, files = split_flags_and_files(raw_args)
    exit_code = run_mypy_per_file(files, flags=flags)
    if json_mode:
        emit_json_status(exit_code, files_checked=len(files))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
