"""Behavior tests for durable auxiliary-learning intent state."""

from __future__ import annotations

import json
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


def test_learning_journal_does_not_overwrite_corrupt_record(tmp_path: Path) -> None:
    """A present invalid record is evidence, not an absent intent."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    path = store.path("key")
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(arming_state.LearningJournalError, match="invalid JSON"):
        store.ensure_pending("key", kind="approved_plan")

    assert path.read_text(encoding="utf-8") == "{not-json"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cleanup_status", "unknown", "invalid cleanup status"),
        ("post_processing", "not-an-object", "invalid post-processing data"),
    ],
)
def test_learning_journal_rejects_invalid_cleanup_state(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    """Recovery rejects malformed cleanup state before it can delete files."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    store.ensure_pending("key", kind="post_merge")
    path = store.path("key")
    record = json.loads(path.read_text(encoding="utf-8"))
    record[field] = value
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(arming_state.LearningJournalError, match=message):
        store.load("key")


def test_learning_journal_identity_cannot_replace_reserved_fields(tmp_path: Path) -> None:
    """Caller identity cannot change journal protocol fields."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)

    with pytest.raises(ValueError, match="reserved"):
        store.ensure_pending(
            "key",
            kind="approved_plan",
            identity={"key": "other", "status": "succeeded"},
        )

    assert store.load("key") is None


def test_learning_journal_exposes_live_claim_until_finish(tmp_path: Path) -> None:
    """A retained claim lock proves that the owning process is still active."""
    owner = arming_state.LearningJournalStore(lambda: tmp_path)
    observer = arming_state.LearningJournalStore(lambda: tmp_path)
    owner.ensure_pending("key", kind="approved_plan")

    assert owner.claim("key") is True
    assert observer.claim_is_active("key") is True

    owner.finish("key", succeeded=True)
    assert observer.claim_is_active("key") is False


def test_learning_journal_requires_local_claim_ownership_to_finish(tmp_path: Path) -> None:
    """One process cannot finish another process's claimed delivery."""
    owner = arming_state.LearningJournalStore(lambda: tmp_path)
    observer = arming_state.LearningJournalStore(lambda: tmp_path)
    owner.ensure_pending("key", kind="approved_plan")
    assert owner.claim("key")

    with pytest.raises(ValueError, match="owned by this process"):
        observer.finish("key", succeeded=True)

    owner.finish("key", succeeded=True)


def test_shared_claim_registry_survives_store_reconstruction(tmp_path: Path) -> None:
    """An evicted repository context does not lose its live claim owner."""
    registry = arming_state.LearningClaimRegistry()
    owner = arming_state.LearningJournalStore(lambda: tmp_path, claim_registry=registry)
    replacement = arming_state.LearningJournalStore(lambda: tmp_path, claim_registry=registry)
    owner.ensure_pending("key", kind="approved_plan")
    assert owner.claim("key")

    replacement.finish("key", succeeded=True)

    record = replacement.load("key")
    assert record is not None and record["status"] == "succeeded"


def test_disable_does_not_overwrite_a_live_claim(tmp_path: Path) -> None:
    """The no-learn path cannot replace an active delivery result."""
    owner = arming_state.LearningJournalStore(lambda: tmp_path)
    observer = arming_state.LearningJournalStore(lambda: tmp_path)
    owner.ensure_pending("key", kind="approved_plan")
    assert owner.claim("key")

    assert observer.disable("key") is None
    record = observer.load("key")
    assert record is not None and record["status"] == "claimed"
    owner.finish("key", succeeded=True)


def test_abandoned_claim_terminal_race_is_idempotent(tmp_path: Path) -> None:
    """A stale recovery read accepts a claim that became terminal."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    store.ensure_pending("key", kind="approved_plan")
    assert store.claim("key")
    store.finish("key", succeeded=True)

    record = store.fail_abandoned_claim("key", error="outcome_unknown")

    assert record is not None and record["status"] == "succeeded"


def test_terminal_learning_keeps_cleanup_recoverable(tmp_path: Path) -> None:
    """A terminal learn result remains discoverable until cleanup completes."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    identity = {
        "repo": "Hephaestus",
        "issue": 2705,
        "post_processing": {"result": {"passed": True}},
    }
    store.ensure_pending("key", kind="post_merge", identity=identity)
    assert store.claim("key")
    store.finish("key", succeeded=True)

    assert [
        record["key"] for record in store.incomplete_for_issue(repo="Hephaestus", issue=2705)
    ] == ["key"]

    store.finish_cleanup("key", succeeded=True)
    assert store.incomplete_for_issue(repo="Hephaestus", issue=2705) == []


def test_learning_journal_validates_required_identity(tmp_path: Path) -> None:
    """A syntactically valid but incomplete record is corrupt evidence."""
    store = arming_state.LearningJournalStore(lambda: tmp_path)
    store.path("key").write_text(
        json.dumps({"key": "key", "kind": "approved_plan", "status": "pending"}),
        encoding="utf-8",
    )

    with pytest.raises(arming_state.LearningJournalError, match="missing fields"):
        store.load("key")
