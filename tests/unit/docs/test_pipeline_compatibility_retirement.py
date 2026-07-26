"""Regression tests for active pipeline stage structure."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PR_REVIEW = ROOT / "hephaestus" / "automation" / "pipeline" / "stages" / "pr_review.py"


def test_inactive_followup_mini_states_are_absent_from_active_stage() -> None:
    """The active PR-review stage must not retain unreachable follow-up states."""
    source = PR_REVIEW.read_text(encoding="utf-8")

    assert "FOLLOWUP_WAIT" not in source
    assert "PR_FINISH" not in source
