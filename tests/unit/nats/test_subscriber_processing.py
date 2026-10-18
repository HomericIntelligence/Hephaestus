"""Direct behavioral tests for :meth:`NATSSubscriberThread._process_message` (#2395).

These tests drive the durable processing pipeline without a broker loop:
the ledger transport and the JetStream admin seam are in-memory fakes, so
every disposition (``ack_sync`` / ``nak`` / ``term`` / abstain) is asserted
against one deterministic ledger and dead-letter flow.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.delivery_ledger import (
    DeliveryLedger,
    LedgerSequenceConflictError,
    LedgerStoreError,
    StoredLedger,
)
from hephaestus.nats.events import NATSEvent
from hephaestus.nats.failure_models import ConsumerBinding
from hephaestus.nats.failure_store import (
    AdvisoryReconciler,
    ExistingStream,
    FailureResources,
    FailureStore,
    PubAckView,
    SequenceConflictError,
    StoredSourceMessage,
)
from hephaestus.nats.subscriber import NATSSubscriberThread

_BINDING = ConsumerBinding(subject="hi.tasks.>", durable="hephaestus-subscriber")


class _RecordingLedgerTransport:
    """Ledger transport with CAS semantics and an ordered call log."""

    def __init__(self, calls: list[str]) -> None:
        self.rows: dict[str, StoredLedger] = {}
        self.calls = calls
        self.fail_writes = False

    async def read(self, subject: str) -> StoredLedger | None:
        return self.rows.get(subject)

    async def write(self, subject: str, payload: bytes, *, expected_sequence: int) -> int:
        if self.fail_writes:
            raise LedgerStoreError("ledger store unavailable")
        current = self.rows.get(subject)
        sequence = 0 if current is None else current.sequence
        if expected_sequence != sequence:
            raise LedgerSequenceConflictError(f"stale write for {subject}")
        new_sequence = sequence + 1
        self.rows[subject] = StoredLedger(sequence=new_sequence, payload=payload)
        state = json.loads(payload.decode())["state"]
        self.calls.append(f"write:{state}")
        return new_sequence


class _FakeDlqAdmin:
    """Minimal JetStream admin seam for the dead-letter publication path.

    Only ``publish`` is exercised in these tests; the other protocol methods
    exist to satisfy the structural seam and fail loudly if reached.
    """

    def __init__(self) -> None:
        self.rows: dict[str, StoredLedger] = {}

    async def stream_info(self, name: str) -> ExistingStream | None:
        raise AssertionError("stream_info must not run in the processing harness")

    async def add_stream(self, topology: Any) -> None:
        raise AssertionError("add_stream must not run in the processing harness")

    async def update_stream(self, name: str, *, subjects: frozenset[str]) -> None:
        raise AssertionError("update_stream must not run in the processing harness")

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> PubAckView:
        expected: int | None = None
        if headers is not None:
            raw = headers.get("Nats-Expected-Last-Subject-Sequence")
            if raw is not None:
                expected = int(raw)
        current = self.rows.get(subject)
        sequence = 0 if current is None else current.sequence
        if expected is not None and expected != sequence:
            raise SequenceConflictError(f"duplicate failure record for {subject}")
        new_sequence = sequence + 1
        self.rows[subject] = StoredLedger(sequence=new_sequence, payload=payload)
        return PubAckView(stream="HEPHAESTUS_NATS_FAILURES", sequence=new_sequence, duplicate=False)

    async def get_message(self, stream: str, sequence: int) -> StoredSourceMessage | None:
        raise AssertionError("get_message must not run in the processing harness")

    async def get_last_message(self, stream: str, subject: str) -> StoredLedger | None:
        raise AssertionError("get_last_message must not run in the processing harness")


def _make_resources(
    calls: list[str],
) -> tuple[FailureResources, _RecordingLedgerTransport, _FakeDlqAdmin]:
    """Wire the processing bundle against in-memory seams."""
    transport = _RecordingLedgerTransport(calls)
    dlq_admin = _FakeDlqAdmin()
    resources = FailureResources(
        ledger=DeliveryLedger(transport, max_attempts=3, lease_seconds=30.0),
        store=FailureStore(dlq_admin),
        advisory_reconciler=AdvisoryReconciler(dlq_admin),
    )
    return resources, transport, dlq_admin


def _make_msg(
    payload: dict[str, Any] | bytes,
    *,
    sequence: int = 1,
    num_delivered: int = 1,
) -> MagicMock:
    """Build a mock JetStream message with valid durable metadata."""
    msg = MagicMock()
    msg.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    msg.subject = "hi.tasks.demo"
    msg.headers = None
    metadata = MagicMock()
    metadata.stream = "TASKS"
    metadata.consumer = _BINDING.durable
    metadata.sequence.stream = sequence
    metadata.num_delivered = num_delivered
    msg.metadata = metadata
    msg.ack = AsyncMock()
    msg.ack_sync = AsyncMock()
    msg.nak = AsyncMock()
    msg.term = AsyncMock()
    msg.in_progress = AsyncMock()
    return msg


def _make_thread(handler: Any, **config_kwargs: Any) -> NATSSubscriberThread:
    """Build a subscriber thread for direct ``_process_message`` driving."""
    config_kwargs.setdefault("enabled", True)
    config_kwargs.setdefault("subjects", ["hi.tasks.>"])
    return NATSSubscriberThread(config=NATSConfig(**config_kwargs), handler=handler)


def _run(thread: NATSSubscriberThread, msg: MagicMock, resources: FailureResources) -> None:
    asyncio.run(thread._process_message(msg, _BINDING, resources))


def test_success_orders_ledger_handler_ack_and_acknowledged_state() -> None:
    """A success runs claim → handler → completed CAS → ack_sync → acked CAS."""
    calls: list[str] = []
    resources, transport, _ = _make_resources(calls)

    def handler(event: NATSEvent) -> None:
        calls.append("handler")
        assert event.data == {"hello": "world"}

    thread = _make_thread(handler)
    msg = _make_msg({"hello": "world"})
    msg.ack_sync = AsyncMock(side_effect=lambda: calls.append("ack:sync"))

    _run(thread, msg, resources)

    assert calls == [
        "write:running",
        "handler",
        "write:completed",
        "ack:sync",
        "write:acked",
    ]
    msg.nak.assert_not_awaited()
    msg.term.assert_not_awaited()
    assert thread.last_message_at is not None
    assert len(transport.rows) == 1


def test_transient_decode_failure_naks_then_recovers() -> None:
    """A decode failure NAKs below the budget; the redelivery succeeds."""
    calls: list[str] = []
    resources, _, _ = _make_resources(calls)
    received: list[NATSEvent] = []
    thread = _make_thread(received.append)

    first = _make_msg(b"\xff", num_delivered=1)
    _run(thread, first, resources)

    first.nak.assert_awaited_once()
    assert first.nak.await_args.kwargs["delay"] == 1.0
    first.ack_sync.assert_not_awaited()
    first.term.assert_not_awaited()
    assert received == []

    second = _make_msg({"recovered": True}, num_delivered=2)
    _run(thread, second, resources)

    second.ack_sync.assert_awaited_once()
    second.nak.assert_not_awaited()
    assert [event.data for event in received] == [{"recovered": True}]


def test_handler_failure_recovers_on_redelivery() -> None:
    """A raising handler NAKs below the budget; the redelivery completes."""
    calls: list[str] = []
    resources, _, _ = _make_resources(calls)
    attempts: list[NATSEvent] = []

    def handler(event: NATSEvent) -> None:
        attempts.append(event)
        if len(attempts) == 1:
            raise RuntimeError("transient boom")

    thread = _make_thread(handler)

    first = _make_msg({"n": 1}, num_delivered=1)
    _run(thread, first, resources)
    first.nak.assert_awaited_once()
    assert first.nak.await_args.kwargs["delay"] == 1.0

    second = _make_msg({"n": 1}, num_delivered=2)
    _run(thread, second, resources)
    second.ack_sync.assert_awaited_once()

    assert len(attempts) == 2, "the handler retries on the redelivery"
    assert calls == [
        "write:running",
        "write:ready",
        "write:running",
        "write:completed",
        "write:acked",
    ]


def test_permanent_handoff_publishes_dead_letter_and_terms() -> None:
    """At the processing budget the failure dead-letters and terminates.

    A later delivery (the broker's retry after the handoff) re-publishes
    idempotently and terminates without re-invoking the handler.
    """
    calls: list[str] = []
    resources, _, dlq_admin = _make_resources(calls)
    attempts = {"n": 0}

    def handler(_event: NATSEvent) -> None:
        attempts["n"] += 1
        raise RuntimeError("permanent boom")

    thread = _make_thread(handler)

    deliveries = [_make_msg({"n": 1}, num_delivered=n) for n in (1, 2, 3)]
    for msg in deliveries:
        _run(thread, msg, resources)

    deliveries[0].nak.assert_awaited_once()
    assert deliveries[0].nak.await_args.kwargs["delay"] == 1.0
    deliveries[1].nak.assert_awaited_once()
    assert deliveries[1].nak.await_args.kwargs["delay"] == 5.0

    deliveries[2].nak.assert_not_awaited()
    deliveries[2].term.assert_awaited_once()
    deliveries[2].ack_sync.assert_not_awaited()
    assert len(dlq_admin.rows) == 1, "exactly one dead-letter record publishes"
    envelope = json.loads(next(iter(dlq_admin.rows.values())).payload.decode())
    assert envelope["failure_kind"] == "handler"
    assert envelope["attempt_count"] == 3
    assert envelope["replayable"] is True

    # Broker redelivery after the handoff: idempotent publication + term,
    # and the handler is never invoked again.
    fourth = _make_msg({"n": 1}, num_delivered=4)
    _run(thread, fourth, resources)
    fourth.term.assert_awaited_once()
    fourth.nak.assert_not_awaited()
    assert attempts["n"] == 3
    assert len(dlq_admin.rows) == 1, "the repeated publication is idempotent"


def test_snapshot_failure_abstains_without_disposition() -> None:
    """Uncapturable metadata gets no client disposition and no records.

    The broker redelivers until its own max-delivery advisory; the advisory
    reconciler owns the durable record for this class. The subscriber must
    not ack, nak, or term a message it cannot durably identify.
    """
    calls: list[str] = []
    resources, transport, dlq_admin = _make_resources(calls)
    handler = MagicMock()
    thread = _make_thread(handler)
    msg = _make_msg({"hello": "world"})
    msg.metadata = None

    _run(thread, msg, resources)

    msg.ack.assert_not_awaited()
    msg.ack_sync.assert_not_awaited()
    msg.nak.assert_not_awaited()
    msg.term.assert_not_awaited()
    handler.assert_not_called()
    assert transport.rows == {}
    assert dlq_admin.rows == {}
    assert thread.last_error is None


def test_ledger_uncertainty_abstains_without_disposition() -> None:
    """A lost CAS claim takes no client disposition and skips the handler."""
    calls: list[str] = []
    resources, transport, dlq_admin = _make_resources(calls)
    transport.fail_writes = True
    handler = MagicMock()
    thread = _make_thread(handler)
    msg = _make_msg({"hello": "world"})

    _run(thread, msg, resources)

    msg.ack_sync.assert_not_awaited()
    msg.nak.assert_not_awaited()
    msg.term.assert_not_awaited()
    handler.assert_not_called()
    assert dlq_admin.rows == {}


def test_completed_but_failed_ack_recovers_on_redelivery() -> None:
    """An ack error after completion does not re-invoke the handler.

    The first delivery completes the ledger record but loses the ack; the
    redelivery replays the ack from the completed record and marks it acked.
    """
    calls: list[str] = []
    resources, _, _ = _make_resources(calls)
    handler = MagicMock()
    thread = _make_thread(handler)

    first = _make_msg({"hello": "world"}, num_delivered=1)
    first.ack_sync = AsyncMock(side_effect=ConnectionError("ack lost"))
    _run(thread, first, resources)

    assert calls == ["write:running", "write:completed"], "no acked state yet"
    first.nak.assert_not_awaited()
    first.term.assert_not_awaited()

    second = _make_msg({"hello": "world"}, num_delivered=2)
    _run(thread, second, resources)

    second.ack_sync.assert_awaited_once()
    second.nak.assert_not_awaited()
    second.term.assert_not_awaited()
    handler.assert_called_once()  # the handler never re-runs for a completed event
    assert calls[-1] == "write:acked"


def test_decode_of_non_object_json_is_a_decode_failure() -> None:
    """Valid JSON that is not an object is a decode failure, not a dispatch."""
    calls: list[str] = []
    resources, _, _ = _make_resources(calls)
    handler = MagicMock()
    thread = _make_thread(handler)
    msg = _make_msg(b"[1, 2, 3]")

    _run(thread, msg, resources)

    msg.nak.assert_awaited_once()
    msg.ack_sync.assert_not_awaited()
    msg.term.assert_not_awaited()
    handler.assert_not_called()


def test_handler_runs_inside_bounded_heartbeat() -> None:
    """A slow handler keeps the delivery in progress via the heartbeat.

    The heartbeat ticks on ``heartbeat_interval_seconds`` while the blocking
    handler runs, so the broker does not consider the delivery stalled.
    """
    calls: list[str] = []
    resources, _, _ = _make_resources(calls)

    def slow_handler(_event: NATSEvent) -> None:
        time.sleep(0.3)

    thread = _make_thread(
        slow_handler,
        heartbeat_interval_seconds=0.05,
        ack_wait_seconds=30.0,
        ledger_lease_seconds=30.0,
    )
    msg = _make_msg({"slow": True})

    _run(thread, msg, resources)

    assert msg.in_progress.await_count >= 2, "heartbeats tick during the slow handler"
    msg.ack_sync.assert_awaited_once()
    assert calls[-1] == "write:acked"
