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
LOGICAL_TIP = "f" * 40


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
        first.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)


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
    prepared = store.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)
    assert prepared.state == "prepared" and prepared.bridge_sha == BRIDGE
    assert prepared.logical_tip_sha == LOGICAL_TIP
    with pytest.raises(ProtectedMergeConflictError):
        store.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)
    published = store.mark_cycle_published(17, 0, BRIDGE)
    assert published.state == "published" and published.published_head_sha == BRIDGE
    assert published.logical_tip_sha == LOGICAL_TIP
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


def _validator(bridge_sha: str) -> tuple[int, int, str, str, str]:
    """Return matching metadata for test constants."""
    del bridge_sha
    return (17, 1, BASE, HEAD, LOGICAL_TIP)


def test_reconcile_on_open_rejects_an_unprepared_requested_cycle(tmp_path: Path) -> None:
    """A durable requested cycle without a prepared bridge is uncertain."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    with pytest.raises(ProtectedMergeUncertainError):
        _store(tmp_path).reconcile_on_open(lambda pr_number: HEAD, _validator)


def test_reconcile_on_open_confirms_a_published_cycle(tmp_path: Path) -> None:
    """A published cycle must still equal the remote head."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    store.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)
    store.mark_cycle_published(17, 0, BRIDGE)
    verified = _store(tmp_path).reconcile_on_open(lambda pr_number: BRIDGE, _validator)
    assert len(verified) == 1 and verified[0].state == "published"
    with pytest.raises(ProtectedMergeUncertainError):
        _store(tmp_path).reconcile_on_open(lambda pr_number: REMOTE, _validator)


def test_reconcile_on_open_handles_a_prepared_cycle_without_mutation(tmp_path: Path) -> None:
    """A prepared cycle confirms the remote or fails closed; it never publishes."""
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    store.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)

    # The push never happened: the remote still holds the requested head.
    verified = _store(tmp_path).reconcile_on_open(lambda pr_number: HEAD, _validator)
    assert len(verified) == 1 and verified[0].state == "prepared"
    state = _store(tmp_path).load()
    assert state is not None and state.rebase_cycles[0][1][0].state == "prepared"

    # The push landed but the publication write crashed: equality upgrades.
    upgraded = _store(tmp_path).reconcile_on_open(lambda pr_number: BRIDGE, _validator)
    assert len(upgraded) == 1 and upgraded[0].state == "published"

    # Any other remote state fails closed.
    store.claim_rebase_cycle(17, HEAD2, BASE)
    store.mark_cycle_prepared(17, 1, REMOTE, LOGICAL_TIP)
    with pytest.raises(ProtectedMergeUncertainError):
        _store(tmp_path).reconcile_on_open(lambda pr_number: "1" * 40, _validator)


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


def test_reconcile_on_open_validates_bridge_metadata(tmp_path: Path) -> None:
    """A prepared cycle upgrades when remote holds the bridge and metadata matches."""
    # Test 1: Valid metadata upgrades the cycle to published
    store = _store(tmp_path)
    store.claim_rebase_cycle(17, HEAD, BASE)
    store.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)

    def validator(bridge_sha: str) -> tuple[int, int, str, str, str]:
        # Returns (pr_number, cycle, base_sha, previous_head_sha, logical_tip_sha)
        assert bridge_sha == BRIDGE
        return (17, 1, BASE, HEAD, LOGICAL_TIP)

    # Remote holds the bridge; validator returns matching metadata for all
    # five fields (pr_number, cycle, base, previous_head, logical_tip).
    upgraded = store.reconcile_on_open(lambda pr_number: BRIDGE, validator)
    assert len(upgraded) == 1
    assert upgraded[0].state == "published"
    assert upgraded[0].published_head_sha == BRIDGE

    # Test 2: A mismatched cycle number fails closed.
    path2 = tmp_path / "repo2"
    path2.mkdir()
    store2 = _store(path2)
    store2.claim_rebase_cycle(17, HEAD, BASE)
    store2.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)

    def bad_cycle_validator(bridge_sha: str) -> tuple[int, int, str, str, str]:
        return (17, 2, BASE, HEAD, LOGICAL_TIP)

    with pytest.raises(ProtectedMergeUncertainError):
        store2.reconcile_on_open(lambda pr_number: BRIDGE, bad_cycle_validator)

    # Test 3: A mismatched base fails closed.
    path3 = tmp_path / "repo3"
    path3.mkdir()
    store3 = _store(path3)
    store3.claim_rebase_cycle(17, HEAD, BASE)
    store3.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)

    def bad_base_validator(bridge_sha: str) -> tuple[int, int, str, str, str]:
        return (17, 1, "x" * 40, HEAD, LOGICAL_TIP)

    with pytest.raises(ProtectedMergeUncertainError):
        store3.reconcile_on_open(lambda pr_number: BRIDGE, bad_base_validator)

    # Test 4: A mismatched previous head fails closed.
    path4 = tmp_path / "repo4"
    path4.mkdir()
    store4 = _store(path4)
    store4.claim_rebase_cycle(17, HEAD, BASE)
    store4.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)

    def bad_prev_validator(bridge_sha: str) -> tuple[int, int, str, str, str]:
        return (17, 1, BASE, "y" * 40, LOGICAL_TIP)

    with pytest.raises(ProtectedMergeUncertainError):
        store4.reconcile_on_open(lambda pr_number: BRIDGE, bad_prev_validator)

    # Test 5: A mismatched PR number fails closed.
    path5 = tmp_path / "repo5"
    path5.mkdir()
    store5 = _store(path5)
    store5.claim_rebase_cycle(17, HEAD, BASE)
    store5.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)

    def bad_pr_validator(bridge_sha: str) -> tuple[int, int, str, str, str]:
        return (99, 1, BASE, HEAD, LOGICAL_TIP)

    with pytest.raises(ProtectedMergeUncertainError):
        store5.reconcile_on_open(lambda pr_number: BRIDGE, bad_pr_validator)

    # Test 6: A mismatched logical tip fails closed.
    path6 = tmp_path / "repo6"
    path6.mkdir()
    store6 = _store(path6)
    store6.claim_rebase_cycle(17, HEAD, BASE)
    store6.mark_cycle_prepared(17, 0, BRIDGE, LOGICAL_TIP)

    def bad_tip_validator(bridge_sha: str) -> tuple[int, int, str, str, str]:
        return (17, 1, BASE, HEAD, "x" * 40)

    with pytest.raises(ProtectedMergeUncertainError):
        store6.reconcile_on_open(lambda pr_number: BRIDGE, bad_tip_validator)
