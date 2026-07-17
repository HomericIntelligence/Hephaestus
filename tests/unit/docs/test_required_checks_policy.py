"""Regression tests for the required-checks documentation boundary."""

from pathlib import Path

POLICY_DOC = Path(__file__).resolve().parents[3] / "docs" / "ci" / "required-checks.md"


def test_policy_lists_only_code_validation_contexts() -> None:
    """The runbook cannot reintroduce an automation-loop approval check."""
    text = POLICY_DOC.read_text(encoding="utf-8")

    assert "`required-checks-gate`" in text
    assert "`pr-policy`" in text
    assert "strict-review" + "-proof" not in text


def test_policy_keeps_ci_observation_optional_for_the_loop() -> None:
    """No configured CI is an explicitly supported automation-loop mode."""
    text = POLICY_DOC.read_text(encoding="utf-8")

    assert "NO_CHECKS" in text
    assert "$athena:pr-review" in text
