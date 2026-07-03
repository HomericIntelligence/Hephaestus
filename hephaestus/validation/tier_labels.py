"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.tiers.tier_labels`.
Retained so existing import sites and the ``hephaestus-check-tier-labels``
console script need no changes.
"""

import sys

from hephaestus.validation.tiers.tier_labels import (
    BAD_PATTERNS as BAD_PATTERNS,
    CANONICAL_TIERS as CANONICAL_TIERS,
    TierLabelFinding as TierLabelFinding,
    check_tier_label_consistency as check_tier_label_consistency,
    find_violations as find_violations,
    format_json as format_json,
    format_report as format_report,
    main as main,
    scan_repository as scan_repository,
)

__all__ = [
    "BAD_PATTERNS",
    "CANONICAL_TIERS",
    "TierLabelFinding",
    "check_tier_label_consistency",
    "find_violations",
    "format_json",
    "format_report",
    "main",
    "scan_repository",
]


if __name__ == "__main__":
    sys.exit(main())
