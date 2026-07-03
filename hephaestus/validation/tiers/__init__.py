"""Tier consistency validators.

Merges the tier-label-consistency and CLI-tier-docs checks into one focused
subpackage. The original ``hephaestus.validation.<module>`` paths remain as
thin re-export shims for backward compatibility.
"""

from hephaestus.validation.tiers.cli_tier_docs import (
    TierDocFinding as TierDocFinding,
    find_duplicate_tiers as find_duplicate_tiers,
    load_documented_tiers as load_documented_tiers,
    load_pyproject_scripts as load_pyproject_scripts,
)
from hephaestus.validation.tiers.tier_labels import (
    TierLabelFinding as TierLabelFinding,
    check_tier_label_consistency as check_tier_label_consistency,
    scan_repository as scan_repository,
)

__all__ = [
    "TierDocFinding",
    "TierLabelFinding",
    "check_tier_label_consistency",
    "find_duplicate_tiers",
    "load_documented_tiers",
    "load_pyproject_scripts",
    "scan_repository",
]
