"""Delivery ledger for durable NATS failure handling (#2395).

The delivery ledger keeps exactly one current record per event on the
``hephaestus.nats.ledger.<event_id>`` subject of the delivery-ledger
stream. State transitions use expected-last-subject-sequence compare-and-set
(CAS) publication, so concurrent owners cannot corrupt the record.

Ledger state machine::

    absent    -> running      claim handler ownership
    ready     -> running      claim the next processing attempt
    running   -> ready        known decode/handler failure below the budget
    running   -> completed    handler returned and the owner CAS succeeded
    running   -> handoff      processing budget exhausted
    running   -> handoff      lease expired with the handler outcome unknown
    handoff   -> terminal     DLQ PubAck confirmed
    completed -> acked        source ack_sync confirmed

CAS conflicts are re-read; persistent conflicts and store uncertainty return
``DeliveryAction.ABSTAIN`` so the caller makes no client disposition.

Usage::

    claim = await ledger.claim(identity)
    if claim.action is DeliveryAction.PROCESS:
        ...  # run the handler
        completion = await ledger.record_completed(claim)
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from secrets import token_hex
from typing import Any, NamedTuple, Protocol

from hephaestus.nats.failure_models import (
    FAILURE_KINDS,
    EventIdentity,
    FailureKind,
)

logger = logging.getLogger(__name__)

LEDGER_SUBJECT_PREFIX = "hephaestus.nats.ledger"
"""Subject prefix for one-ledger-record-per-event publication."""

_MAX_CAS_ATTEMPTS = 3
"""Bounded claim retries after expected-sequence conflicts."""


class LedgerState(Enum):
    """Durable per-event delivery states."""

    RUNNING = "running"
    READY = "ready"
    COMPLETED = "completed"
    HANDOFF = "handoff"
    TERMINAL = "terminal"
    ACKED = "acked"


class DeliveryAction(Enum):
    """The action a ledger operation instructs the subscriber to take."""

    PROCESS = "process"
    ACK = "ack"
    HANDOFF = "handoff"
    TERM = "term"
    ABSTAIN = "abstain"
    READY = "ready"


class LedgerSequenceConflictError(Exception):
    """A CAS publication lost the expected-subject-sequence race."""


class LedgerStoreError(Exception):
    """The ledger store cannot satisfy the request without corruption."""


class StoredLedger(NamedTuple):
    """One raw ledger subject row as returned by the transport.

    Attributes:
        sequence: Last subject sequence for CAS publication.
        payload: Serialized :class:`LedgerRecord` bytes.

    """

    sequence: int
    payload: bytes


class LedgerTransport(Protocol):
    """Minimal async store seam the ledger needs from JetStream."""

    async def read(self, subject: str) -> StoredLedger | None:
        """Return the current row for *subject*, or ``None`` when absent."""
        ...  # pragma: no cover - protocol declaration

    async def write(self, subject: str, payload: bytes, *, expected_sequence: int) -> int:
        """Publish *payload* with expected-last-subject-sequence CAS.

        Returns the new subject sequence.

        Raises:
            LedgerSequenceConflictError: The expected sequence is stale.
            LedgerStoreError: The store cannot satisfy the write.

        """
        ...  # pragma: no cover - protocol declaration


def ledger_subject(event_id: str) -> str:
    """Return the ledger subject for one deterministic event id."""
    if not event_id:
        raise ValueError("event_id must be a non-empty string")
    return f"{LEDGER_SUBJECT_PREFIX}.{event_id}"


@dataclass(frozen=True)
class LedgerRecord:
    """One durable delivery record.

    Attributes:
        event_id: Deterministic source event identifier.
        state: Current ledger state.
        attempt_count: Processing attempts claimed so far.
        owner_token: Token of the owning subscriber for ``running`` records.
        lease_expires_at: Lease expiry (Unix seconds) for ``running`` records.
        failure_kind: Bounded failure classification for ``handoff`` records.
        first_attempt_at: Unix timestamp of the first processing attempt.
        updated_at: Unix timestamp of the last state transition.

    """

    event_id: str
    state: LedgerState
    attempt_count: int
    owner_token: str | None
    lease_expires_at: float | None
    failure_kind: FailureKind | None
    first_attempt_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        """Return the bounded serialization mapping for the record."""
        return {
            "event_id": self.event_id,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "owner_token": self.owner_token,
            "lease_expires_at": self.lease_expires_at,
            "failure_kind": self.failure_kind,
            "first_attempt_at": self.first_attempt_at,
            "updated_at": self.updated_at,
        }

    def to_bytes(self) -> bytes:
        """Serialize the record to compact JSON bytes."""
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, payload: bytes) -> LedgerRecord:
        """Parse and validate one durable record.

        Raises:
            ValueError: If the payload is corrupt or violates the schema.

        """
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"ledger record is not valid JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise ValueError("ledger record must be a JSON object")
        try:
            state = LedgerState(raw["state"])
        except (KeyError, ValueError):
            raise ValueError(f"ledger record has an unknown state: {raw.get('state')!r}") from None
        failure_kind = raw.get("failure_kind")
        if failure_kind is not None and failure_kind not in FAILURE_KINDS:
            raise ValueError(f"ledger record has an unknown failure kind: {failure_kind!r}")
        owner_token = raw.get("owner_token")
        if owner_token is not None and not isinstance(owner_token, str):
            raise ValueError("ledger record owner_token must be a string or None")
        lease_expires_at = raw.get("lease_expires_at")
        if lease_expires_at is not None and not isinstance(lease_expires_at, (int, float)):
            raise ValueError("ledger record lease_expires_at must be a number or None")
        event_id = raw.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("ledger record event_id must be a non-empty string")
        attempt_count = raw.get("attempt_count")
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int):
            raise ValueError("ledger record attempt_count must be an integer")
        try:
            first_attempt_at = float(raw["first_attempt_at"])
            updated_at = float(raw["updated_at"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("ledger record timestamps must be numbers") from None
        return cls(
            event_id=event_id,
            state=state,
            attempt_count=attempt_count,
            owner_token=owner_token,
            lease_expires_at=None if lease_expires_at is None else float(lease_expires_at),
            failure_kind=failure_kind,
            first_attempt_at=first_attempt_at,
            updated_at=updated_at,
        )


@dataclass(frozen=True)
class DeliveryClaim:
    """The outcome of a ledger operation.

    Attributes:
        action: The subscriber action the ledger guarantees is safe.
        record: The record the action derives from, or ``None`` when the
            store could not supply one.

    """

    action: DeliveryAction
    record: LedgerRecord | None


class DeliveryLedger:
    """One-record-per-event delivery ledger with CAS ownership."""

    def __init__(
        self,
        transport: LedgerTransport,
        *,
        max_attempts: int,
        lease_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialize the ledger.

        Args:
            transport: Store seam implementing :class:`LedgerTransport`.
            max_attempts: Processing attempts before a failure hands off.
            lease_seconds: Ownership lease duration for ``running`` records.
            clock: Wall-clock source (Unix seconds); injectable for tests.

        """
        self._transport = transport
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._clock = clock

    # ------------------------------------------------------------------
    # Entry operations
    # ------------------------------------------------------------------

    async def claim(self, identity: EventIdentity) -> DeliveryClaim:
        """Claim processing ownership or return the safe duplicate action.

        CAS conflicts are re-read with a bounded retry budget. Store
        uncertainty abstains without any client disposition.
        """
        subject = ledger_subject(identity.event_id)
        for _ in range(_MAX_CAS_ATTEMPTS):
            try:
                stored = await self._transport.read(subject)
            except Exception as exc:
                logger.warning("delivery ledger read failed for %s: %s", subject, exc)
                return DeliveryClaim(DeliveryAction.ABSTAIN, None)
            now = self._clock()
            if stored is None:
                outcome = await self._claim_fresh(subject, identity, now)
            else:
                try:
                    prior = LedgerRecord.from_bytes(stored.payload)
                except ValueError as exc:
                    logger.warning("delivery ledger record is corrupt for %s: %s", subject, exc)
                    return DeliveryClaim(DeliveryAction.ABSTAIN, None)
                outcome = await self._claim_prior(subject, prior, stored.sequence, now)
            if outcome is None:
                continue  # CAS lost; re-read
            return outcome

        logger.warning("delivery ledger claim exhausted CAS retries for %s", subject)
        return DeliveryClaim(DeliveryAction.ABSTAIN, None)

    async def record_failure(
        self,
        claim: DeliveryClaim,
        failure_kind: FailureKind,
    ) -> DeliveryClaim:
        """Record a known decode/handler failure for an owned claim.

        Below the processing budget the record returns to ``ready``; at the
        budget it transitions to ``handoff`` with the failure kind recorded.
        """
        if claim.action is not DeliveryAction.PROCESS or claim.record is None:
            return DeliveryClaim(DeliveryAction.ABSTAIN, None)
        subject = ledger_subject(claim.record.event_id)
        guard = await self._read_owned(subject, claim.record, LedgerState.RUNNING)
        if guard is None or guard.action is not DeliveryAction.PROCESS:
            return DeliveryClaim(DeliveryAction.ABSTAIN, None)
        now = self._clock()
        prior = guard.record
        if prior is None:  # defensive: _read_owned guarantees a record
            return DeliveryClaim(DeliveryAction.ABSTAIN, None)
        if prior.attempt_count < self._max_attempts:
            record = replace(
                prior,
                state=LedgerState.READY,
                owner_token=None,
                lease_expires_at=None,
                updated_at=now,
            )
            action = DeliveryAction.READY
        else:
            record = replace(
                prior,
                state=LedgerState.HANDOFF,
                owner_token=None,
                lease_expires_at=None,
                failure_kind=failure_kind,
                updated_at=now,
            )
            action = DeliveryAction.HANDOFF
        outcome = await self._publish(subject, record, expected_sequence=guard.sequence)
        if outcome is None or outcome.action is DeliveryAction.ABSTAIN:
            return DeliveryClaim(DeliveryAction.ABSTAIN, None)
        return DeliveryClaim(action, record)

    async def record_completed(self, claim: DeliveryClaim) -> DeliveryClaim:
        """Record successful handler completion for an owned claim.

        A successful owner CAS returns ``ACK``; ownership loss or store
        uncertainty abstains so the handler is never re-invoked by mistake.
        """
        if claim.action is not DeliveryAction.PROCESS or claim.record is None:
            return DeliveryClaim(DeliveryAction.ABSTAIN, None)
        subject = ledger_subject(claim.record.event_id)
        guard = await self._read_owned(subject, claim.record, LedgerState.RUNNING)
        if guard is None or guard.action is not DeliveryAction.PROCESS:
            return DeliveryClaim(DeliveryAction.ABSTAIN, None)
        now = self._clock()
        prior = guard.record
        if prior is None:  # defensive: _read_owned guarantees a record
            return DeliveryClaim(DeliveryAction.ABSTAIN, None)
        record = replace(
            prior,
            state=LedgerState.COMPLETED,
            owner_token=None,
            lease_expires_at=None,
            updated_at=now,
        )
        outcome = await self._publish(subject, record, expected_sequence=guard.sequence)
        if outcome is None or outcome.action is DeliveryAction.ABSTAIN:
            return DeliveryClaim(DeliveryAction.ABSTAIN, None)
        return DeliveryClaim(DeliveryAction.ACK, record)

    async def record_acked(self, claim: DeliveryClaim) -> bool:
        """Mark a completed record as acknowledged after a confirmed ack_sync.

        Best-effort: a mismatch means forward progress is impossible here;
        the exact-head redelivery path re-derives the safe action.
        """
        return await self._transition(
            claim,
            from_state=LedgerState.COMPLETED,
            to_state=LedgerState.ACKED,
            require_owner=False,
        )

    async def record_terminal(self, claim: DeliveryClaim) -> bool:
        """Mark a handoff record terminal after a confirmed DLQ PubAck."""
        return await self._transition(
            claim,
            from_state=LedgerState.HANDOFF,
            to_state=LedgerState.TERMINAL,
            require_owner=False,
        )

    async def renew_lease(self, claim: DeliveryClaim) -> bool:
        """Renew the ownership lease for a running record.

        Returns ``False`` on ownership loss or store uncertainty; the
        completion CAS is the authoritative fallback gate.
        """
        if claim.action is not DeliveryAction.PROCESS or claim.record is None:
            return False
        subject = ledger_subject(claim.record.event_id)
        guard = await self._read_owned(subject, claim.record, LedgerState.RUNNING)
        if guard is None or guard.action is not DeliveryAction.PROCESS:
            return False
        now = self._clock()
        prior = guard.record
        if prior is None:  # defensive: _read_owned guarantees a record
            return False
        record = replace(prior, lease_expires_at=now + self._lease_seconds, updated_at=now)
        outcome = await self._publish(subject, record, expected_sequence=guard.sequence)
        return outcome is not None and outcome.action is not DeliveryAction.ABSTAIN

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _claim_fresh(
        self,
        subject: str,
        identity: EventIdentity,
        now: float,
    ) -> DeliveryClaim | None:
        """Claim an absent subject as the first running attempt.

        Returns ``None`` when the CAS race is lost so the caller re-reads.
        """
        record = LedgerRecord(
            event_id=identity.event_id,
            state=LedgerState.RUNNING,
            attempt_count=1,
            owner_token=token_hex(8),
            lease_expires_at=now + self._lease_seconds,
            failure_kind=None,
            first_attempt_at=now,
            updated_at=now,
        )
        outcome = await self._publish(subject, record, expected_sequence=0)
        return self._publish_outcome(outcome, DeliveryAction.PROCESS, record)

    async def _claim_prior(
        self,
        subject: str,
        prior: LedgerRecord,
        sequence: int,
        now: float,
    ) -> DeliveryClaim | None:
        """Claim or short-circuit one existing record by its durable state."""
        if prior.state is LedgerState.READY:
            record = replace(
                prior,
                state=LedgerState.RUNNING,
                attempt_count=prior.attempt_count + 1,
                owner_token=token_hex(8),
                lease_expires_at=now + self._lease_seconds,
                updated_at=now,
            )
            outcome = await self._publish(subject, record, expected_sequence=sequence)
            return self._publish_outcome(outcome, DeliveryAction.PROCESS, record)
        if prior.state is LedgerState.RUNNING:
            if prior.lease_expires_at is not None and prior.lease_expires_at > now:
                return DeliveryClaim(DeliveryAction.ABSTAIN, prior)
            record = replace(
                prior,
                state=LedgerState.HANDOFF,
                owner_token=None,
                lease_expires_at=None,
                failure_kind="handler_outcome_unknown",
                updated_at=now,
            )
            outcome = await self._publish(subject, record, expected_sequence=sequence)
            return self._publish_outcome(outcome, DeliveryAction.HANDOFF, record)
        if prior.state in (LedgerState.COMPLETED, LedgerState.ACKED):
            return DeliveryClaim(DeliveryAction.ACK, prior)
        if prior.state is LedgerState.HANDOFF:
            return DeliveryClaim(DeliveryAction.HANDOFF, prior)
        return DeliveryClaim(DeliveryAction.TERM, prior)

    def _publish_outcome(
        self,
        outcome: _GuardResult | None,
        success_action: DeliveryAction,
        record: LedgerRecord,
    ) -> DeliveryClaim | None:
        """Map a publish result to a claim.

        ``None`` means the CAS race was lost and the caller must re-read.
        """
        if outcome is None:
            return None
        if outcome.action is DeliveryAction.ABSTAIN:
            return DeliveryClaim(DeliveryAction.ABSTAIN, outcome.record)
        return DeliveryClaim(success_action, record)

    async def _transition(
        self,
        claim: DeliveryClaim,
        *,
        from_state: LedgerState,
        to_state: LedgerState,
        require_owner: bool,
    ) -> bool:
        """CAS one state-to-state transition with a bounded single shot."""
        if claim.record is None:
            return False
        subject = ledger_subject(claim.record.event_id)
        try:
            stored = await self._transport.read(subject)
        except Exception as exc:
            logger.warning("delivery ledger read failed for %s: %s", subject, exc)
            return False
        if stored is None:
            return False
        try:
            prior = LedgerRecord.from_bytes(stored.payload)
        except ValueError:
            return False
        if prior.state is not from_state:
            return False
        if require_owner and prior.owner_token != claim.record.owner_token:
            return False
        record = replace(prior, state=to_state, updated_at=self._clock())
        outcome = await self._publish(subject, record, expected_sequence=stored.sequence)
        return outcome is not None and outcome.action is not DeliveryAction.ABSTAIN

    async def _read_owned(
        self,
        subject: str,
        expected: LedgerRecord,
        state: LedgerState,
    ) -> _GuardResult | None:
        """Read and verify ownership/state before a mutation.

        Returns a :class:`_GuardResult` with ``PROCESS`` when the caller still
        owns the record, ``ABSTAIN`` on store uncertainty or ownership loss,
        or ``None`` when the subject is absent.
        """
        try:
            stored = await self._transport.read(subject)
        except Exception as exc:
            logger.warning("delivery ledger read failed for %s: %s", subject, exc)
            return _GuardResult(DeliveryAction.ABSTAIN, None, 0)
        if stored is None:
            return None
        try:
            prior = LedgerRecord.from_bytes(stored.payload)
        except ValueError:
            return _GuardResult(DeliveryAction.ABSTAIN, None, 0)
        if prior.state is state and prior.owner_token == expected.owner_token:
            return _GuardResult(DeliveryAction.PROCESS, prior, stored.sequence)
        return _GuardResult(DeliveryAction.ABSTAIN, None, stored.sequence)

    async def _publish(
        self,
        subject: str,
        record: LedgerRecord,
        *,
        expected_sequence: int,
    ) -> _GuardResult | None:
        """CAS-publish one record.

        Returns ``None`` on sequence conflict (caller re-reads), an ABSTAIN
        result on store uncertainty, or a PROCESS result on success.
        """
        try:
            sequence = await self._transport.write(
                subject,
                record.to_bytes(),
                expected_sequence=expected_sequence,
            )
        except LedgerSequenceConflictError:
            return None
        except Exception as exc:
            logger.warning("delivery ledger write failed for %s: %s", subject, exc)
            return _GuardResult(DeliveryAction.ABSTAIN, None, 0)
        return _GuardResult(DeliveryAction.PROCESS, record, sequence)


@dataclass(frozen=True)
class _GuardResult:
    """Internal carrier for guard/publish outcomes including the sequence."""

    action: DeliveryAction
    record: LedgerRecord | None
    sequence: int
