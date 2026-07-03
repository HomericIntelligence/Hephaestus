"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.code.mypy_per_file`.
Retained so existing import sites and the ``hephaestus-mypy-each-file``
console script need no changes.
"""

import sys

from hephaestus.validation.code.mypy_per_file import (
    check_mypy_per_file as check_mypy_per_file,
    main as main,
    run_mypy_per_file as run_mypy_per_file,
    split_flags_and_files as split_flags_and_files,
)

__all__ = [
    "check_mypy_per_file",
    "main",
    "run_mypy_per_file",
    "split_flags_and_files",
]


if __name__ == "__main__":
    sys.exit(main())
