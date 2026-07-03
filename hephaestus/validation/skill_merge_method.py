"""Backward-compatibility shim.

Canonical implementation:
:mod:`hephaestus.validation.skills.skill_merge_method`.
Retained so existing import sites and the
``python -m hephaestus.validation.skill_merge_method`` invocation
(``.pre-commit-config.yaml``) need no changes.
"""

import sys

from hephaestus.validation.skills.skill_merge_method import (
    FENCE as FENCE,
    HARDCODED as HARDCODED,
    MARKER as MARKER,
    main as main,
    scan as scan,
)

__all__ = [
    "FENCE",
    "HARDCODED",
    "MARKER",
    "main",
    "scan",
]


if __name__ == "__main__":
    sys.exit(main())
