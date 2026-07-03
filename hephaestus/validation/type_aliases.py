"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.code.type_aliases`.
Retained so existing import sites and the ``hephaestus-check-type-aliases``
console script need no changes.
"""

import sys

from hephaestus.validation.code.type_aliases import (
    check_files as check_files,
    detect_shadowing as detect_shadowing,
    format_error as format_error,
    is_shadowing_pattern as is_shadowing_pattern,
    main as main,
)

__all__ = [
    "check_files",
    "detect_shadowing",
    "format_error",
    "is_shadowing_pattern",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
