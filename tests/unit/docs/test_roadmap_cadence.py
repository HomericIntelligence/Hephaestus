"""Regression test: ROADMAP.md must define an explicit iteration cadence.

Issue #1493 (S10 Planning, MODULARITY): the roadmap previously said it was
"reviewed and updated at the end of each release cycle (typically monthly)"
without defining the trigger, whether releases are date- or feature-driven,
or who owns the review. This guard fails if that section regresses to vague
prose. It asserts the HARD invariant (required phrases are present), never an
unverifiable cadence value like "monthly".
"""

from pathlib import Path

from hephaestus.validation.doc_maintenance import validate_roadmap_maintenance

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASING_MD = REPO_ROOT / "docs" / "RELEASING.md"


def test_repository_roadmap_satisfies_maintenance_contract() -> None:
    """The checked-in roadmap has an owner, trigger, source, and fresh focus."""
    assert validate_roadmap_maintenance(REPO_ROOT) == []


def test_release_checklist_owns_roadmap_refresh() -> None:
    """The release maintainer's checklist must include the roadmap review."""
    checklist = RELEASING_MD.read_text(encoding="utf-8").lower()

    assert "docs/roadmap.md" in checklist
    assert "open epics" in checklist
    assert "audit-finding" in checklist
