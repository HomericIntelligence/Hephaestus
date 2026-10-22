"""Tests for hephaestus.nats.replay (#2395).

These tests pin the idempotent replay contract: deterministic receipt
identities, compare-and-swap receipt transitions, original-subject
publication with a deterministic ``Nats-Msg-Id``, completed-receipt restart
deduplication, replayability rejection, and explicit unknown outcomes for
indeterminate publications.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Coroutine
from typing import Any

import pytest

from hephaestus.nats.delivery_ledger import StoredLedger
from hephaestus.nats.failure_models import (
    DLQ_MAX_MSG_SIZE,
    FailureEnvelope,
    sha256_hex,
)
from hephaestus.nats.failure_store import (
    ExistingStream,
    PubAckView,
    SequenceConflictError,
    StoredSourceMessage,
    failure_subject,
)
from hephaestus.nats.replay import (
    FailureReplayer,
    ReplayNotFoundError,
    ReplayNotReplayableError,
    ReplayOutcome,
    replay_message_id,
    replay_receipt_id,
    replay_receipt_subject,
)


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


class _FakeReplayAdmin:
    """In-memory JetStream admin seam with CAS publish semantics.

    Only ``publish`` and ``get_last_message`` are exercised; the other
    protocol methods satisfy the structural seam and fail loudly if reached.
    """

    def __init__(self) -> None:
        self.rows: dict[str, StoredLedger] = {}
        self.publish_log: list[tuple[str, bytes, dict[str, str] | None]] = []
        self.fail_subjects: set[str] = set()

    async def stream_info(self, name: str) -> ExistingStream | None:
        raise AssertionError("stream_info must not run in the replay harness")

    async def add_stream(self, topology: Any) -> None:
        raise AssertionError("add_stream must not run in the replay harness")

    async def update_stream(self, name: str, *, subjects: frozenset[str]) -> None:
        raise AssertionError("update_stream must not run in the replay harness")

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> PubAckView:
        if subject in self.fail_subjects:
            raise ConnectionError("publication unavailable")
        expected: int | None = None
        if headers is not None:
            raw = headers.get("Nats-Expected-Last-Subject-Sequence")
            if raw is not None:
                expected = int(raw)
        current = self.rows.get(subject)
        sequence = 0 if current is None else current.sequence
        if expected is not None and expected != sequence:
            raise SequenceConflictError(f"stale publish for {subject}")
        new_sequence = sequence + 1
        self.rows[subject] = StoredLedger(sequence=new_sequence, payload=payload)
        self.publish_log.append((subject, payload, headers))
        return PubAckView(stream="TASKS", sequence=new_sequence, duplicate=False)

    async def get_last_message(self, stream: str, subject: str) -> StoredLedger | None:
        return self.rows.get(subject)

    async def get_message(self, stream: str, sequence: int) -> StoredSourceMessage | None:
        raise AssertionError("get_message must not run in the replay harness")


def _replayable_envelope(failure_id: str, payload: bytes = b'{"hello":"world"}') -> FailureEnvelope:
    event_id = f"event-{failure_id}"
    data = {
        "failure_id": failure_id,
        "event_id": event_id,
        "failure_kind": "handler",
        "attempt_count": 3,
        "stream": "TASKS",
        "durable": "hephaestus-subscriber",
        "stream_sequence": 42,
        "subject": "hi.tasks.demo",
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "payload_length": len(payload),
        "payload_sha256": sha256_hex(payload),
        "first_failure_at": "2026-01-01T00:00:00Z",
        "last_attempt_at": "2026-01-01T00:00:00Z",
        "replayable": True,
    }
    return FailureEnvelope.from_dict(data)


def _seed_dead_letter(
    admin: _FakeReplayAdmin,
    envelope: FailureEnvelope,
) -> None:
    admin.rows[failure_subject(envelope.failure_id)] = StoredLedger(
        sequence=1,
        payload=envelope.to_bytes(),
    )


def test_replay_receipt_and_message_ids_are_deterministic() -> None:
    """Receipt and message ids are derived identically for the same inputs."""
    receipt_a = replay_receipt_id("failure-a", "request-1")
    receipt_b = replay_receipt_id("failure-a", "request-1")
    assert receipt_a == receipt_b
    assert replay_receipt_id("failure-a", "request-2") != receipt_a
    assert replay_receipt_id("failure-b", "request-1") != receipt_a
    assert replay_message_id("failure-a", receipt_a) == replay_message_id("failure-a", receipt_a)
    assert replay_receipt_subject(receipt_a).endswith(receipt_a)


def test_replay_publishes_original_payload_and_completes_the_receipt() -> None:
    """A fresh request publishes once and records a completed receipt."""
    admin = _FakeReplayAdmin()
    envelope = _replayable_envelope("failure-a")
    _seed_dead_letter(admin, envelope)
    replayer = FailureReplayer(admin)

    result = _run(replayer.replay("failure-a", "request-1"))

    assert result.outcome is ReplayOutcome.REPLAYED
    assert result.receipt_id == replay_receipt_id("failure-a", "request-1")
    assert result.puback is not None
    assert result.puback.duplicate is False

    original_publishes = [entry for entry in admin.publish_log if entry[0] == "hi.tasks.demo"]
    assert len(original_publishes) == 1
    _, payload, headers = original_publishes[0]
    assert payload == b'{"hello":"world"}'
    assert headers is not None
    assert headers.get("Nats-Msg-Id") == replay_message_id("failure-a", result.receipt_id)

    receipt_subject = replay_receipt_subject(result.receipt_id)
    receipt_raw = admin.rows[receipt_subject]
    receipt = json.loads(receipt_raw.payload.decode())
    assert receipt["state"] == "completed"
    assert receipt["pub_stream"] == result.puback.stream
    assert receipt["pub_sequence"] == result.puback.sequence


def test_completed_receipt_returns_the_original_puback_after_restart() -> None:
    """A completed receipt replays nothing and echoes its original PubAck."""
    admin = _FakeReplayAdmin()
    envelope = _replayable_envelope("failure-a")
    _seed_dead_letter(admin, envelope)

    first = _run(FailureReplayer(admin).replay("failure-a", "request-1"))
    publishes_before = len(admin.publish_log)

    # A new replayer over the same store simulates a process restart.
    second = _run(FailureReplayer(admin).replay("failure-a", "request-1"))

    assert first.outcome is ReplayOutcome.REPLAYED
    assert second.outcome is ReplayOutcome.ALREADY_REPLAYED
    assert len(admin.publish_log) == publishes_before, "no new publish after restart"
    assert second.puback is not None
    assert first.puback is not None
    assert second.puback.stream == first.puback.stream
    assert second.puback.sequence == first.puback.sequence
    assert second.puback.duplicate is True

    third = _run(FailureReplayer(admin).replay("failure-a", "request-1"))
    assert third.outcome is ReplayOutcome.ALREADY_REPLAYED
    assert len(admin.publish_log) == publishes_before


def test_a_missing_dead_letter_record_raises_not_found() -> None:
    """Replay without a dead-letter record raises ReplayNotFoundError."""
    replayer = FailureReplayer(_FakeReplayAdmin())

    with pytest.raises(ReplayNotFoundError):
        _run(replayer.replay("failure-missing", "request-1"))


def test_a_metadata_only_record_is_rejected() -> None:
    """A non-replayable record publishes nothing and raises."""
    admin = _FakeReplayAdmin()
    data = _replayable_envelope("failure-meta").to_dict()
    data["payload_b64"] = None
    data["payload_length"] = 0
    data["payload_sha256"] = None
    data["replayable"] = False
    envelope = FailureEnvelope.from_dict(data)
    _seed_dead_letter(admin, envelope)

    with pytest.raises(ReplayNotReplayableError):
        _run(FailureReplayer(admin).replay("failure-meta", "request-1"))

    assert admin.publish_log == [], "a rejected record publishes nothing"


def test_an_oversized_payload_is_rejected() -> None:
    """A payload beyond the DLQ bound publishes nothing and raises."""
    admin = _FakeReplayAdmin()
    oversized = b"x" * (DLQ_MAX_MSG_SIZE + 1)
    data = _replayable_envelope("failure-big", oversized).to_dict()
    # Bypass the envelope size bound by seeding the raw mapping directly:
    # the rejection must come from the replayer, not the envelope builder.
    admin.rows[failure_subject("failure-big")] = StoredLedger(
        sequence=1,
        payload=json.dumps(data).encode(),
    )

    with pytest.raises(ReplayNotReplayableError):
        _run(FailureReplayer(admin).replay("failure-big", "request-1"))

    assert admin.publish_log == [], "a rejected record publishes nothing"


def test_a_stuck_publishing_receipt_surfaces_unknown() -> None:
    """An indeterminate receipt blocks duplicate publication until reconciled."""
    admin = _FakeReplayAdmin()
    envelope = _replayable_envelope("failure-a")
    _seed_dead_letter(admin, envelope)

    # Leave a publishing receipt: the original publish never confirmed.
    admin.fail_subjects.add("hi.tasks.demo")
    first = _run(FailureReplayer(admin).replay("failure-a", "request-1"))
    assert first.outcome is ReplayOutcome.UNKNOWN

    publishes_before = len(admin.publish_log)

    # Even with the broker healthy again, the indeterminate state blocks a
    # duplicate publication until an operator reconciles it.
    admin.fail_subjects.clear()
    second = _run(FailureReplayer(admin).replay("failure-a", "request-1"))

    assert second.outcome is ReplayOutcome.UNKNOWN
    assert len(admin.publish_log) == publishes_before


def test_receipt_writes_transition_publishing_then_completed() -> None:
    """The receipt CAS chain records publishing before completed."""
    admin = _FakeReplayAdmin()
    envelope = _replayable_envelope("failure-a")
    _seed_dead_letter(admin, envelope)

    replayer = FailureReplayer(admin)
    first = _run(replayer.replay("failure-a", "request-1"))
    assert first.outcome is ReplayOutcome.REPLAYED

    receipt_subject = replay_receipt_subject(replay_receipt_id("failure-a", "request-1"))
    receipt_payloads = [entry for entry in admin.publish_log if entry[0] == receipt_subject]
    assert len(receipt_payloads) == 2, "publishing then completed receipt writes"
    states = [json.loads(entry[1].decode())["state"] for entry in receipt_payloads]
    assert states == ["publishing", "completed"]


@pytest.mark.parametrize(
    ("failure_id", "request_id"),
    [("", "request-1"), ("failure-a", ""), (None, "request-1"), ("failure-a", 7)],
)
def test_invalid_identifiers_raise(failure_id: Any, request_id: Any) -> None:
    """Empty or non-string identifiers are rejected before any store read."""
    replayer = FailureReplayer(_FakeReplayAdmin())

    with pytest.raises(ValueError, match="must be a non-empty string"):
        _run(replayer.replay(failure_id, request_id))
