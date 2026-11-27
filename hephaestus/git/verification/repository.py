"""Repository-scoped view of the canonical host verification specs.

Other repositories have a different rebase contract. The argv and path below
are the canonical Hephaestus values; a coordinator injects them into the
shared worker policy.
"""

from __future__ import annotations

from .specs import (
    REBASE_STRUCTURAL_TEST as REBASE_STRUCTURAL_TEST,
    REBASE_STRUCTURAL_TEST_ARGV as REBASE_STRUCTURAL_TEST_ARGV,
    REBASE_STRUCTURAL_TEST_PATH as REBASE_STRUCTURAL_TEST_PATH,
)

__all__ = [
    "HEPHAESTUS_REPOSITORY",
    "REBASE_STRUCTURAL_TEST",
    "REBASE_STRUCTURAL_TEST_ARGV",
    "REBASE_STRUCTURAL_TEST_PATH",
    "is_hephaestus_repository",
]

HEPHAESTUS_REPOSITORY = "HomericIntelligence/Hephaestus"


def is_hephaestus_repository(repository: str) -> bool:
    """Return whether one repository identity selects the Hephaestus policy."""
    return isinstance(repository, str) and repository.casefold() == HEPHAESTUS_REPOSITORY.casefold()
