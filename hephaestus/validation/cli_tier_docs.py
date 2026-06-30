"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.tiers.cli_tier_docs`.
Retained so existing import sites and the ``hephaestus-check-cli-tier-docs``
console script need no changes.
"""

import sys

from hephaestus.validation.tiers.cli_tier_docs import (
    TierDocFinding as TierDocFinding,
    find_duplicate_tiers as find_duplicate_tiers,
    find_violations as find_violations,
    format_json as format_json,
    format_report as format_report,
    load_documented_tiers as load_documented_tiers,
    load_pyproject_scripts as load_pyproject_scripts,
    main as main,
)

__all__ = [
    "TierDocFinding",
    "find_duplicate_tiers",
    "find_violations",
    "format_json",
    "format_report",
    "load_documented_tiers",
    "load_pyproject_scripts",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
