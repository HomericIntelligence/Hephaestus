"""Tests for the automation loop architecture document."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ARCHITECTURE_DOC = REPO_ROOT / "docs" / "AUTOMATION_LOOP_ARCHITECTURE.md"


def test_ci_stage_documents_shipped_classifier() -> None:
    """The CI-stage doc must describe classify_ci_state as shipped code."""
    text = ARCHITECTURE_DOC.read_text(encoding="utf-8")

    assert "classify_ci_state" in text
    assert "NEW pure function" not in text
    assert "does not exist yet" not in text
    assert "shipped pure classifier" in text
    assert "tests/unit/automation/pipeline/stages/test_classify_ci_state.py" in text
