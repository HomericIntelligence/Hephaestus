"""Tests for ``hephaestus.automation.label_taxonomy``."""

from __future__ import annotations

from pathlib import Path

from hephaestus.automation.label_taxonomy import (
    REQUIRED_REPOSITORY_LABEL_SPECS,
    TECH_DEBT_LABEL,
    TECH_DEBT_LABEL_SPECS,
    WONTFIX_LABEL,
)
from hephaestus.automation.state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    ALL_STATE_LABELS,
    STATE_LABEL_SPECS,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestRepositoryLabelTaxonomy:
    """The repository taxonomy keeps debt labels separate from state labels."""

    def test_repository_taxonomy_includes_debt_labels(self) -> None:
        assert set(STATE_LABEL_SPECS) <= set(REQUIRED_REPOSITORY_LABEL_SPECS)
        assert TECH_DEBT_LABEL in REQUIRED_REPOSITORY_LABEL_SPECS
        assert WONTFIX_LABEL in REQUIRED_REPOSITORY_LABEL_SPECS

    def test_debt_labels_are_not_state_labels(self) -> None:
        assert TECH_DEBT_LABEL not in ALL_STATE_LABELS
        assert WONTFIX_LABEL not in ALL_STATE_LABELS
        assert TECH_DEBT_LABEL not in ALL_IMPLEMENTATION_STATE_LABELS
        assert WONTFIX_LABEL not in ALL_IMPLEMENTATION_STATE_LABELS

    def test_debt_label_specs_are_well_formed(self) -> None:
        for label, spec in TECH_DEBT_LABEL_SPECS.items():
            assert label in {TECH_DEBT_LABEL, WONTFIX_LABEL}
            assert len(spec["color"]) == 6
            int(spec["color"], 16)
            assert len(spec["description"]) <= 100

    def test_docs_reference_the_documented_taxonomy(self) -> None:
        text = (REPO_ROOT / "docs" / "TECH_DEBT.md").read_text(encoding="utf-8")
        assert TECH_DEBT_LABEL in text
        assert WONTFIX_LABEL in text
        assert "hephaestus-ensure-state-labels" in text
