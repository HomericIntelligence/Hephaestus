"""Tests for hephaestus.nats.delivery_ledger (#2395).

These tests pin the delivery-ledger contract: every ledger transition, the
retry budget, lease renewal and expiry, CAS-conflict behavior, duplicate
suppression for completed/handoff states, and abstention on store
uncertainty. Async ledger calls are driven through ``asyncio.run`` per the
repository convention for async unit tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from hephaestus.nats.delivery_ledger import (
    DeliveryAction,
    DeliveryLedger,
    LedgerRecord,
    LedgerSequenceConflictError,
    LedgerState,
    LedgerStoreError,
    StoredLedger,
    ledger_subject,
)
from hephaestus.nats.failure_models import EventIdentity

_IDENTITY = EventIdentity(stream="TASKS", durable="durable-a", stream_sequence=7)
_MAX_ATTEMPTS = 3
_LEASE_SECONDS = 30.0
_FOREIGN_OWNER = "foreign-owner"


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive one async ledger scenario to completion."""
    return asyncio.run(coro)


class FakeLedgerTransport:
    """In-memory ledger transport with injectable conflicts and failures."""

    def __init__(self) -> None:
        self.records: dict[str, StoredLedger] = {}
        self.write_calls: list[tuple[str, int]] = []
        self.fail_next_read = False
        self.fail_next_write = False
        self.conflicts_remaining = 0
        self.foreign_writer: LedgerRecord | None = None

    def install(self, subject: str, record: LedgerRecord, sequence: int = 1) -> None:
        self.records[subject] = StoredLedger(sequence=sequence, payload=record.to_bytes())

    async def read(self, subject: str) -> StoredLedger | None:
        if self.fail_next_read:
            self.fail_next_read = False
            raise LedgerStoreError("read unavailable")
        return self.records.get(subject)

    async def write(self, subject: str, payload: bytes, *, expected_sequence: int) -> int:
        self.write_calls.append((subject, expected_sequence))
        if self.fail_next_write:
            self.fail_next_write = False
            raise LedgerStoreError("write unavailable")
        current = self.records.get(subject)
        current_sequence = 0 if current is None else current.sequence
        if self.conflicts_remaining > 0:
            self.conflicts_remaining -= 1
            if self.foreign_writer is not None:
                self.install(subject, self.foreign_writer, sequence=current_sequence + 1)
            raise LedgerSequenceConflictError("foreign writer won the race")
        if current_sequence != expected_sequence:
            raise LedgerSequenceConflictError("stale expected sequence")
        new_sequence = current_sequence + 1
        self.records[subject] = StoredLedger(sequence=new_sequence, payload=payload)
        return new_sequence


def _record(
    state: LedgerState,
    *,
    attempt_count: int = 1,
    owner_token: str | None = "owner-1",
    lease_expires_at: float | None = 1030.0,
    failure_kind: str | None = None,
) -> LedgerRecord:
    return LedgerRecord(
        event_id=_IDENTITY.event_id,
        state=state,
        attempt_count=attempt_count,
        owner_token=owner_token,
        lease_expires_at=lease_expires_at,
        failure_kind=failure_kind,  # type: ignore[arg-type]
        first_attempt_at=1000.0,
        updated_at=1000.0,
    )


def _ledger(
    transport: FakeLedgerTransport,
    *,
    now: float = 1000.0,
) -> tuple[DeliveryLedger, list[float]]:
    clock_value = [now]
    return (
        DeliveryLedger(
            transport,
            max_attempts=_MAX_ATTEMPTS,
            lease_seconds=_LEASE_SECONDS,
            clock=lambda: clock_value[0],
        ),
        clock_value,
    )


def _subject() -> str:
    return ledger_subject(_IDENTITY.event_id)


def _persisted(transport: FakeLedgerTransport) -> LedgerRecord:
    return LedgerRecord.from_bytes(transport.records[_subject()].payload)


class TestLedgerRecordCodec:
    """Tests for LedgerRecord serialization."""

    def test_round_trip_preserves_every_field(self) -> None:
        record = _record(LedgerState.READY, attempt_count=2, failure_kind="decode")
        assert LedgerRecord.from_bytes(record.to_bytes()) == record

    def test_from_bytes_rejects_corrupt_payload(self) -> None:
        with pytest.raises(ValueError):
            LedgerRecord.from_bytes(b"{corrupt")

    def test_from_bytes_rejects_an_unknown_state(self) -> None:
        record = _record(LedgerState.READY)
        raw = record.to_bytes().replace(b'"ready"', b'"sideways"')
        with pytest.raises(ValueError):
            LedgerRecord.from_bytes(raw)


class TestClaim:
    """Tests for DeliveryLedger.claim transitions."""

    def test_absent_subject_starts_a_running_first_attempt(self) -> None:
        transport = FakeLedgerTransport()
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.PROCESS
        assert claim.record is not None
        assert claim.record.state is LedgerState.RUNNING
        assert claim.record.attempt_count == 1
        assert claim.record.owner_token
        assert transport.write_calls == [(_subject(), 0)]

    def test_ready_record_claims_the_next_attempt(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(
            _subject(),
            _record(LedgerState.READY, attempt_count=1, owner_token=None, lease_expires_at=None),
        )
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.PROCESS
        assert claim.record is not None
        assert claim.record.state is LedgerState.RUNNING
        assert claim.record.attempt_count == 2

    def test_running_record_with_an_active_lease_abstains(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(
            _subject(),
            _record(LedgerState.RUNNING, lease_expires_at=2600.0),
        )
        ledger, _ = _ledger(transport, now=1000.0)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.ABSTAIN
        assert transport.write_calls == []

    def test_running_record_with_an_expired_lease_hands_off_as_unknown(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(
            _subject(),
            _record(LedgerState.RUNNING, attempt_count=2, lease_expires_at=999.0),
        )
        ledger, _ = _ledger(transport, now=1000.0)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.HANDOFF
        assert claim.record is not None
        assert claim.record.state is LedgerState.HANDOFF
        assert claim.record.failure_kind == "handler_outcome_unknown"
        assert _persisted(transport).state is LedgerState.HANDOFF

    def test_completed_record_skips_the_handler_and_acks(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(
            _subject(),
            _record(LedgerState.COMPLETED, owner_token=None, lease_expires_at=None),
        )
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.ACK
        assert transport.write_calls == []

    def test_acked_record_repeats_the_idempotent_ack(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(
            _subject(),
            _record(LedgerState.ACKED, owner_token=None, lease_expires_at=None),
        )
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.ACK

    def test_handoff_record_skips_the_handler(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(
            _subject(),
            _record(LedgerState.HANDOFF, owner_token=None, lease_expires_at=None),
        )
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.HANDOFF
        assert transport.write_calls == []

    def test_terminal_record_terms_the_delivery(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(
            _subject(),
            _record(LedgerState.TERMINAL, owner_token=None, lease_expires_at=None),
        )
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.TERM

    def test_cas_conflict_is_resolved_by_a_fresh_re_read(self) -> None:
        transport = FakeLedgerTransport()
        transport.conflicts_remaining = 1
        transport.foreign_writer = _record(
            LedgerState.READY, attempt_count=1, owner_token=None, lease_expires_at=None
        )
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.PROCESS
        assert claim.record is not None
        assert claim.record.attempt_count == 2
        assert len(transport.write_calls) == 2

    def test_persistent_cas_conflicts_abstain_after_bounded_retries(self) -> None:
        transport = FakeLedgerTransport()
        transport.conflicts_remaining = 10
        transport.foreign_writer = _record(
            LedgerState.READY, attempt_count=1, owner_token=None, lease_expires_at=None
        )
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.ABSTAIN
        assert 1 < len(transport.write_calls) <= 3

    def test_store_read_uncertainty_abstains(self) -> None:
        transport = FakeLedgerTransport()
        transport.fail_next_read = True
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.ABSTAIN
        assert transport.write_calls == []

    def test_store_write_uncertainty_abstains(self) -> None:
        transport = FakeLedgerTransport()
        transport.fail_next_write = True
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.ABSTAIN

    def test_a_corrupt_durable_record_abstains(self) -> None:
        transport = FakeLedgerTransport()
        transport.records[_subject()] = StoredLedger(sequence=3, payload=b"{corrupt")
        ledger, _ = _ledger(transport)

        claim = _run(ledger.claim(_IDENTITY))

        assert claim.action is DeliveryAction.ABSTAIN


class TestRecordFailure:
    """Tests for DeliveryLedger.record_failure."""

    def test_failure_below_the_budget_transitions_back_to_ready(self) -> None:
        transport = FakeLedgerTransport()
        ledger, _ = _ledger(transport)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            return await ledger.record_failure(claim, "decode")

        outcome = _run(scenario())

        assert outcome.action is DeliveryAction.READY
        assert outcome.record is not None
        assert outcome.record.state is LedgerState.READY
        assert outcome.record.attempt_count == 1
        assert _persisted(transport).state is LedgerState.READY

    def test_failure_at_the_budget_transitions_to_handoff(self) -> None:
        transport = FakeLedgerTransport()
        ledger, _ = _ledger(transport)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            for _ in range(_MAX_ATTEMPTS - 1):
                outcome = await ledger.record_failure(claim, "handler")
                assert outcome.action is DeliveryAction.READY
                claim = await ledger.claim(_IDENTITY)
            return await ledger.record_failure(claim, "handler")

        outcome = _run(scenario())

        assert outcome.action is DeliveryAction.HANDOFF
        assert outcome.record is not None
        assert outcome.record.state is LedgerState.HANDOFF
        assert outcome.record.failure_kind == "handler"

    def test_failure_without_ownership_abstains(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(_subject(), _record(LedgerState.RUNNING, owner_token=_FOREIGN_OWNER))
        ledger, _ = _ledger(transport)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            assert claim.action is DeliveryAction.ABSTAIN
            return await ledger.record_failure(claim, "decode")

        outcome = _run(scenario())

        assert outcome.action is DeliveryAction.ABSTAIN
        assert _persisted(transport).state is LedgerState.RUNNING


class TestRecordCompletedAndAcked:
    """Tests for the completion and acknowledgement transitions."""

    def test_record_completed_returns_ack_and_persists_completed(self) -> None:
        transport = FakeLedgerTransport()
        ledger, _ = _ledger(transport)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            return await ledger.record_completed(claim)

        completion = _run(scenario())

        assert completion.action is DeliveryAction.ACK
        assert completion.record is not None
        assert completion.record.state is LedgerState.COMPLETED

    def test_record_completed_after_ownership_loss_abstains(self) -> None:
        transport = FakeLedgerTransport()
        ledger, clock = _ledger(transport, now=1000.0)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            assert claim.record is not None and claim.record.lease_expires_at is not None
            # The lease expires and a competing claim hands the record off.
            clock[0] = claim.record.lease_expires_at + 1.0
            rival = await ledger.claim(_IDENTITY)
            assert rival.action is DeliveryAction.HANDOFF
            return claim, await ledger.record_completed(claim)

        _, completion = _run(scenario())

        assert completion.action is DeliveryAction.ABSTAIN

    def test_record_acked_marks_the_record_acked(self) -> None:
        transport = FakeLedgerTransport()
        ledger, _ = _ledger(transport)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            completion = await ledger.record_completed(claim)
            return await ledger.record_acked(completion)

        assert _run(scenario()) is True
        assert _persisted(transport).state is LedgerState.ACKED

    def test_record_acked_fails_closed_on_state_mismatch(self) -> None:
        transport = FakeLedgerTransport()
        transport.install(_subject(), _record(LedgerState.RUNNING))
        ledger, _ = _ledger(transport)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            assert claim.action is DeliveryAction.ABSTAIN
            return await ledger.record_acked(claim)

        assert _run(scenario()) is False


class TestRecordTerminalAndLease:
    """Tests for terminal marking and lease renewal."""

    def test_record_terminal_marks_handoff_terminal(self) -> None:
        transport = FakeLedgerTransport()
        ledger, _ = _ledger(transport)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            for _ in range(_MAX_ATTEMPTS - 1):
                await ledger.record_failure(claim, "handler")
                claim = await ledger.claim(_IDENTITY)
            handoff = await ledger.record_failure(claim, "handler")
            assert handoff.action is DeliveryAction.HANDOFF
            return await ledger.record_terminal(handoff)

        assert _run(scenario()) is True
        assert _persisted(transport).state is LedgerState.TERMINAL

    def test_renew_lease_extends_the_expiry(self) -> None:
        transport = FakeLedgerTransport()
        ledger, clock = _ledger(transport, now=1000.0)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            clock[0] = 1010.0
            return await ledger.renew_lease(claim)

        assert _run(scenario()) is True
        assert _persisted(transport).lease_expires_at == 1010.0 + _LEASE_SECONDS

    def test_renew_lease_returns_false_after_ownership_loss(self) -> None:
        transport = FakeLedgerTransport()
        ledger, clock = _ledger(transport, now=1000.0)

        async def scenario() -> Any:
            claim = await ledger.claim(_IDENTITY)
            assert claim.record is not None and claim.record.lease_expires_at is not None
            clock[0] = claim.record.lease_expires_at + 1.0
            rival = await ledger.claim(_IDENTITY)
            assert rival.action is DeliveryAction.HANDOFF
            return await ledger.renew_lease(claim)

        assert _run(scenario()) is False
        assert _persisted(transport).state is LedgerState.HANDOFF
