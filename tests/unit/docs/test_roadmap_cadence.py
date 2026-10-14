"""Integration coverage for the roadmap maintenance contract."""

from __future__ import annotations

from pathlib import Path

from hephaestus.validation.doc_maintenance import validate_roadmap_maintenance

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_repository_roadmap_satisfies_maintenance_contract() -> None:
    """The checked-in roadmap has an owner, trigger, source, and fresh period."""
    assert validate_roadmap_maintenance(REPO_ROOT) == []
