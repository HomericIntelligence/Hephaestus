"""Regression tests for pipeline compatibility retirement documentation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ARCHITECTURE = ROOT / "docs" / "AUTOMATION_LOOP_ARCHITECTURE.md"
PR_REVIEW = ROOT / "hephaestus" / "automation" / "pipeline" / "stages" / "pr_review.py"


def test_retained_pipeline_compatibility_has_removal_gates() -> None:
    """Retained compatibility branches must have observable retirement criteria."""
    text = ARCHITECTURE.read_text(encoding="utf-8")

    assert "## Legacy compatibility inventory and retirement gates" in text
    assert "`legacy_issue_impl_go_fallback`" in text
    assert "`already_implementation_go_pr`" in text
    assert "`not_implementation_go`" in text
    assert "#2055" in text
    assert "zero fallback observations" in text
    assert "zero open legacy implementation-GO PRs" in text


def test_retired_followup_mini_states_are_absent_from_active_stage() -> None:
    """The active PR-review stage must not retain unreachable follow-up states."""
    source = PR_REVIEW.read_text(encoding="utf-8")

    assert "FOLLOWUP_WAIT" not in source
    assert "PR_FINISH" not in source
