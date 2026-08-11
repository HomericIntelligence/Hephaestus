"""Behavior tests for durable auxiliary-learning intent state."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hephaestus.automation.arming_state as arming_state


def test_learning_journal_commits_only_one_claim(tmp_path: Path) -> None:
    """A durable pending intent can be claimed exactly once."""
    assert hasattr(arming_state, "LearningJournalStore")
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    store.ensure_pending("approved-plan-key", kind="approved_plan")

    assert store.claim("approved-plan-key") is True
    assert store.claim("approved-plan-key") is False
    record = store.load("approved-plan-key")
    assert record is not None and record["status"] == "claimed"


def test_learning_journal_reconstructs_and_disables_one_issue(tmp_path: Path) -> None:
    """Recovery lists only matching nonterminal identity records."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    identity = {"repo": "Hephaestus", "issue": 2705, "pr": 12}
    store.ensure_pending("post-merge-key", kind="post_merge", identity=identity)
    store.ensure_pending(
        "other-key", kind="post_merge", identity={"repo": "Hephaestus", "issue": 1}
    )

    records = store.incomplete_for_issue(repo="Hephaestus", issue=2705)

    assert [record["key"] for record in records] == ["post-merge-key"]
    store.disable("post-merge-key")
    record = store.load("post-merge-key")
    assert record is not None and record["status"] == "failed"
    assert store.incomplete_for_issue(repo="Hephaestus", issue=2705) == []


def test_learning_journal_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    """The lock makes concurrent claim attempts exactly once."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    store.ensure_pending("key", kind="approved_plan")

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _index: store.claim("key"), range(32)))

    assert results.count(True) == 1
    assert results.count(False) == 31


def test_learning_journal_rejects_invalid_terminal_transition(tmp_path: Path) -> None:
    """A pending record cannot become terminal without a committed claim."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    store.ensure_pending("key", kind="approved_plan")

    with pytest.raises(ValueError, match="cannot finish"):
        store.finish("key", succeeded=True)
