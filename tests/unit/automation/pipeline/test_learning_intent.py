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


@pytest.mark.parametrize(
    "intent",
    [
        work_item.LearningIntent.approved_plan(
            repo="HomericIntelligence/ProjectHephaestus",
            issue=2754,
            plan_revision=4,
            plan_fingerprint="a" * 64,
        ),
        work_item.LearningIntent.post_merge(
            repo="HomericIntelligence/ProjectHephaestus",
            issue=2754,
            pr=2800,
        ),
    ],
)
def test_learning_intent_payload_round_trips_closed_schema(
    intent: work_item.LearningIntent,
) -> None:
    """Host preparation receives one complete, self-authenticating intent."""
    payload = intent.to_payload()

    assert payload["intent_key"] == intent.key
    assert work_item.LearningIntent.from_payload(payload) == intent


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "approved_plan",
            "repo": "acme/repo",
            "issue": 1,
            "intent_key": "approved_plan:bad",
            "plan_revision": 1,
            "plan_fingerprint": "a" * 64,
            "unknown": True,
        },
        {
            "kind": "approved_plan",
            "repo": "acme/repo",
            "issue": 1,
            "intent_key": "approved_plan:bad",
            "plan_revision": 1,
            "plan_fingerprint": "a" * 64,
            "pr": 2,
        },
        {
            "kind": "post_merge",
            "repo": "acme/repo",
            "issue": 1,
            "intent_key": "post_merge:bad",
            "pr": 2,
            "plan_revision": 1,
        },
    ],
)
def test_learning_intent_payload_rejects_unknown_and_cross_kind_fields(
    payload: dict[str, object],
) -> None:
    """Untrusted payloads cannot widen the preparation contract."""
    with pytest.raises(ValueError, match="learning intent payload"):
        work_item.LearningIntent.from_payload(payload)
