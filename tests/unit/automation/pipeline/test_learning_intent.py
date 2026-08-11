"""Behavior tests for immutable learning intents."""

from __future__ import annotations

import dataclasses

import pytest

import hephaestus.automation.pipeline.work_item as work_item


def test_learning_intent_key_is_stable_and_immutable() -> None:
    """Equivalent identities produce one stable key and cannot be changed."""
    assert hasattr(work_item, "LearningIntent")
    first = work_item.LearningIntent.approved_plan(
        repo="Hephaestus", issue=2705, plan_revision=8, plan_fingerprint="abc"
    )
    second = work_item.LearningIntent.approved_plan(
        repo="Hephaestus", issue=2705, plan_revision=8, plan_fingerprint="abc"
    )

    assert first.key == second.key
    try:
        first.issue = 1  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("LearningIntent must be immutable")


def test_learning_intent_rejects_tampered_journal_identity() -> None:
    """Recovery cannot bind a stored key to different intent metadata."""
    intent = work_item.LearningIntent.post_merge(repo="Hephaestus", issue=2705, pr=12)
    record = {
        "key": intent.key,
        "kind": intent.kind.value,
        **intent.journal_identity(),
        "pr": 13,
    }

    with pytest.raises(ValueError, match="identity"):
        work_item.LearningIntent.from_journal(record)
