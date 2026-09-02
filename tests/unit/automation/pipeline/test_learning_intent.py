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
    "intent",
    [
        work_item.LearningIntent.approved_plan(
            repo="comet",
            issue=813,
            plan_revision=4,
            plan_fingerprint="a" * 64,
        ),
        work_item.LearningIntent.post_merge(repo="comet", issue=813, pr=900),
    ],
)
def test_learning_intent_qualifies_delivery_without_changing_durable_key(
    intent: work_item.LearningIntent,
) -> None:
    """The host gets owner/name while the journal identity stays unchanged."""
    original_key = intent.key

    payload = intent.to_payload(owner="LLM360")
    parsed = work_item.LearningIntent.from_payload(payload)

    assert payload["repo"] == "LLM360/comet"
    assert payload["identity_repo"] == "comet"
    assert payload["intent_key"] == original_key
    assert parsed.repo == "LLM360/comet"
    assert parsed.key == original_key
    assert parsed.journal_identity()["repo"] == "comet"


def test_recovered_learning_intent_keeps_key_when_delivery_adds_owner() -> None:
    """Restart recovery keeps the short key at the qualified host boundary."""
    original = work_item.LearningIntent.post_merge(repo="comet", issue=813, pr=900)
    record = {
        "key": original.key,
        "kind": original.kind.value,
        **original.journal_identity(),
    }

    recovered = work_item.LearningIntent.from_journal(record)
    payload = recovered.to_payload(owner="LLM360")

    assert recovered.key == original.key
    assert payload["intent_key"] == original.key
    assert payload["repo"] == "LLM360/comet"
    assert payload["identity_repo"] == "comet"


@pytest.mark.parametrize("repo", ["Other/comet", "bad/name/extra", "bad name"])
def test_learning_intent_rejects_foreign_or_malformed_delivery_repository(repo: str) -> None:
    """The trusted boundary cannot send a foreign or malformed repository."""
    intent = work_item.LearningIntent.post_merge(repo=repo, issue=813, pr=900)

    with pytest.raises(ValueError, match="repository identity"):
        intent.to_payload(owner="LLM360")


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
