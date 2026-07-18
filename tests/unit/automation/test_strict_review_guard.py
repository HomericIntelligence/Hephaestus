"""Tests for the host-local strict-review ownership guard."""

from pathlib import Path

from hephaestus.automation.strict_review_guard import StrictReviewGuard


def test_guard_serializes_same_pr_across_loop_instances(tmp_path: Path) -> None:
    """A second loop cannot review the same PR until the first releases it."""
    first = StrictReviewGuard(tmp_path)
    second = StrictReviewGuard(tmp_path)

    assert first.try_claim("Homer", "Hephaestus", 42, owner=101)
    assert not second.try_claim("Homer", "Hephaestus", 42, owner=202)

    first.release("Homer", "Hephaestus", 42, owner=101)

    assert second.try_claim("Homer", "Hephaestus", 42, owner=202)
    second.release("Homer", "Hephaestus", 42, owner=202)
