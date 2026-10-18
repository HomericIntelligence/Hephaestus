"""Behavioral tests for the durable protected-merge state store."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.protected_merge_state import (
    MAX_REBASE_CYCLES,
    STATE_NAME,
    MergeAttemptKey,
    ProtectedMergeConflictError,
    ProtectedMergeRepositoryError,
    ProtectedMergeStateStore,
    ProtectedMergeUncertainError,
    ProtectedMergeValidationError,
    ProtectedRebaseCyclesExhausted,
)

BASE = "a" * 40
HEAD = "b" * 40
HEAD2 = "c" * 40
BRIDGE = "d" * 40
REMOTE = "e" * 40


def _key(
    repository: str = "acme/hephaestus",
    pr_number: int = 17,
    *,
    head: str = HEAD,
    base: str = BASE,
    generation: int = 0,
    mergeable: str = "MERGEABLE",
    status: str = "CLEAN",
) -> MergeAttemptKey:
    return MergeAttemptKey(
        repository=repository,
        pr_number=pr_number,
        reviewed_head_sha=head,
        base_sha=base,
        proof_generation=generation,
        mergeable=mergeable,
        readiness_status=status,
    )


def _store(tmp_path: Path, repository: str = "acme/hephaestus") -> ProtectedMergeStateStore:
    return ProtectedMergeStateStore(tmp_path, repository)


def test_state_path_is_repository_confined(tmp_path: Path) -> None:
    """The state directory stays beneath the repository build directory."""
    store = _store(tmp_path)
    assert store.state_dir == tmp_path / DEFAULT_STATE_DIR
    assert store.state_path.name == STATE_NAME


def test_repository_root_must_be_a_real_directory(tmp_path: Path) -> None:
    """A missing or symlinked repository root fails closed."""
    missing = tmp_path / "absent"
    with pytest.raises(ProtectedMergeRepositoryError):
        ProtectedMergeStateStore(missing, "acme/hephaestus")
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ProtectedMergeRepositoryError):
        ProtectedMergeStateStore(linked, "acme/hephaestus")


def test_symlinked_state_file_is_rejected(tmp_path: Path) -> None:
    """A symlinked durable state path fails closed on the first read."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    target = tmp_path / "outside.json"
    target.write_text("{}\n", encoding="utf-8")
    store.state_path.unlink()
    store.state_path.symlink_to(target)
    with pytest.raises(ProtectedMergeRepositoryError):
        store.load()


def test_non_regular_state_file_is_rejected(tmp_path: Path) -> None:
    """A state path that is not a regular file fails closed."""
    store = _store(tmp_path)
    store.state_dir.mkdir(parents=True)
    store.state_path.mkdir()
    with pytest.raises(ProtectedMergeRepositoryError):
        store.load()


def test_broad_state_permissions_are_rejected(tmp_path: Path) -> None:
    """A state file readable by the group or other users is rejected."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    os.chmod(store.state_path, 0o640)
    with pytest.raises(ProtectedMergeValidationError):
        store.load()


def test_malformed_state_payload_is_rejected(tmp_path: Path) -> None:
    """A state file with an unknown schema or missing fields fails strict load."""
    store = _store(tmp_path)
    store.state_dir.mkdir(parents=True)
    store.state_path.write_text('{"schema": "other"}\n', encoding="utf-8")
    os.chmod(store.state_path, 0o600)
    with pytest.raises(ProtectedMergeValidationError):
        store.load()


def test_repository_binding_mismatch_is_rejected(tmp_path: Path) -> None:
    """State recorded for one repository cannot be claimed by another."""
    first = _store(tmp_path, "acme/hephaestus")
    first.claim_rebase_cycle(17, HEAD, BASE)
    second = _store(tmp_path, "acme/other")
    with pytest.raises(ProtectedMergeValidationError):
        second.load()


def test_nonblocking_cross_process_lock_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable exclusive file lock maps to a fail-closed conflict."""
    from hephaestus.utils.file_lock import LockUnavailableError

    def unavailable(*args: object, **kwargs: object) -> object:
        raise LockUnavailableError("held by another process")

    monkeypatch.setattr("hephaestus.automation.protected_merge_state.file_lock", unavailable)
    with pytest.raises(ProtectedMergeConflictError):
        _store(tmp_path).claim_rebase_cycle(17, HEAD, BASE)


def test_same_process_keyed_lock_serializes_threads(tmp_path: Path) -> None:
    """Concurrent threads on one store path claim disjoint durable cycles."""
    store = _store(tmp_path)
    results: list[int] = []
    errors: list[Exception] = []

    def claim(index: int) -> None:
        try:
            record = store.claim_rebase_cycle(17, f"{index:040d}", BASE)
            results.append(record.index)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert sorted(results) == list(range(6))
    reopened = _store(tmp_path)
    state = reopened.load()
    assert state is not None
    assert len(state.rebase_cycles[0][1]) == 6


def test_compare_and_swap_rejects_stale_mutation(tmp_path: Path) -> None:
    """A claim response recorded after an external change fails closed."""
    first = _store(tmp_path)
    second = _store(tmp_path)
    key = _key()
    grant = first.claim_merge_attempt(key)
    assert grant.granted
    second.mark_claim_ambiguous(key)
    with pytest.raises(ProtectedMergeConflictError):
        first.record_claim_response(key, 200, "merged")
    with pytest.raises(ProtectedMergeConflictError):
        first.mark_cycle_prepared(17, 0, BRIDGE)


def test_two_store_instances_grant_exactly_one_merge_claim(tmp_path: Path) -> None:
    """Concurrent claimants across store instances yield one merge request."""
    key = _key()
    winner = _store(tmp_path).claim_merge_attempt(key)
    loser = _store(tmp_path).claim_merge_attempt(key)
    assert winner.granted and not winner.reconcile_only
    assert winner.record.state == "requested"
    assert not loser.granted and loser.reconcile_only
    assert loser.record.key == key


def test_ambiguous_claim_is_permanently_non_repeatable(tmp_path: Path) -> None:
    """A lost or malformed merge response can never be granted again."""
    store = _store(tmp_path)
    key = _key()
    store.claim_merge_attempt(key)
    record = store.mark_claim_ambiguous(key)
    assert record.ambiguous and record.state == "ambiguous"
    reopened = _store(tmp_path)
    later = reopened.claim_merge_attempt(key)
    assert not later.granted and later.reconcile_only
    assert later.record.ambiguous
    with pytest.raises(ProtectedMergeConflictError):
        reopened.record_claim_response(key, 200, "merged")
    changed_fingerprint = reopened.claim_merge_attempt(_key(status="BEHIND"))
    assert changed_fingerprint.granted


@pytest.mark.parametrize("status", [200, 409, 405])
def test_conclusive_claim_response_is_recorded(tmp_path: Path, status: int) -> None:
    """A 200, 409, or 405 merge response is recorded as conclusive."""
    store = _store(tmp_path)
    key = _key()
    store.claim_merge_attempt(key)
    record = store.record_claim_response(key, status, f"http {status}")
    assert record.state == "conclusive"
    assert record.http_status == status
    assert record.classification == f"http {status}"
    later = _store(tmp_path).claim_merge_attempt(key)
    assert not later.granted and later.reconcile_only
    assert not later.record.ambiguous


def test_nonconclusive_claim_response_is_rejected(tmp_path: Path) -> None:
    """Only a 200, 409, or 405 response may be recorded as conclusive."""
    store = _store(tmp_path)
    key = _key()
    store.claim_merge_attempt(key)
    with pytest.raises(ProtectedMergeValidationError):
        store.record_claim_response(key, 500, "server error")


def test_rebase_cycles_deduplicate_a_repeated_observation(tmp_path: Path) -> None:
    """The same head and base observation reuses the durable cycle record."""
    store = _store(tmp_path)
    first = store.claim_rebase_cycle(17, HEAD, BASE)
    second = store.claim_rebase_cycle(17, HEAD, BASE)
    assert first == second
    state = _store(tmp_path).load()
    assert state is not None and len(state.rebase_cycles[0][1]) == 1


def test_eight_cycles_persist_and_the_ninth_is_rejected(tmp_path: Path) -> None:
    """The durable store, not an in-memory counter, enforces the cycle bound."""
    store = _store(tmp_path)
    for index in range(MAX_REBASE_CYCLES):
        record = store.claim_rebase_cycle(17, f"{index + 1:040d}", BASE)
        assert record.index == index
    reopened = _store(tmp_path)
    with pytest.raises(ProtectedRebaseCyclesExhausted):
        reopened.claim_rebase_cycle(17, "f" * 40, BASE)
    state = _store(tmp_path).load()
    assert state is not None
    assert len(state.rebase_cycles[0][1]) == MAX_REBASE_CYCLES


def test_cycle_transition_order_is_enforced(tmp_path: Path) -> None:
    """A bridge is prepared, then published, and no step may be skipped."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    with pytest.raises(ProtectedMergeConflictError):
        store.mark_cycle_published(17, 0, REMOTE)
    prepared = store.mark_cycle_prepared(17, 0, BRIDGE)
    assert prepared.state == "prepared" and prepared.bridge_sha == BRIDGE
    with pytest.raises(ProtectedMergeConflictError):
        store.mark_cycle_prepared(17, 0, BRIDGE)
    published = store.mark_cycle_published(17, 0, BRIDGE)
    assert published.state == "published" and published.published_head_sha == BRIDGE
    with pytest.raises(ProtectedMergeConflictError):
        store.mark_cycle_failed(17, 0, "late failure")


def test_failed_cycle_records_terminal_reason(tmp_path: Path) -> None:
    """A conflict records a terminal failure reason on the cycle."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    failed = store.mark_cycle_failed(17, 0, "rebase_conflict")
    assert failed.state == "failed" and failed.failure_reason == "rebase_conflict"
    state = _store(tmp_path).load()
    assert state is not None
    assert state.rebase_cycles[0][1][0].state == "failed"


def test_reconcile_on_open_rejects_an_unprepared_requested_cycle(tmp_path: Path) -> None:
    """A durable requested cycle without a prepared bridge is uncertain."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    with pytest.raises(ProtectedMergeUncertainError):
        _store(tmp_path).reconcile_on_open(lambda pr_number: HEAD)


def test_reconcile_on_open_confirms_a_published_cycle(tmp_path: Path) -> None:
    """A published cycle must still equal the remote head."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    store.mark_cycle_prepared(17, 0, BRIDGE)
    store.mark_cycle_published(17, 0, BRIDGE)
    verified = _store(tmp_path).reconcile_on_open(lambda pr_number: BRIDGE)
    assert len(verified) == 1 and verified[0].state == "published"
    with pytest.raises(ProtectedMergeUncertainError):
        _store(tmp_path).reconcile_on_open(lambda pr_number: REMOTE)


def test_reconcile_on_open_handles_a_prepared_cycle_without_mutation(tmp_path: Path) -> None:
    """A prepared cycle confirms the remote or fails closed; it never publishes."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    store.mark_cycle_prepared(17, 0, BRIDGE)

    # The push never happened: the remote still holds the requested head.
    verified = _store(tmp_path).reconcile_on_open(lambda pr_number: HEAD)
    assert len(verified) == 1 and verified[0].state == "prepared"
    state = _store(tmp_path).load()
    assert state is not None and state.rebase_cycles[0][1][0].state == "prepared"

    # The push landed but the publication write crashed: equality upgrades.
    upgraded = _store(tmp_path).reconcile_on_open(lambda pr_number: BRIDGE)
    assert len(upgraded) == 1 and upgraded[0].state == "published"

    # Any other remote state fails closed.
    store.claim_rebase_cycle(17, HEAD2, BASE)
    store.mark_cycle_prepared(17, 1, REMOTE)
    with pytest.raises(ProtectedMergeUncertainError):
        _store(tmp_path).reconcile_on_open(lambda pr_number: "1" * 40)


def test_restart_persists_claims_and_cycles(tmp_path: Path) -> None:
    """New store instances observe claims and cycles after a restart."""
    store = _store(tmp_path)
    key = _key()
    store.claim_merge_attempt(key)
    store.claim_rebase_cycle(17, HEAD, BASE)
    state = _store(tmp_path).load()
    assert state is not None
    assert state.generation == 2
    assert [record.key for record in state.merge_attempts] == [key]
    assert len(state.rebase_cycles[0][1]) == 1
