"""Durable failure resources for the NATS subscriber (#2395).

This module provisions and opens the four exact JetStream topologies behind
durable failure delivery, publishes idempotent dead-letter records, parses
max-delivery advisories, and reconciles advisories into durable dead-letter
records.

The four managed streams are:

- ``HEPHAESTUS_NATS_FAILURES`` — one dead-letter record per failure id.
- ``HEPHAESTUS_NATS_DELIVERY_LEDGER`` — one current ledger record per event.
- ``HEPHAESTUS_NATS_ADVISORIES`` — retained max-delivery advisories.
- ``HEPHAESTUS_NATS_REPLAY_RECEIPTS`` — one replay receipt per receipt id.

Managed streams carry ``metadata["hephaestus.role"]``. Provisioning only
adds missing advisory subjects or raises on undersized limits; it never
removes foreign subjects, lowers retention or capacity, or mutates a stream
without the managed role.

Usage::

    resources = await FailureResources.prepare(admin, config, bindings)
    outcome = await resources.store.publish_failure(envelope)
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, NamedTuple, Protocol

from hephaestus.nats.delivery_ledger import (
    DeliveryLedger,
    LedgerSequenceConflictError,
    LedgerStoreError,
    StoredLedger,
)
from hephaestus.nats.failure_models import (
    DLQ_MAX_MSG_SIZE,
    MAX_FAILURE_PAYLOAD_BYTES,
    EventIdentity,
    FailureEnvelope,
    MessageSnapshot,
    utc_iso8601,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exact topology constants
# ---------------------------------------------------------------------------

FAILURE_STREAM = "HEPHAESTUS_NATS_FAILURES"
LEDGER_STREAM = "HEPHAESTUS_NATS_DELIVERY_LEDGER"
ADVISORY_STREAM = "HEPHAESTUS_NATS_ADVISORIES"
REPLAY_RECEIPTS_STREAM = "HEPHAESTUS_NATS_REPLAY_RECEIPTS"

FAILURE_SUBJECT_PREFIX = "hephaestus.nats.failure"
REPLAY_RECEIPT_SUBJECT_PREFIX = "hephaestus.nats.replay.receipt"
ADVISORY_SUBJECT_PREFIX = "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES"

MANAGED_ROLE_KEY = "hephaestus.role"
RETENTION_SECONDS_30_DAYS = 2_592_000
_SMALL_MESSAGE_BYTES = 16_384
_GIB = 1_073_741_824
_MIB = 1_048_576

EXPECTED_LAST_SUBJECT_SEQUENCE_HEADER = "Nats-Expected-Last-Subject-Sequence"
_MSG_ID_HEADER = "Nats-Msg-Id"


class SequenceConflictError(Exception):
    """An admin-seam publication lost an expected-subject-sequence race."""


class FailureResourceError(Exception):
    """Base error for durable failure-resource problems."""


class FailureResourceMissingError(FailureResourceError):
    """A required stream is absent and read-only mode cannot create it."""


class ForeignStreamError(FailureResourceError):
    """An existing stream is not managed by this module; never mutate it."""


class FailureResourceUndersizedError(FailureResourceError):
    """An existing managed stream has limits below the required topology."""


@dataclass(frozen=True)
class StreamTopology:
    """One exact file-backed JetStream stream definition.

    Attributes:
        name: Stream name.
        subjects: Managed subjects.
        retention: Retention policy token (``limits``).
        discard: Discard policy token (``old``).
        storage: Storage token (``file``).
        max_age_seconds: Retention horizon in seconds.
        max_bytes: Storage capacity bound.
        max_msgs_per_subject: Per-subject message cap (-1 means unlimited).
        max_msg_size: Maximum message size in bytes.
        role: Managed role marker stored in stream metadata.

    """

    name: str
    subjects: frozenset[str]
    retention: str
    discard: str
    storage: str
    max_age_seconds: int
    max_bytes: int
    max_msgs_per_subject: int
    max_msg_size: int
    role: str


def failure_stream_topology() -> StreamTopology:
    """Dead-letter stream: one record per failure id, exact DLQ size bound."""
    return StreamTopology(
        name=FAILURE_STREAM,
        subjects=frozenset({f"{FAILURE_SUBJECT_PREFIX}.>"}),
        retention="limits",
        discard="old",
        storage="file",
        max_age_seconds=RETENTION_SECONDS_30_DAYS,
        max_bytes=_GIB,
        max_msgs_per_subject=1,
        max_msg_size=DLQ_MAX_MSG_SIZE,
        role="failure-dead-letter",
    )


def ledger_stream_topology() -> StreamTopology:
    """Delivery-ledger stream: one current state per event id."""
    return StreamTopology(
        name=LEDGER_STREAM,
        subjects=frozenset({"hephaestus.nats.ledger.>"}),
        retention="limits",
        discard="old",
        storage="file",
        max_age_seconds=RETENTION_SECONDS_30_DAYS,
        max_bytes=256 * _MIB,
        max_msgs_per_subject=1,
        max_msg_size=_SMALL_MESSAGE_BYTES,
        role="delivery-ledger",
    )


def advisory_stream_topology(
    bindings: Any,
    source_stream: str,
) -> StreamTopology:
    """Advisory stream retaining max-delivery advisories for every binding."""
    subjects = frozenset(
        f"{ADVISORY_SUBJECT_PREFIX}.{source_stream}.{binding.durable}" for binding in bindings
    )
    return StreamTopology(
        name=ADVISORY_STREAM,
        subjects=subjects,
        retention="limits",
        discard="old",
        storage="file",
        max_age_seconds=RETENTION_SECONDS_30_DAYS,
        max_bytes=256 * _MIB,
        max_msgs_per_subject=-1,
        max_msg_size=_SMALL_MESSAGE_BYTES,
        role="max-delivery-advisories",
    )


def replay_receipts_stream_topology() -> StreamTopology:
    """Replay-receipt stream: one receipt per receipt id."""
    return StreamTopology(
        name=REPLAY_RECEIPTS_STREAM,
        subjects=frozenset({f"{REPLAY_RECEIPT_SUBJECT_PREFIX}.>"}),
        retention="limits",
        discard="old",
        storage="file",
        max_age_seconds=RETENTION_SECONDS_30_DAYS,
        max_bytes=64 * _MIB,
        max_msgs_per_subject=1,
        max_msg_size=_SMALL_MESSAGE_BYTES,
        role="replay-receipts",
    )


# ---------------------------------------------------------------------------
# Admin seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingStream:
    """The live limits and metadata of one existing stream."""

    subjects: frozenset[str]
    retention: str
    discard: str
    storage: str
    max_age_seconds: int
    max_bytes: int
    max_msgs_per_subject: int
    max_msg_size: int
    metadata: dict[str, str]


class PubAckView(NamedTuple):
    """Bounded view of a JetStream publish acknowledgement."""

    stream: str
    sequence: int
    duplicate: bool


class StoredSourceMessage(NamedTuple):
    """One raw source-stream message fetched by sequence."""

    subject: str
    data: bytes
    headers: dict[str, str] | None
    sequence: int


class JetStreamAdmin(Protocol):
    """Async admin seam over JetStream for hermetic testing."""

    async def stream_info(self, name: str) -> ExistingStream | None:
        """Return the live stream view, or ``None`` when absent."""
        pass  # pragma: no cover - protocol declaration

    async def add_stream(self, topology: StreamTopology) -> None:
        """Create one stream from an exact topology."""
        pass  # pragma: no cover - protocol declaration

    async def update_stream(self, name: str, *, subjects: frozenset[str]) -> None:
        """Replace only the subjects of one managed stream."""
        pass  # pragma: no cover - protocol declaration

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> PubAckView:
        """Publish one message.

        Raises:
            SequenceConflictError: The expected subject sequence is stale.

        """
        pass  # pragma: no cover - protocol declaration

    async def get_message(self, stream: str, sequence: int) -> StoredSourceMessage | None:
        """Fetch one source message by stream sequence, or ``None``."""
        pass  # pragma: no cover - protocol declaration

    async def get_last_message(self, stream: str, subject: str) -> StoredLedger | None:
        """Fetch the last message for one subject, or ``None``."""
        pass  # pragma: no cover - protocol declaration


class NatsJetStreamAdmin:
    """Production admin seam over a nats-py ``JetStreamContext``.

    nats-py imports stay inside the methods so this module imports cleanly
    without the optional ``nats`` extra installed.
    """

    def __init__(self, js: Any) -> None:
        """Bind the seam to one nats-py JetStream context."""
        self._js = js

    async def stream_info(self, name: str) -> ExistingStream | None:
        """Return the live stream view, or ``None`` when absent."""
        from nats.js.errors import APIError

        try:
            info = await self._js.stream_info(name)
        except APIError as exc:
            if getattr(exc, "code", None) == 404:
                return None
            raise
        config = info.config
        return ExistingStream(
            subjects=frozenset(config.subjects or []),
            retention=getattr(config.retention, "value", str(config.retention)),
            discard=getattr(config.discard, "value", str(config.discard)),
            storage=getattr(config.storage, "value", str(config.storage)),
            max_age_seconds=int(config.max_age or 0),
            max_bytes=int(config.max_bytes or 0),
            max_msgs_per_subject=int(config.max_msgs_per_subject or -1),
            max_msg_size=int(config.max_msg_size or 0),
            metadata=dict(config.metadata or {}),
        )

    async def add_stream(self, topology: StreamTopology) -> None:
        """Create one stream from an exact topology."""
        from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

        config = StreamConfig(
            name=topology.name,
            subjects=sorted(topology.subjects),
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.OLD,
            storage=StorageType.FILE,
            max_age=topology.max_age_seconds,
            max_bytes=topology.max_bytes,
            max_msgs_per_subject=topology.max_msgs_per_subject,
            max_msg_size=topology.max_msg_size,
            metadata={MANAGED_ROLE_KEY: topology.role},
        )
        await self._js.add_stream(config=config)

    async def update_stream(self, name: str, *, subjects: frozenset[str]) -> None:
        """Replace only the subjects of one managed stream."""
        info = await self._js.stream_info(name)
        config = info.config
        config.subjects = sorted(subjects)
        await self._js.update_stream(config=config)

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> PubAckView:
        """Publish one message, translating sequence conflicts.

        Raises:
            SequenceConflictError: The expected subject sequence is stale.

        """
        from nats.js.errors import APIError

        try:
            ack = await self._js.publish(subject, payload, headers=headers)
        except APIError as exc:
            if getattr(exc, "err_code", None) == 10071:
                raise SequenceConflictError(str(exc)) from exc
            raise
        return PubAckView(stream=ack.stream, sequence=ack.seq, duplicate=bool(ack.duplicate))

    async def get_message(self, stream: str, sequence: int) -> StoredSourceMessage | None:
        """Fetch one source message by stream sequence, or ``None``."""
        from nats.js.errors import APIError

        try:
            msg = await self._js.get_msg(stream, sequence)
        except APIError as exc:
            if getattr(exc, "code", None) == 404 or getattr(exc, "err_code", None) in {
                10037,
                10059,
            }:
                return None
            raise
        return StoredSourceMessage(
            subject=msg.subject, data=msg.data, headers=msg.headers, sequence=msg.seq
        )

    async def get_last_message(self, stream: str, subject: str) -> StoredLedger | None:
        """Fetch the last message for one subject, or ``None``."""
        from nats.js.errors import APIError

        try:
            msg = await self._js.get_last_msg(stream, subject)
        except APIError as exc:
            if getattr(exc, "code", None) == 404 or getattr(exc, "err_code", None) in {
                10037,
                10059,
            }:
                return None
            raise
        return StoredLedger(sequence=msg.seq, payload=msg.data)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def _validate_existing(topology: StreamTopology, existing: ExistingStream) -> None:
    """Validate one existing stream against its exact topology.

    Raises:
        ForeignStreamError: The stream is not managed by this module.
        FailureResourceUndersizedError: A live limit is below the topology.

    """
    role = existing.metadata.get(MANAGED_ROLE_KEY)
    if role != topology.role:
        raise ForeignStreamError(
            f"stream {topology.name} exists without managed role {topology.role!r} "
            f"(found {role!r}); refusing to mutate a foreign stream"
        )
    if (
        existing.retention != topology.retention
        or existing.discard != topology.discard
        or existing.storage != topology.storage
    ):
        raise FailureResourceUndersizedError(
            f"stream {topology.name} policy differs from the required "
            f"{topology.retention}/{topology.discard}/{topology.storage} topology"
        )
    undersized: list[str] = []
    if existing.max_age_seconds < topology.max_age_seconds:
        undersized.append(
            f"max_age_seconds {existing.max_age_seconds} < {topology.max_age_seconds}"
        )
    if existing.max_bytes < topology.max_bytes:
        undersized.append(f"max_bytes {existing.max_bytes} < {topology.max_bytes}")
    if existing.max_msg_size < topology.max_msg_size:
        undersized.append(f"max_msg_size {existing.max_msg_size} < {topology.max_msg_size}")
    if topology.max_msgs_per_subject == -1:
        # JetStream reports -1 for an unlimited per-subject cap. A server that
        # reports any other value has a real cap, so the comparison stands.
        if existing.max_msgs_per_subject != -1:
            undersized.append(f"max_msgs_per_subject {existing.max_msgs_per_subject} != unlimited")
    elif existing.max_msgs_per_subject < topology.max_msgs_per_subject:
        undersized.append(
            f"max_msgs_per_subject {existing.max_msgs_per_subject} < "
            f"{topology.max_msgs_per_subject}"
        )
    if topology.name != ADVISORY_STREAM and not topology.subjects <= existing.subjects:
        undersized.append(
            f"subjects {sorted(topology.subjects - existing.subjects)} are not configured"
        )
    if undersized:
        raise FailureResourceUndersizedError(
            f"stream {topology.name} is undersized: {'; '.join(undersized)}"
        )


async def ensure_topologies(
    admin: JetStreamAdmin,
    *,
    auto_provision: bool,
    bindings: Any,
    source_stream: str,
) -> None:
    """Provision or validate the four exact failure-resource topologies.

    Missing streams are created when ``auto_provision`` is true; read-only
    mode raises instead. Advisory subjects are union-added on managed
    streams without removing foreign subjects. Limits are only validated;
    provisioning never lowers retention or capacity.

    Args:
        admin: JetStream admin seam.
        auto_provision: Create missing resources and add advisory subjects.
        bindings: Consumer bindings that derive advisory subjects.
        source_stream: Source JetStream stream name.

    Raises:
        FailureResourceMissingError: A stream is absent in read-only mode.
        ForeignStreamError: A same-named stream is not managed.
        FailureResourceUndersizedError: A managed stream is undersized.

    """
    topologies = (
        failure_stream_topology(),
        ledger_stream_topology(),
        advisory_stream_topology(bindings, source_stream),
        replay_receipts_stream_topology(),
    )
    for topology in topologies:
        existing = await admin.stream_info(topology.name)
        if existing is None:
            if not auto_provision:
                raise FailureResourceMissingError(
                    f"stream {topology.name} is absent and failure resources are "
                    "in read-only mode (auto_provision_failure_resources=False)"
                )
            logger.info("provisioning durable failure stream %s", topology.name)
            await admin.add_stream(topology)
            continue
        _validate_existing(topology, existing)
        if topology.name is ADVISORY_STREAM and not topology.subjects <= existing.subjects:
            missing = topology.subjects - existing.subjects
            if not auto_provision:
                raise FailureResourceUndersizedError(
                    f"stream {ADVISORY_STREAM} is missing advisory subjects "
                    f"{sorted(missing)} in read-only mode"
                )
            union = existing.subjects | topology.subjects
            logger.info("adding %d advisory subject(s) to %s", len(missing), ADVISORY_STREAM)
            await admin.update_stream(topology.name, subjects=union)


# ---------------------------------------------------------------------------
# Dead-letter publication
# ---------------------------------------------------------------------------


class DlqOutcome(Enum):
    """Result of one dead-letter publication attempt."""

    PUBLISHED = "published"
    ALREADY_PRESENT = "already_present"
    UNKNOWN = "unknown"


def failure_subject(failure_id: str) -> str:
    """Return the dead-letter subject for one failure id."""
    if not failure_id:
        raise ValueError("failure_id must be a non-empty string")
    return f"{FAILURE_SUBJECT_PREFIX}.{failure_id}"


class FailureStore:
    """Idempotent bounded dead-letter publisher.

    Publication carries ``Nats-Msg-Id`` for broker deduplication and an
    expected-last-subject-sequence of zero so a repeated publication is an
    idempotent success instead of a duplicate record.
    """

    def __init__(self, admin: JetStreamAdmin, *, clock: Callable[[], float] = time.time) -> None:
        """Bind the store to the admin seam and wall clock."""
        self._admin = admin
        self._clock = clock

    async def publish_failure(self, envelope: FailureEnvelope) -> DlqOutcome:
        """Publish one dead-letter envelope to its deterministic subject.

        The envelope bounds are validated before publication. A sequence
        conflict means the record is already present (idempotent success).

        Returns:
            The publication outcome. ``UNKNOWN`` means the broker outcome is
            uncertain and the caller must abstain.

        """
        payload = envelope.to_bytes()
        headers = {
            _MSG_ID_HEADER: envelope.failure_id,
            EXPECTED_LAST_SUBJECT_SEQUENCE_HEADER: "0",
        }
        try:
            await self._admin.publish(
                failure_subject(envelope.failure_id), payload, headers=headers
            )
        except SequenceConflictError:
            return DlqOutcome.ALREADY_PRESENT
        except Exception as exc:
            logger.warning(
                "dead-letter publication failed for failure id %s: %s",
                envelope.failure_id,
                exc,
            )
            return DlqOutcome.UNKNOWN
        return DlqOutcome.PUBLISHED


# ---------------------------------------------------------------------------
# Advisory parsing and reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaxDeliveryAdvisory:
    """One parsed max-delivery advisory.

    The advisory subject carries the authoritative ``(stream, consumer)``
    identity; the payload supplies ``stream_seq`` and optional context.

    Attributes:
        stream: Source stream name from the advisory subject.
        consumer: Source consumer (durable) name from the advisory subject.
        stream_seq: Authoritative stream sequence of the exhausted message.
        deliveries: Physical delivery count, when supplied.
        time: Advisory timestamp, when supplied.

    """

    stream: str
    consumer: str
    stream_seq: int
    deliveries: int | None
    time: str | None


def parse_max_delivery_advisory(subject: str, payload: bytes) -> MaxDeliveryAdvisory:
    """Parse one max-delivery advisory message.

    Args:
        subject: The advisory subject carrying ``<stream>.<consumer>``.
        payload: The advisory JSON payload.

    Returns:
        The validated advisory.

    Raises:
        ValueError: If the subject is not a max-delivery advisory subject or
            the payload is unusable.

    """
    prefix = f"{ADVISORY_SUBJECT_PREFIX}."
    if not subject.startswith(prefix):
        raise ValueError(f"not a max-delivery advisory subject: {subject!r}")
    parts = subject[len(prefix) :].split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"advisory subject lacks <stream>.<consumer>: {subject!r}")
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"advisory payload is not valid JSON: {exc}") from None
    if not isinstance(body, dict):
        raise ValueError("advisory payload must be a JSON object")
    stream_seq = body.get("stream_seq")
    if isinstance(stream_seq, bool) or not isinstance(stream_seq, int) or stream_seq < 1:
        raise ValueError(f"advisory stream_seq must be an integer >= 1, got {stream_seq!r}")
    deliveries = body.get("deliveries")
    if deliveries is not None and (
        isinstance(deliveries, bool) or not isinstance(deliveries, int) or deliveries < 1
    ):
        raise ValueError(f"advisory deliveries must be an integer >= 1, got {deliveries!r}")
    advisory_time = body.get("time")
    return MaxDeliveryAdvisory(
        stream=parts[0],
        consumer=parts[1],
        stream_seq=stream_seq,
        deliveries=deliveries,
        time=advisory_time if isinstance(advisory_time, str) else None,
    )


class ReconcileOutcome(Enum):
    """Result of one advisory reconciliation."""

    PUBLISHED = "published"
    ALREADY_PRESENT = "already_present"
    IGNORED = "ignored"
    UNKNOWN = "unknown"


class AdvisoryReconciler:
    """Reconcile max-delivery advisories into durable dead-letter records.

    The reconciler uses the authoritative ``(stream, consumer, stream_seq)``
    from each advisory to fetch the source message. A replayable dead-letter
    record keeps the fetched payload; a missing source message produces a
    durable metadata-only, non-replayable record.
    """

    def __init__(
        self,
        admin: JetStreamAdmin,
        *,
        max_failure_payload_bytes: int = MAX_FAILURE_PAYLOAD_BYTES,
        source_stream: str = "",
        bindings: tuple[Any, ...] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the reconciler to the admin seam, payload bound, and clock.

        Args:
            admin: JetStream admin seam.
            max_failure_payload_bytes: Payload bound for replayable records.
            source_stream: Source stream name; with ``bindings`` it derives
                the live advisory subjects for :meth:`start`.
            bindings: Consumer bindings whose durable names derive the live
                advisory subjects for :meth:`start`.
            clock: Wall-clock source; injectable for tests.

        """
        self._admin = admin
        self._max_failure_payload_bytes = max_failure_payload_bytes
        self._clock = clock
        self._advisory_subjects: tuple[str, ...] = (
            tuple(
                f"{ADVISORY_SUBJECT_PREFIX}.{source_stream}.{binding.durable}"
                for binding in bindings
            )
            if source_stream
            else ()
        )
        self._subscriptions: list[Any] = []
        # One store per reconciler: the store holds no per-publication state,
        # so rebuilding it for every advisory only allocates.
        self._failure_store = FailureStore(admin, clock=clock)

    async def reconcile_advisory(self, subject: str, payload: bytes) -> ReconcileOutcome:
        """Turn one advisory into one durable dead-letter publication.

        Malformed advisories are ignored without publication; publication
        outcomes mirror :class:`DlqOutcome`.
        """
        try:
            advisory = parse_max_delivery_advisory(subject, payload)
        except ValueError as exc:
            logger.warning("ignoring unusable advisory on %s: %s", subject, exc)
            return ReconcileOutcome.IGNORED

        identity = EventIdentity(
            stream=advisory.stream,
            durable=advisory.consumer,
            stream_sequence=advisory.stream_seq,
        )
        attempt_count = advisory.deliveries if advisory.deliveries else 1
        last_attempt_at = utc_iso8601(self._clock())
        first_failure_at = advisory.time or last_attempt_at

        try:
            source = await self._admin.get_message(advisory.stream, advisory.stream_seq)
        except Exception as exc:
            logger.warning(
                "source message lookup failed for %s seq=%d: %s",
                advisory.stream,
                advisory.stream_seq,
                exc,
            )
            return ReconcileOutcome.UNKNOWN

        if source is not None and len(source.data) <= self._max_failure_payload_bytes:
            timestamp = ""
            if isinstance(source.headers, dict):
                raw_ts = source.headers.get("Nats-Time-Stamp", "")
                if isinstance(raw_ts, str):
                    timestamp = raw_ts
            snapshot = MessageSnapshot(
                identity=identity,
                subject=source.subject,
                payload=source.data,
                timestamp=timestamp,
                delivery_count=attempt_count,
            )
            envelope = FailureEnvelope.build(
                snapshot,
                "handler_outcome_unknown",
                attempt_count=attempt_count,
                first_failure_at=first_failure_at,
                last_attempt_at=last_attempt_at,
            )
        else:
            envelope = FailureEnvelope.metadata_only(
                identity,
                "handler_outcome_unknown",
                attempt_count=attempt_count,
                first_failure_at=first_failure_at,
                last_attempt_at=last_attempt_at,
            )

        outcome = await self._failure_store.publish_failure(envelope)
        return ReconcileOutcome(outcome.value)

    # ------------------------------------------------------------------
    # Live advisory subscription lifecycle
    # ------------------------------------------------------------------

    async def start(self, connector: Any) -> None:
        """Subscribe to the live max-delivery advisories for every binding.

        The subscriptions are ephemeral core subscriptions, so they never
        compete with durable consumer state. Advisory payloads reconcile
        into idempotent dead-letter records. A repeated ``start`` first
        stops the previous subscriptions.

        Args:
            connector: Active NATS connection (``nc``) whose async
                ``subscribe(subject, cb=...)`` creates a core subscription.

        """
        if self._subscriptions:
            await self.stop()
        for subject in self._advisory_subjects:

            async def _on_advisory(msg: Any) -> None:
                try:
                    await self.reconcile_advisory(msg.subject, msg.data)
                except Exception as exc:  # defensive: reconcile_advisory is total
                    logger.warning("advisory reconciliation raised on %s: %s", msg.subject, exc)

            self._subscriptions.append(await connector.subscribe(subject, cb=_on_advisory))
        if self._advisory_subjects:
            logger.info(
                "subscribed to %d max-delivery advisory subject(s)",
                len(self._advisory_subjects),
            )

    async def stop(self) -> None:
        """Unsubscribe every live advisory subscription; tolerate errors."""
        for subscription in self._subscriptions:
            try:
                await subscription.unsubscribe()
            except Exception as exc:
                logger.warning("advisory unsubscribe failed: %s", exc)
        self._subscriptions.clear()


# ---------------------------------------------------------------------------
# Prepared resource bundle
# ---------------------------------------------------------------------------


class AdminLedgerTransport:
    """Adapt a :class:`JetStreamAdmin` seam to the ledger transport."""

    def __init__(self, admin: JetStreamAdmin, *, stream: str = LEDGER_STREAM) -> None:
        """Bind the adapter to the admin seam and ledger stream name."""
        self._admin = admin
        self._stream = stream

    async def read(self, subject: str) -> StoredLedger | None:
        """Return the current ledger row for the subject.

        Raises:
            LedgerStoreError: The store cannot satisfy the read.

        """
        try:
            return await self._admin.get_last_message(self._stream, subject)
        except Exception as exc:
            raise LedgerStoreError(f"ledger read failed for {subject}: {exc}") from exc

    async def write(self, subject: str, payload: bytes, *, expected_sequence: int) -> int:
        """CAS-publish one ledger record.

        Raises:
            LedgerSequenceConflictError: The expected sequence is stale.
            LedgerStoreError: The store cannot satisfy the write.

        """
        try:
            ack = await self._admin.publish(
                subject,
                payload,
                headers={EXPECTED_LAST_SUBJECT_SEQUENCE_HEADER: str(expected_sequence)},
            )
        except SequenceConflictError as exc:
            raise LedgerSequenceConflictError(str(exc)) from exc
        except Exception as exc:
            raise LedgerStoreError(f"ledger write failed for {subject}: {exc}") from exc
        return ack.sequence


@dataclass
class FailureResources:
    """The prepared durable failure stores used by the subscriber.

    Attributes:
        ledger: CAS delivery ledger.
        store: Idempotent dead-letter publisher.
        advisory_reconciler: Advisory-to-DLQ reconciler.

    """

    ledger: DeliveryLedger
    store: FailureStore
    advisory_reconciler: AdvisoryReconciler

    @classmethod
    async def prepare(
        cls,
        admin: JetStreamAdmin,
        config: Any,
        bindings: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> FailureResources:
        """Provision/open the failure resources and build the stores.

        Args:
            admin: JetStream admin seam.
            config: :class:`~hephaestus.nats.config.NATSConfig` controlling
                provisioning mode, attempt budget, lease duration, and the
                payload bound.
            bindings: Consumer bindings deriving advisory subjects.
            clock: Wall-clock source; injectable for tests.

        Returns:
            The wired resource bundle.

        """
        await ensure_topologies(
            admin,
            auto_provision=config.auto_provision_failure_resources,
            bindings=bindings,
            source_stream=config.stream,
        )
        return cls(
            ledger=DeliveryLedger(
                AdminLedgerTransport(admin),
                max_attempts=config.max_processing_attempts,
                lease_seconds=config.ledger_lease_seconds,
                clock=clock,
            ),
            store=FailureStore(admin, clock=clock),
            advisory_reconciler=AdvisoryReconciler(
                admin,
                max_failure_payload_bytes=config.max_failure_payload_bytes,
                source_stream=config.stream,
                bindings=tuple(bindings),
                clock=clock,
            ),
        )
