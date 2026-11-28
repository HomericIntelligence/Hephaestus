"""Behavior guard for the older-generation automation import surface."""

from __future__ import annotations

from hephaestus.automation import athena_jobs
from hephaestus.automation.learning_intent import LearningIntent, LearningIntentKind
from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError, valid_delivery_receipt
from hephaestus.automation.pipeline import athena_skill_jobs, work_item


def test_pipeline_athena_names_keep_object_identity() -> None:
    """The retained pipeline path binds the canonical Athena objects."""
    for name in ("AthenaSkillJob", "AthenaSkillRequest", "AthenaSkillResult"):
        assert getattr(athena_skill_jobs, name) is getattr(athena_jobs, name)


def test_pipeline_learning_intent_names_keep_object_identity() -> None:
    """The retained work-item path binds the canonical learning intent objects."""
    assert work_item.LearningIntent is LearningIntent
    assert work_item.LearningIntentKind is LearningIntentKind


def test_mnemosyne_delivery_contracts_keep_object_identity() -> None:
    """The retained delivery path binds the neutral contracts."""
    assert LearnDeliveryError is athena_jobs.LearnDeliveryError
    assert valid_delivery_receipt is athena_jobs.valid_delivery_receipt


def test_old_learning_intent_path_round_trips_a_journal_payload() -> None:
    """The older caller can still parse an intent built through the canonical path."""
    intent = work_item.LearningIntent.post_merge(
        repo="HomericIntelligence/Hephaestus", issue=9, pr=10
    )
    parsed = LearningIntent.from_payload(intent.to_payload())
    assert parsed.key == intent.key
    assert parsed.journal_identity() == intent.journal_identity()
