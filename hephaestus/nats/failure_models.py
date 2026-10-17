"""Durable failure-delivery data models for the NATS subscriber (#2395).

This module holds the bounded data contract behind durable NATS failure
handling:

- :class:`ConsumerBinding` — one subscribed subject and its durable consumer.
- :class:`EventIdentity` — the ``(stream, durable, stream_sequence)`` identity
  of one source message and its deterministic ``event_id`` digest.
- :class:`MessageSnapshot` — a validated, non-throwing capture of one incoming
  message. ``capture()`` never raises and never invents an identity for a
  malformed message.
- :class:`SnapshotFailure` — the failure arm of :data:`SnapshotResult`.
- :class:`FailureEnvelope` — the bounded dead-letter record. It carries base64
  payload bytes, the payload length and SHA-256, a bounded failure kind, the
  attempt count, timestamps, and the source identity. It never carries
  exception text, tracebacks, raw reply subjects, credentials, or arbitrary
  message headers.

Size bounds:

- ``MAX_FAILURE_PAYLOAD_BYTES`` (1 MiB) caps one captured payload.
- ``MAX_FAILURE_METADATA_BYTES`` (16 KiB) caps the non-payload envelope bytes.
- ``DLQ_MAX_MSG_SIZE`` (1,414,488 bytes) is the exact broker-side bound: the
  base64 expansion of one 1 MiB payload plus the 16 KiB metadata allowance.

Usage::

    from hephaestus.nats.failure_models import ConsumerBinding, MessageSnapshot

    result = MessageSnapshot.capture(msg, ConsumerBinding(subject="hi.>", durable="sub"))
    if isinstance(result, SnapshotFailure):
        return  # no disposition; JetStream redelivery owns correlation
    event = result.to_event()
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from hephaestus.nats.events import NATSEvent

MAX_FAILURE_PAYLOAD_BYTES = 1_048_576
"""Maximum captured source payload size in bytes (1 MiB)."""

MAX_FAILURE_METADATA_BYTES = 16_384
"""Maximum non-payload dead-letter envelope size in bytes (16 KiB)."""

DLQ_MAX_MSG_SIZE = 4 * ((MAX_FAILURE_PAYLOAD_BYTES + 2) // 3) + MAX_FAILURE_METADATA_BYTES
"""Exact broker-side dead-letter message bound.

One 1 MiB payload expands to 1,398,104 base64 bytes; the 16 KiB metadata
allowance brings the exact bound to 1,414,488 bytes. The dead-letter stream
is provisioned with this ``max_msg_size``.
"""

FailureKind = Literal["decode", "handler", "handler_outcome_unknown"]
"""Bounded failure classification carried by a dead-letter envelope."""

FAILURE_KINDS: frozenset[str] = frozenset({"decode", "handler", "handler_outcome_unknown"})
"""Runtime allowlist mirroring :data:`FailureKind`."""

_SNAPSHOT_FAILURE_REASON_LIMIT = 256
"""Bound on the diagnostic reason text of a :class:`SnapshotFailure`."""


def canonical_json(value: Any) -> bytes:
    """Serialize *value* to deterministic canonical JSON bytes.

    The serialization uses compact separators and ASCII output so identical
    inputs hash identically across processes and restarts.
    """
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def utc_iso8601(epoch_seconds: float) -> str:
    """Format Unix epoch seconds as an ISO-8601 UTC timestamp string."""
    return (
        datetime.fromtimestamp(epoch_seconds, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class ConsumerBinding:
    """One subscribed subject bound to one durable consumer.

    Attributes:
        subject: Subscribed NATS subject (may contain wildcards).
        durable: Durable consumer name for the binding.

    """

    subject: str
    durable: str

    def __post_init__(self) -> None:
        """Validate that both binding fields are non-empty strings.

        Raises:
            ValueError: If ``subject`` or ``durable`` is empty or not a string.

        """
        if not isinstance(self.subject, str) or not self.subject:
            raise ValueError(f"subject must be a non-empty string, got {self.subject!r}")
        if not isinstance(self.durable, str) or not self.durable:
            raise ValueError(f"durable must be a non-empty string, got {self.durable!r}")


@dataclass(frozen=True)
class EventIdentity:
    """The durable identity of one source message.

    The identity is exactly ``(stream, durable, stream_sequence)``. The
    ``event_id`` digest is deterministic across restarts, so the delivery
    ledger and dead-letter store suppress duplicates after a crash.

    Attributes:
        stream: Source JetStream stream name.
        durable: Durable consumer name that delivered the message.
        stream_sequence: Stream sequence number of the source message.

    """

    stream: str
    durable: str
    stream_sequence: int

    def __post_init__(self) -> None:
        """Validate identity fields.

        Raises:
            ValueError: If ``stream`` or ``durable`` is not a non-empty
                string, or ``stream_sequence`` is not an integer >= 1.

        """
        if not isinstance(self.stream, str) or not self.stream:
            raise ValueError(f"stream must be a non-empty string, got {self.stream!r}")
        if not isinstance(self.durable, str) or not self.durable:
            raise ValueError(f"durable must be a non-empty string, got {self.durable!r}")
        if (
            isinstance(self.stream_sequence, bool)
            or not isinstance(self.stream_sequence, int)
            or self.stream_sequence < 1
        ):
            raise ValueError(
                f"stream_sequence must be an integer >= 1, got {self.stream_sequence!r}"
            )

    @property
    def event_id(self) -> str:
        """Deterministic event identifier derived from the identity fields."""
        return sha256_hex(
            canonical_json(["hephaestus-event-v1", self.stream, self.durable, self.stream_sequence])
        )


def failure_id_for_event(event_id: str) -> str:
    """Return the deterministic dead-letter identifier for one event id."""
    return sha256_hex(canonical_json(["hephaestus-failure-v1", event_id]))


@dataclass(frozen=True)
class SnapshotFailure:
    """The failure arm of :data:`SnapshotResult`.

    A ``SnapshotFailure`` deliberately carries no event identity: malformed
    metadata receives no fallback identity, no client disposition, and no
    dead-letter correlation. The durable max-delivery advisory supplies the
    authoritative ``stream_seq`` for recovery.

    Attributes:
        reason: Bounded diagnostic text (<= 256 characters). For operator
            logs only; never published to a dead-letter record.
        subject: Best-effort message subject when it is a non-empty string,
            else ``None``.

    """

    reason: str
    subject: str | None

    @classmethod
    def from_exception(cls, exc: Exception, msg: Any) -> SnapshotFailure:
        """Build a bounded failure record from a capture exception.

        Args:
            exc: The validation exception raised while capturing.
            msg: The original message, used only for best-effort subject text.

        Returns:
            A :class:`SnapshotFailure` with bounded reason text.

        """
        subject = getattr(msg, "subject", None)
        if not isinstance(subject, str) or not subject:
            subject = None
        reason = f"{type(exc).__name__}: {exc}"
        return cls(reason=reason[:_SNAPSHOT_FAILURE_REASON_LIMIT], subject=subject)


@dataclass(frozen=True)
class MessageSnapshot:
    """A validated capture of one incoming JetStream message.

    ``capture()`` validates the source metadata, subject, payload size, and
    delivery count, but does NOT decode the payload. Decoding happens in
    :meth:`to_event`, inside the subscriber's decode exception boundary.

    Attributes:
        identity: Durable source identity of the message.
        subject: Message subject.
        payload: Raw payload bytes, at most ``MAX_FAILURE_PAYLOAD_BYTES``.
        timestamp: Value of the ``Nats-Time-Stamp`` header, or ``""``.
        delivery_count: Physical delivery count (``num_delivered``).

    """

    identity: EventIdentity
    subject: str
    payload: bytes
    timestamp: str
    delivery_count: int

    @classmethod
    def capture(cls, msg: Any, binding: ConsumerBinding) -> SnapshotResult:
        """Capture *msg* without raising.

        Args:
            msg: A nats-py JetStream message (or compatible stand-in).
            binding: The consumer binding that delivered the message.

        Returns:
            A :class:`MessageSnapshot` for a valid message, else a
            :class:`SnapshotFailure`. Never raises and never returns a
            partially valid snapshot.

        """
        try:
            return cls._capture_validated(msg, binding)
        except Exception as exc:
            return SnapshotFailure.from_exception(exc, msg)

    @classmethod
    def _capture_validated(cls, msg: Any, binding: ConsumerBinding) -> MessageSnapshot:
        """Validate all snapshot fields or raise.

        Raises:
            ValueError: If the metadata, subject, payload size, or delivery
                count is missing or invalid.

        """
        metadata = getattr(msg, "metadata", None)
        if metadata is None:
            raise ValueError("message metadata is unavailable")

        stream = getattr(metadata, "stream", None)
        if not isinstance(stream, str) or not stream:
            raise ValueError(f"message metadata has no stream name, got {stream!r}")

        consumer = getattr(metadata, "consumer", None)
        if not isinstance(consumer, str) or not consumer:
            raise ValueError(f"message metadata has no consumer name, got {consumer!r}")
        if consumer != binding.durable:
            raise ValueError(
                f"message consumer {consumer!r} does not match binding durable {binding.durable!r}"
            )

        sequence = getattr(metadata, "sequence", None)
        stream_seq = getattr(sequence, "stream", None)
        if isinstance(stream_seq, bool) or not isinstance(stream_seq, int) or stream_seq < 1:
            raise ValueError(f"stream sequence must be an integer >= 1, got {stream_seq!r}")

        num_delivered = getattr(metadata, "num_delivered", None)
        if (
            isinstance(num_delivered, bool)
            or not isinstance(num_delivered, int)
            or num_delivered < 1
        ):
            raise ValueError(f"delivery count must be an integer >= 1, got {num_delivered!r}")

        subject = getattr(msg, "subject", None)
        if not isinstance(subject, str) or not subject:
            raise ValueError(f"message subject must be a non-empty string, got {subject!r}")

        payload = getattr(msg, "data", None)
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError(f"message payload must be bytes, got {type(payload).__name__}")
        payload = bytes(payload)
        if len(payload) > MAX_FAILURE_PAYLOAD_BYTES:
            raise ValueError(
                "message payload exceeds the durable failure bound "
                f"({len(payload)} > {MAX_FAILURE_PAYLOAD_BYTES} bytes)"
            )

        headers = getattr(msg, "headers", None)
        timestamp = ""
        if isinstance(headers, dict):
            raw_timestamp = headers.get("Nats-Time-Stamp", "")
            if isinstance(raw_timestamp, str):
                timestamp = raw_timestamp

        return cls(
            identity=EventIdentity(stream=stream, durable=consumer, stream_sequence=stream_seq),
            subject=subject,
            payload=payload,
            timestamp=timestamp,
            delivery_count=num_delivered,
        )

    def to_event(self) -> NATSEvent:
        """Decode the captured payload into a :class:`NATSEvent`.

        The subscriber evaluates this method inside its decode exception
        boundary.

        Returns:
            The decoded event.

        Raises:
            UnicodeDecodeError: If the payload is not valid UTF-8.
            json.JSONDecodeError: If the payload is not valid JSON.
            ValueError: If the decoded JSON is not an object.

        """
        decoded = json.loads(self.payload.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError(
                f"NATS event payload must decode to a JSON object, got {type(decoded).__name__}"
            )
        return NATSEvent(
            subject=self.subject,
            data=decoded,
            timestamp=self.timestamp,
            sequence=self.identity.stream_sequence,
        )


SnapshotResult = MessageSnapshot | SnapshotFailure
"""Discriminated result of :meth:`MessageSnapshot.capture`."""


@dataclass(frozen=True)
class FailureEnvelope:
    """One bounded dead-letter record.

    The envelope contains the base64 payload bytes, the payload length and
    SHA-256, a bounded failure kind, the attempt count, timestamps, and the
    source identity. It excludes exception text, tracebacks, raw reply
    subjects, credentials, and arbitrary message headers.

    Attributes:
        failure_id: Deterministic dead-letter identifier.
        event_id: Deterministic source event identifier.
        failure_kind: Bounded failure classification.
        attempt_count: Number of processing attempts recorded.
        stream: Source JetStream stream name.
        durable: Durable consumer name.
        stream_sequence: Source stream sequence number.
        subject: Source message subject (empty for metadata-only records).
        payload_b64: Base64-encoded source payload, or ``None`` for a
            metadata-only record.
        payload_length: Source payload length in bytes.
        payload_sha256: Hexadecimal SHA-256 of the source payload, or ``None``
            for a metadata-only record.
        first_failure_at: ISO-8601 timestamp of the first failure.
        last_attempt_at: ISO-8601 timestamp of the most recent attempt.
        replayable: Whether the record carries a payload that replay can
            republish.

    """

    failure_id: str
    event_id: str
    failure_kind: FailureKind
    attempt_count: int
    stream: str
    durable: str
    stream_sequence: int
    subject: str
    payload_b64: str | None
    payload_length: int
    payload_sha256: str | None
    first_failure_at: str
    last_attempt_at: str
    replayable: bool

    def __post_init__(self) -> None:
        """Validate the bounded envelope fields.

        Raises:
            ValueError: If the failure kind is unbounded, an identifier is
                empty, the attempt count or payload length is negative or not
                an integer, or the payload fields are inconsistent.

        """
        if self.failure_kind not in FAILURE_KINDS:
            raise ValueError(
                f"failure_kind must be one of {sorted(FAILURE_KINDS)}, got {self.failure_kind!r}"
            )
        if not isinstance(self.failure_id, str) or not self.failure_id:
            raise ValueError("failure_id must be a non-empty string")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be a non-empty string")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count < 1
        ):
            raise ValueError(f"attempt_count must be an integer >= 1, got {self.attempt_count!r}")
        for name in ("stream", "durable"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if (
            isinstance(self.stream_sequence, bool)
            or not isinstance(self.stream_sequence, int)
            or self.stream_sequence < 1
        ):
            raise ValueError(
                f"stream_sequence must be an integer >= 1, got {self.stream_sequence!r}"
            )
        if (
            isinstance(self.payload_length, bool)
            or not isinstance(self.payload_length, int)
            or self.payload_length < 0
        ):
            raise ValueError(f"payload_length must be an integer >= 0, got {self.payload_length!r}")
        if (self.payload_b64 is None) != (self.payload_sha256 is None):
            raise ValueError("payload_b64 and payload_sha256 must both be set or both be None")
        for name in ("first_failure_at", "last_attempt_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty ISO-8601 timestamp")

    @classmethod
    def build(
        cls,
        snapshot: MessageSnapshot,
        failure_kind: FailureKind,
        *,
        attempt_count: int,
        first_failure_at: str,
        last_attempt_at: str,
    ) -> FailureEnvelope:
        """Build a replayable envelope from one captured snapshot.

        Args:
            snapshot: The validated message capture.
            failure_kind: Bounded failure classification.
            attempt_count: Number of processing attempts recorded.
            first_failure_at: ISO-8601 timestamp of the first failure.
            last_attempt_at: ISO-8601 timestamp of the most recent attempt.

        Returns:
            A replayable :class:`FailureEnvelope` with derived identifiers and
            payload digest fields.

        """
        event_id = snapshot.identity.event_id
        return cls(
            failure_id=failure_id_for_event(event_id),
            event_id=event_id,
            failure_kind=failure_kind,
            attempt_count=attempt_count,
            stream=snapshot.identity.stream,
            durable=snapshot.identity.durable,
            stream_sequence=snapshot.identity.stream_sequence,
            subject=snapshot.subject,
            payload_b64=base64.b64encode(snapshot.payload).decode("ascii"),
            payload_length=len(snapshot.payload),
            payload_sha256=sha256_hex(snapshot.payload),
            first_failure_at=first_failure_at,
            last_attempt_at=last_attempt_at,
            replayable=True,
        )

    @classmethod
    def metadata_only(
        cls,
        identity: EventIdentity,
        failure_kind: FailureKind,
        *,
        attempt_count: int,
        first_failure_at: str,
        last_attempt_at: str,
    ) -> FailureEnvelope:
        """Build a durable metadata-only record for a missing source message.

        A metadata-only record keeps the failure auditable when the source
        payload could not be captured. It is not replayable.

        Args:
            identity: Durable source identity.
            failure_kind: Bounded failure classification.
            attempt_count: Number of deliveries recorded.
            first_failure_at: ISO-8601 timestamp of the first failure.
            last_attempt_at: ISO-8601 timestamp of the most recent attempt.

        Returns:
            A non-replayable :class:`FailureEnvelope` without payload fields.

        """
        return cls(
            failure_id=failure_id_for_event(identity.event_id),
            event_id=identity.event_id,
            failure_kind=failure_kind,
            attempt_count=attempt_count,
            stream=identity.stream,
            durable=identity.durable,
            stream_sequence=identity.stream_sequence,
            subject="",
            payload_b64=None,
            payload_length=0,
            payload_sha256=None,
            first_failure_at=first_failure_at,
            last_attempt_at=last_attempt_at,
            replayable=False,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the bounded serialization mapping for the envelope."""
        return {
            "failure_id": self.failure_id,
            "event_id": self.event_id,
            "failure_kind": self.failure_kind,
            "attempt_count": self.attempt_count,
            "stream": self.stream,
            "durable": self.durable,
            "stream_sequence": self.stream_sequence,
            "subject": self.subject,
            "payload_b64": self.payload_b64,
            "payload_length": self.payload_length,
            "payload_sha256": self.payload_sha256,
            "first_failure_at": self.first_failure_at,
            "last_attempt_at": self.last_attempt_at,
            "replayable": self.replayable,
        }

    def to_bytes(self) -> bytes:
        """Serialize the envelope to JSON bytes within the DLQ bounds.

        The serializer rejects metadata exceeding 16 KiB before publication
        and rejects any envelope that exceeds the broker-side DLQ bound.

        Returns:
            Canonical JSON bytes of :meth:`to_dict`.

        Raises:
            ValueError: If the non-payload metadata exceeds
                ``MAX_FAILURE_METADATA_BYTES`` or the complete envelope
                exceeds ``DLQ_MAX_MSG_SIZE``.

        """
        body = json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":")).encode()
        payload_chars = len(self.payload_b64) if isinstance(self.payload_b64, str) else 0
        metadata_bytes = len(body) - payload_chars
        if metadata_bytes > MAX_FAILURE_METADATA_BYTES:
            raise ValueError(
                "failure envelope metadata exceeds "
                f"{MAX_FAILURE_METADATA_BYTES} bytes (got {metadata_bytes})"
            )
        if len(body) > DLQ_MAX_MSG_SIZE:
            raise ValueError(
                f"failure envelope exceeds the DLQ bound of {DLQ_MAX_MSG_SIZE} bytes "
                f"(got {len(body)})"
            )
        return body

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FailureEnvelope:
        """Parse and validate an envelope mapping read from the DLQ.

        Args:
            raw: Decoded JSON mapping.

        Returns:
            The validated :class:`FailureEnvelope`.

        Raises:
            ValueError: If required keys are missing or invalid.

        """
        if not isinstance(raw, dict):
            raise ValueError(f"failure envelope must be a JSON object, got {type(raw).__name__}")
        required = {
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
        missing = required - raw.keys()
        if missing:
            raise ValueError(f"failure envelope is missing keys: {sorted(missing)}")
        subject = raw["subject"]
        if not isinstance(subject, str):
            raise ValueError(f"subject must be a string, got {type(subject).__name__}")
        payload_b64 = raw["payload_b64"]
        if payload_b64 is not None:
            if not isinstance(payload_b64, str):
                raise ValueError("payload_b64 must be a base64 string or None")
            try:
                decoded_len = len(base64.b64decode(payload_b64, validate=True))
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"payload_b64 is not valid base64: {exc}") from None
            if decoded_len != raw["payload_length"]:
                raise ValueError("payload_b64 does not match payload_length")
        return cls(
            failure_id=raw["failure_id"],
            event_id=raw["event_id"],
            failure_kind=raw["failure_kind"],
            attempt_count=raw["attempt_count"],
            stream=raw["stream"],
            durable=raw["durable"],
            stream_sequence=raw["stream_sequence"],
            subject=subject,
            payload_b64=payload_b64,
            payload_length=raw["payload_length"],
            payload_sha256=raw["payload_sha256"],
            first_failure_at=raw["first_failure_at"],
            last_attempt_at=raw["last_attempt_at"],
            replayable=bool(raw["replayable"]),
        )
