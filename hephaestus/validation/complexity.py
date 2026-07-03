"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.code.complexity`.
Retained so existing import sites and the ``hephaestus-check-complexity``
console script need no changes.
"""

import sys

from hephaestus.validation.code.complexity import (
    check_max_complexity as check_max_complexity,
    main as main,
    run_ruff_complexity_check as run_ruff_complexity_check,
)

__all__ = [
    "check_max_complexity",
    "main",
    "run_ruff_complexity_check",
]


if __name__ == "__main__":
    sys.exit(main())
