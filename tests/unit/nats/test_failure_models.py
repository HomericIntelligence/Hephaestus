"""Tests for hephaestus.nats.failure_models (#2395).

These tests pin the durable failure-delivery data contract: snapshot capture
never raises and never invents a fallback identity, event and failure
identifiers are deterministic digests, and DLQ envelopes respect the exact
1 MiB payload / 16 KiB metadata bounds.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from hephaestus.nats.events import NATSEvent
from hephaestus.nats.failure_models import (
    DLQ_MAX_MSG_SIZE,
    MAX_FAILURE_METADATA_BYTES,
    MAX_FAILURE_PAYLOAD_BYTES,
    ConsumerBinding,
    EventIdentity,
    FailureEnvelope,
    MessageSnapshot,
    SnapshotFailure,
    failure_id_for_event,
)

_BINDING = ConsumerBinding(subject="hi.tasks.>", durable="hephaestus-subscriber")

_DEFAULT_METADATA = object()
"""Sentinel for "install a default valid metadata" in ``_fake_msg``."""


def _fake_metadata(
    *,
    stream: str | None = "TASKS",
    consumer: str | None = "hephaestus-subscriber",
    stream_seq: int | None = 42,
    num_delivered: int | None = 1,
) -> MagicMock:
    """Build a stand-in for nats-py's ``Metadata`` object."""
    metadata = MagicMock()
    metadata.stream = stream
    metadata.consumer = consumer
    metadata.sequence.stream = stream_seq
    metadata.num_delivered = num_delivered
    return metadata


def _fake_msg(
    payload: bytes,
    *,
    subject: str | None = "hi.tasks.demo",
    headers: dict[str, str] | None = None,
    metadata: MagicMock | object | None = _DEFAULT_METADATA,
) -> MagicMock:
    """Build a stand-in JetStream message carrying raw ``payload`` bytes."""
    msg = MagicMock()
    msg.data = payload
    msg.subject = subject
    msg.headers = headers
    msg.metadata = _fake_metadata() if metadata is _DEFAULT_METADATA else metadata
    return msg


def _json_payload(data: Any = None) -> bytes:
    return json.dumps({"hello": "world"} if data is None else data).encode()


class TestEventIdentity:
    """Tests for EventIdentity and the identifier digests."""

    def test_event_id_is_a_deterministic_sha256_digest(self) -> None:
        identity = EventIdentity(stream="TASKS", durable="durable-a", stream_sequence=42)
        expected = hashlib.sha256(b'["hephaestus-event-v1","TASKS","durable-a",42]')
        assert identity.event_id == expected.hexdigest()

    def test_event_id_depends_on_every_identity_field(self) -> None:
        base = EventIdentity(stream="TASKS", durable="durable-a", stream_sequence=42)
        assert (
            base.event_id
            != EventIdentity(stream="OTHER", durable="durable-a", stream_sequence=42).event_id
        )
        assert (
            base.event_id
            != EventIdentity(stream="TASKS", durable="durable-b", stream_sequence=42).event_id
        )
        assert (
            base.event_id
            != EventIdentity(stream="TASKS", durable="durable-a", stream_sequence=43).event_id
        )

    def test_failure_id_is_derived_from_the_event_id(self) -> None:
        event_id = EventIdentity(stream="TASKS", durable="durable-a", stream_sequence=42).event_id
        expected = hashlib.sha256(
            json.dumps(["hephaestus-failure-v1", event_id], separators=(",", ":")).encode()
        )
        assert failure_id_for_event(event_id) == expected.hexdigest()


class TestMessageSnapshotCapture:
    """Tests for the non-throwing MessageSnapshot.capture() contract."""

    def test_capture_returns_snapshot_for_a_valid_message(self) -> None:
        msg = _fake_msg(_json_payload(), headers={"Nats-Time-Stamp": "2026-01-01T00:00:00Z"})
        result = MessageSnapshot.capture(msg, _BINDING)

        assert isinstance(result, MessageSnapshot)
        assert result.identity == EventIdentity(
            stream="TASKS", durable="hephaestus-subscriber", stream_sequence=42
        )
        assert result.subject == "hi.tasks.demo"
        assert result.timestamp == "2026-01-01T00:00:00Z"
        assert result.delivery_count == 1
        assert result.payload == _json_payload()

    def test_capture_never_raises_for_missing_metadata(self) -> None:
        msg = _fake_msg(_json_payload(), metadata=None)
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, SnapshotFailure)

    @pytest.mark.parametrize("stream_seq", [0, -1, None])
    def test_capture_rejects_invalid_stream_sequence(self, stream_seq: int | None) -> None:
        msg = _fake_msg(_json_payload(), metadata=_fake_metadata(stream_seq=stream_seq))
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, SnapshotFailure)

    @pytest.mark.parametrize("stream", ["", None])
    def test_capture_rejects_missing_stream_name(self, stream: str | None) -> None:
        msg = _fake_msg(_json_payload(), metadata=_fake_metadata(stream=stream))
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, SnapshotFailure)

    def test_capture_rejects_a_consumer_that_does_not_match_the_binding(self) -> None:
        msg = _fake_msg(_json_payload(), metadata=_fake_metadata(consumer="other-durable"))
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, SnapshotFailure)

    @pytest.mark.parametrize("num_delivered", [0, -5, None])
    def test_capture_rejects_invalid_delivery_count(self, num_delivered: int | None) -> None:
        msg = _fake_msg(_json_payload(), metadata=_fake_metadata(num_delivered=num_delivered))
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, SnapshotFailure)

    @pytest.mark.parametrize("subject", ["", None])
    def test_capture_rejects_an_empty_subject(self, subject: str | None) -> None:
        msg = _fake_msg(_json_payload(), subject=subject)
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, SnapshotFailure)

    def test_capture_rejects_a_payload_above_the_configured_size_bound(self) -> None:
        msg = _fake_msg(b"x" * (MAX_FAILURE_PAYLOAD_BYTES + 1))
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, SnapshotFailure)

    def test_capture_accepts_a_payload_at_the_exact_size_bound(self) -> None:
        msg = _fake_msg(b"x" * MAX_FAILURE_PAYLOAD_BYTES)
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, MessageSnapshot)
        assert len(result.payload) == MAX_FAILURE_PAYLOAD_BYTES

    def test_capture_does_not_decode_the_payload(self) -> None:
        """Undecodable bytes still capture; decode belongs to ``to_event()``."""
        msg = _fake_msg(b"\xff{not-json")
        result = MessageSnapshot.capture(msg, _BINDING)
        assert isinstance(result, MessageSnapshot)

    def test_snapshot_failure_carries_no_fallback_identity(self) -> None:
        """Malformed metadata must not receive an invented event identity."""
        msg = _fake_msg(_json_payload(), metadata=None)
        result = MessageSnapshot.capture(msg, _BINDING)

        assert isinstance(result, SnapshotFailure)
        assert not hasattr(result, "identity")
        assert not hasattr(result, "event_id")
        assert not hasattr(result, "failure_id")

    def test_snapshot_failure_reason_is_bounded(self) -> None:
        msg = _fake_msg(_json_payload(), metadata=None)
        result = MessageSnapshot.capture(msg, _BINDING)

        assert isinstance(result, SnapshotFailure)
        assert 0 < len(result.reason) <= 256


class TestMessageSnapshotToEvent:
    """Tests for MessageSnapshot.to_event() inside the decode boundary."""

    def _snapshot(self, payload: bytes) -> MessageSnapshot:
        result = MessageSnapshot.capture(_fake_msg(payload), _BINDING)
        assert isinstance(result, MessageSnapshot)
        return result

    def test_to_event_decodes_a_dict_payload(self) -> None:
        snapshot = self._snapshot(_json_payload({"hello": "world"}))
        event = snapshot.to_event()
        assert event == NATSEvent(
            subject="hi.tasks.demo",
            data={"hello": "world"},
            timestamp="",
            sequence=42,
        )

    def test_to_event_rejects_non_dict_json(self) -> None:
        snapshot = self._snapshot(_json_payload([1, 2, 3]))
        with pytest.raises((TypeError, ValueError)):
            snapshot.to_event()

    def test_to_event_raises_for_invalid_utf8(self) -> None:
        snapshot = self._snapshot(b"\xff")
        with pytest.raises(UnicodeDecodeError):
            snapshot.to_event()

    def test_to_event_raises_for_invalid_json(self) -> None:
        snapshot = self._snapshot(b"{not-json")
        with pytest.raises(json.JSONDecodeError):
            snapshot.to_event()


class TestFailureEnvelope:
    """Tests for FailureEnvelope construction and serialization bounds."""

    def _snapshot(self, payload: bytes = b'{"k": "v"}') -> MessageSnapshot:
        result = MessageSnapshot.capture(_fake_msg(payload), _BINDING)
        assert isinstance(result, MessageSnapshot)
        return result

    def test_dlq_max_msg_size_matches_the_documented_bound(self) -> None:
        expected = 4 * ((MAX_FAILURE_PAYLOAD_BYTES + 2) // 3) + MAX_FAILURE_METADATA_BYTES
        assert DLQ_MAX_MSG_SIZE == 1_414_488
        assert expected == DLQ_MAX_MSG_SIZE

    def test_build_computes_derived_payload_fields(self) -> None:
        snapshot = self._snapshot()
        envelope = FailureEnvelope.build(
            snapshot,
            "decode",
            attempt_count=2,
            first_failure_at="2026-01-01T00:00:00Z",
            last_attempt_at="2026-01-01T00:00:05Z",
        )

        assert envelope.event_id == snapshot.identity.event_id
        assert envelope.failure_id == failure_id_for_event(snapshot.identity.event_id)
        assert envelope.failure_kind == "decode"
        assert envelope.attempt_count == 2
        assert envelope.payload_length == len(snapshot.payload)
        assert envelope.payload_sha256 == hashlib.sha256(snapshot.payload).hexdigest()
        assert base64.b64decode(envelope.payload_b64 or "") == snapshot.payload
        assert envelope.replayable is True

    def test_build_rejects_an_unbounded_failure_kind(self) -> None:
        with pytest.raises(ValueError, match="failure_kind"):
            FailureEnvelope.build(
                self._snapshot(),
                "explosion-details",  # type: ignore[arg-type]
                attempt_count=1,
                first_failure_at="2026-01-01T00:00:00Z",
                last_attempt_at="2026-01-01T00:00:05Z",
            )

    def test_a_one_mib_payload_envelope_fits_the_dlq_bound(self) -> None:
        snapshot = self._snapshot(b"x" * MAX_FAILURE_PAYLOAD_BYTES)
        envelope = FailureEnvelope.build(
            snapshot,
            "handler",
            attempt_count=3,
            first_failure_at="2026-01-01T00:00:00Z",
            last_attempt_at="2026-01-01T00:00:05Z",
        )
        body = envelope.to_bytes()
        assert len(body) > 0
        assert len(body) <= DLQ_MAX_MSG_SIZE

    def test_oversized_metadata_is_rejected_before_serialization(self) -> None:
        envelope = FailureEnvelope.build(
            self._snapshot(),
            "decode",
            attempt_count=1,
            first_failure_at="2026-01-01T00:00:00Z",
            last_attempt_at="2026-01-01T00:00:05Z",
        )
        oversized = FailureEnvelope(
            **{**envelope.to_dict(), "subject": "s" * (MAX_FAILURE_METADATA_BYTES + 1)}
        )
        with pytest.raises(ValueError, match="metadata"):
            oversized.to_bytes()

    def test_serialized_envelope_uses_only_the_bounded_schema_keys(self) -> None:
        envelope = FailureEnvelope.build(
            self._snapshot(),
            "handler_outcome_unknown",
            attempt_count=3,
            first_failure_at="2026-01-01T00:00:00Z",
            last_attempt_at="2026-01-01T00:00:05Z",
        )
        assert set(envelope.to_dict()) == {
            "failure_id",
            "event_id",
            "failure_kind",
            "attempt_count",
            "stream",
            "durable",
            "stream_sequence",
            "subject",
            "payload_b64",
            "payload_length",
            "payload_sha256",
            "first_failure_at",
            "last_attempt_at",
            "replayable",
        }

    def test_from_dict_round_trips_a_built_envelope(self) -> None:
        envelope = FailureEnvelope.build(
            self._snapshot(),
            "decode",
            attempt_count=2,
            first_failure_at="2026-01-01T00:00:00Z",
            last_attempt_at="2026-01-01T00:00:05Z",
        )
        assert FailureEnvelope.from_dict(json.loads(envelope.to_bytes())) == envelope

    def test_from_dict_rejects_an_unbounded_failure_kind(self) -> None:
        envelope = FailureEnvelope.build(
            self._snapshot(),
            "decode",
            attempt_count=1,
            first_failure_at="2026-01-01T00:00:00Z",
            last_attempt_at="2026-01-01T00:00:05Z",
        )
        with pytest.raises(ValueError, match="failure_kind"):
            FailureEnvelope.from_dict({**envelope.to_dict(), "failure_kind": "oops"})

    def test_metadata_only_envelope_carries_no_payload_and_is_not_replayable(self) -> None:
        envelope = FailureEnvelope.metadata_only(
            EventIdentity(stream="TASKS", durable="durable-a", stream_sequence=42),
            "decode",
            attempt_count=6,
            first_failure_at="2026-01-01T00:00:00Z",
            last_attempt_at="2026-01-01T00:00:05Z",
        )
        assert envelope.payload_b64 is None
        assert envelope.payload_length == 0
        assert envelope.payload_sha256 is None
        assert envelope.replayable is False
        assert len(envelope.to_bytes()) > 0
        assert len(envelope.to_bytes()) <= DLQ_MAX_MSG_SIZE
