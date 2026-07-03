"""Code quality validators.

Merges the type-alias-shadowing, cyclomatic-complexity, and per-file mypy
checks into one focused subpackage. The original
``hephaestus.validation.<module>`` paths remain as thin re-export shims for
backward compatibility.
"""

from hephaestus.validation.code.complexity import (
    check_max_complexity as check_max_complexity,
    run_ruff_complexity_check as run_ruff_complexity_check,
)
from hephaestus.validation.code.mypy_per_file import (
    check_mypy_per_file as check_mypy_per_file,
    run_mypy_per_file as run_mypy_per_file,
    split_flags_and_files as split_flags_and_files,
)
from hephaestus.validation.code.type_aliases import (
    check_files as check_files,
    detect_shadowing as detect_shadowing,
    format_error as format_error,
    is_shadowing_pattern as is_shadowing_pattern,
)

__all__ = [
    "check_files",
    "check_max_complexity",
    "check_mypy_per_file",
    "detect_shadowing",
    "format_error",
    "is_shadowing_pattern",
    "run_mypy_per_file",
    "run_ruff_complexity_check",
    "split_flags_and_files",
]
