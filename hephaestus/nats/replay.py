"""Idempotent replay of terminal dead-letter records (#2395).

``FailureReplayer`` republishes the captured payload of one durable
dead-letter record to its original subject. Replay is safe to repeat after
a crash or a restart:

- A caller-supplied ``request_id`` derives a deterministic receipt id and
  receipt subject. The receipt is created by a compare-and-swap publish
  before the original payload is republished, so a repeated request never
  publishes twice.
- The original publication carries a deterministic ``Nats-Msg-Id``, so a
  broker-side duplicate window suppresses a same-request republish.
- A ``completed`` receipt returns its original :class:`PubAckView` without
  republishing, including after a process restart.
- A ``publishing`` receipt is an indeterminate outcome: the original
  publication may already have reached the broker. The replayer surfaces
  :attr:`ReplayOutcome.UNKNOWN` for explicit operator reconciliation
  instead of risking a duplicate publish.

Only replayable records (a captured payload within the exact DLQ bounds)
are replayed. Missing records raise :class:`ReplayNotFoundError`;
non-replayable records raise :class:`ReplayNotReplayableError`.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Literal

from hephaestus.nats.failure_models import (
    DLQ_MAX_MSG_SIZE,
    FailureEnvelope,
    canonical_json,
    sha256_hex,
)
from hephaestus.nats.failure_store import (
    FAILURE_STREAM,
    REPLAY_RECEIPT_SUBJECT_PREFIX,
    REPLAY_RECEIPTS_STREAM,
    JetStreamAdmin,
    PubAckView,
    SequenceConflictError,
    failure_subject,
)

logger = logging.getLogger(__name__)

_MSG_ID_HEADER = "Nats-Msg-Id"
_EXPECTED_HEADER = "Nats-Expected-Last-Subject-Sequence"

ReceiptState = Literal["publishing", "completed"]
"""Bounded replay-receipt states."""


def replay_receipt_id(failure_id: str, request_id: str) -> str:
    """Return the deterministic receipt id for one replay request."""
    return sha256_hex(canonical_json(["hephaestus-replay-receipt-v1", failure_id, request_id]))


def replay_message_id(failure_id: str, receipt_id: str) -> str:
    """Return the deterministic ``Nats-Msg-Id`` for one replay publication."""
    return sha256_hex(canonical_json(["hephaestus-replay-msg-v1", failure_id, receipt_id]))


def replay_receipt_subject(receipt_id: str) -> str:
    """Return the replay-receipt subject for one deterministic receipt id."""
    if not receipt_id:
        raise ValueError("receipt_id must be a non-empty string")
    return f"{REPLAY_RECEIPT_SUBJECT_PREFIX}.{receipt_id}"


class ReplayOutcome(Enum):
    """The bounded outcomes of one replay request."""

    REPLAYED = "replayed"
    ALREADY_REPLAYED = "already_replayed"
    UNKNOWN = "unknown"


class ReplayNotFoundError(Exception):
    """No dead-letter record exists for the requested failure id."""


class ReplayNotReplayableError(Exception):
    """The dead-letter record lacks a replayable in-bounds payload."""


@dataclass(frozen=True)
class ReplayReceipt:
    """One durable replay receipt.

    Attributes:
        receipt_id: Deterministic receipt identifier.
        failure_id: Dead-letter record the replay republishes.
        request_id: Caller-supplied idempotency key.
        state: ``publishing`` before the original publish confirms,
            ``completed`` after the receipt records the PubAck.
        created_at: Unix timestamp of receipt creation.
        pub_stream: Stream of the original-subject PubAck (completed only).
        pub_sequence: Stream sequence of the original-subject PubAck
            (completed only).

    """

    receipt_id: str
    failure_id: str
    request_id: str
    state: ReceiptState
    created_at: float
    pub_stream: str | None
    pub_sequence: int | None

    def to_dict(self) -> dict[str, Any]:
        """Return the bounded serialization mapping for the receipt."""
        return {
            "receipt_id": self.receipt_id,
            "failure_id": self.failure_id,
            "request_id": self.request_id,
            "state": self.state,
            "created_at": self.created_at,
            "pub_stream": self.pub_stream,
            "pub_sequence": self.pub_sequence,
        }

    def to_bytes(self) -> bytes:
        """Serialize the receipt to compact JSON bytes."""
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, payload: bytes) -> ReplayReceipt:
        """Parse and validate one durable receipt.

        Raises:
            ValueError: If the payload is corrupt or violates the schema.

        """
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"replay receipt is not valid JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise ValueError("replay receipt must be a JSON object")
        try:
            receipt_id = raw["receipt_id"]
            failure_id = raw["failure_id"]
            request_id = raw["request_id"]
            state = raw["state"]
            created_at = raw["created_at"]
        except KeyError as exc:
            raise ValueError(f"replay receipt is missing a required key: {exc}") from None
        if not all(
            isinstance(value, str) and value for value in (receipt_id, failure_id, request_id)
        ):
            raise ValueError("replay receipt ids must be non-empty strings")
        if state not in ("publishing", "completed"):
            raise ValueError(f"replay receipt has an unknown state: {state!r}")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            raise ValueError("replay receipt created_at must be a number")
        pub_stream = raw.get("pub_stream")
        pub_sequence = raw.get("pub_sequence")
        if state == "completed" and (not isinstance(pub_stream, str) or not pub_stream):
            raise ValueError("a completed replay receipt must carry the publication stream")
        if state == "completed" and (
            isinstance(pub_sequence, bool) or not isinstance(pub_sequence, int) or pub_sequence < 1
        ):
            raise ValueError("a completed replay receipt must carry the publication sequence")
        return cls(
            receipt_id=receipt_id,
            failure_id=failure_id,
            request_id=request_id,
            state=state,
            created_at=float(created_at),
            pub_stream=pub_stream if isinstance(pub_stream, str) else None,
            pub_sequence=pub_sequence if isinstance(pub_sequence, int) else None,
        )


@dataclass(frozen=True)
class ReplayResult:
    """The outcome of one replay request.

    Attributes:
        outcome: Bounded outcome classification.
        receipt_id: The deterministic receipt for the request.
        puback: The original-subject PubAck for a replayed or already
            replayed request; ``None`` otherwise.

    """

    outcome: ReplayOutcome
    receipt_id: str
    puback: PubAckView | None


class FailureReplayer:
    """Idempotent replayer for terminal dead-letter records."""

    def __init__(self, admin: JetStreamAdmin, *, clock: Callable[[], float] = time.time) -> None:
        """Bind the replayer to the admin seam and wall clock."""
        self._admin = admin
        self._clock = clock

    async def replay(self, failure_id: str, request_id: str) -> ReplayResult:
        """Republish one dead-letter payload to its original subject.

        Args:
            failure_id: The deterministic dead-letter record identifier.
            request_id: Caller-supplied idempotency key for this replay.

        Returns:
            The replay result. A completed receipt returns its original
            PubAck without republishing; an indeterminate receipt or a
            failed original publication returns :attr:`ReplayOutcome.UNKNOWN`.

        Raises:
            ValueError: If an identifier is not a non-empty string.
            ReplayNotFoundError: No dead-letter record exists.
            ReplayNotReplayableError: The record lacks a replayable payload.

        """
        if not isinstance(failure_id, str) or not failure_id:
            raise ValueError(f"failure_id must be a non-empty string, got {failure_id!r}")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"request_id must be a non-empty string, got {request_id!r}")

        receipt_id = replay_receipt_id(failure_id, request_id)
        subject = replay_receipt_subject(receipt_id)

        existing = await self._read_receipt(subject)
        if existing is not None:
            return self._resume(existing, receipt_id)

        envelope = await self._load_replayable_envelope(failure_id)

        receipt = ReplayReceipt(
            receipt_id=receipt_id,
            failure_id=failure_id,
            request_id=request_id,
            state="publishing",
            created_at=self._clock(),
            pub_stream=None,
            pub_sequence=None,
        )
        try:
            receipt_ack = await self._admin.publish(
                subject,
                receipt.to_bytes(),
                headers={_EXPECTED_HEADER: "0"},
            )
        except SequenceConflictError:
            raced = await self._read_receipt(subject)
            if raced is None:
                return ReplayResult(ReplayOutcome.UNKNOWN, receipt_id, None)
            return self._resume(raced, receipt_id)

        payload = base64.b64decode(envelope.payload_b64 or "")
        try:
            puback = await self._admin.publish(
                envelope.subject,
                payload,
                headers={_MSG_ID_HEADER: replay_message_id(failure_id, receipt_id)},
            )
        except Exception as exc:
            logger.warning(
                "replay publication for failure id %s is indeterminate: %s",
                failure_id,
                exc,
            )
            return ReplayResult(ReplayOutcome.UNKNOWN, receipt_id, None)

        completed = replace(
            receipt,
            state="completed",
            pub_stream=puback.stream,
            pub_sequence=puback.sequence,
        )
        try:
            await self._admin.publish(
                subject,
                completed.to_bytes(),
                headers={_EXPECTED_HEADER: str(receipt_ack.sequence)},
            )
        except SequenceConflictError:
            raced = await self._read_receipt(subject)
            if raced is not None and raced.state == "completed":
                return self._resume(raced, receipt_id)
            return ReplayResult(ReplayOutcome.UNKNOWN, receipt_id, puback)
        return ReplayResult(ReplayOutcome.REPLAYED, receipt_id, puback)

    def _resume(self, receipt: ReplayReceipt, receipt_id: str) -> ReplayResult:
        """Resolve an existing receipt without a new publication."""
        if receipt.state == "completed":
            pub_stream = receipt.pub_stream
            pub_sequence = receipt.pub_sequence
            if (
                not pub_stream
                or not isinstance(pub_sequence, int)
                or isinstance(pub_sequence, bool)
                or pub_sequence < 1
            ):
                # Unreachable from durable records: from_bytes enforces the
                # completed-receipt publication fields.
                raise ValueError(f"completed receipt {receipt_id} lacks its PubAck fields")
            return ReplayResult(
                ReplayOutcome.ALREADY_REPLAYED,
                receipt_id,
                PubAckView(
                    stream=pub_stream,
                    sequence=pub_sequence,
                    duplicate=True,
                ),
            )
        logger.warning(
            "replay receipt %s is stuck in publishing; reconcile the "
            "deterministic Nats-Msg-Id before retrying",
            receipt_id,
        )
        return ReplayResult(ReplayOutcome.UNKNOWN, receipt_id, None)

    async def _read_receipt(self, subject: str) -> ReplayReceipt | None:
        stored = await self._admin.get_last_message(REPLAY_RECEIPTS_STREAM, subject)
        if stored is None:
            return None
        return ReplayReceipt.from_bytes(stored.payload)

    async def _load_replayable_envelope(self, failure_id: str) -> FailureEnvelope:
        stored = await self._admin.get_last_message(FAILURE_STREAM, failure_subject(failure_id))
        if stored is None:
            raise ReplayNotFoundError(f"no dead-letter record for failure id {failure_id}")
        try:
            raw = json.loads(stored.payload)
        except json.JSONDecodeError as exc:
            raise ReplayNotReplayableError(
                f"dead-letter record {failure_id} is not valid JSON"
            ) from exc
        try:
            envelope = FailureEnvelope.from_dict(raw)
        except ValueError as exc:
            raise ReplayNotReplayableError(
                f"dead-letter record {failure_id} fails envelope validation: {exc}"
            ) from exc
        if not envelope.replayable or envelope.payload_b64 is None:
            raise ReplayNotReplayableError(
                f"dead-letter record {failure_id} has no captured payload"
            )
        try:
            payload = base64.b64decode(envelope.payload_b64, validate=True)
        except ValueError as exc:
            raise ReplayNotReplayableError(
                f"dead-letter record {failure_id} carries an invalid payload encoding"
            ) from exc
        if len(payload) > DLQ_MAX_MSG_SIZE:
            raise ReplayNotReplayableError(
                f"dead-letter record {failure_id} payload exceeds the DLQ bound"
            )
        return envelope
