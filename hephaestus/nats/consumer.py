"""Explicit pull consumers for the NATS subscriber (#2395).

Durable failure handling needs explicit ``AckPolicy.EXPLICIT`` pull
consumers with bounded ``ack_wait``, ``max_deliver``, and
``idle_heartbeat`` values. ``ConsumerManager.ensure`` validates existing
consumers against that contract:

- An absent consumer is created with the explicit configuration.
- A compatible consumer is reused; tunable drift is updated in place.
- An incompatible acknowledgement policy fails closed by default.
  ``migrate_incompatible_consumers=True`` performs the opt-in
  snapshot/delete/recreate cutover at ``ack_floor.stream_seq + 1`` and
  restores the original consumer when recreation fails. A still-bound
  consumer always refuses the migration.

:class:`DeliveryHeartbeat` protects slow synchronous handlers: it shields
``msg.in_progress()`` and renews the delivery-ledger lease on every
``heartbeat_interval_seconds`` tick until ``ack_wait`` bounds the
delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.delivery_ledger import DeliveryClaim
from hephaestus.nats.failure_models import ConsumerBinding

logger = logging.getLogger(__name__)

DELIVER_ALL = "all"
"""Fresh consumers read every retained message for the binding."""

DELIVER_BY_START_SEQUENCE = "by_start_sequence"
"""Migrated consumers resume at ``ack_floor.stream_seq + 1``."""


class ConsumerMigrationRequiredError(Exception):
    """An incompatible durable consumer needs the opt-in migration."""


@dataclass(frozen=True)
class ExplicitConsumerConfig:
    """One explicit pull-consumer configuration.

    Attributes:
        durable_name: Durable consumer name.
        filter_subject: Subject filter for the binding.
        ack_policy: Acknowledgement policy token (``explicit``).
        ack_wait: Seconds before redelivery without acknowledgement.
        max_deliver: Physical delivery budget before advisories fire.
        idle_heartbeat: Inactivity heartbeat of the pull consumer.
        replay_policy: Replay policy token (``instant``).
        deliver_policy: Deliver policy token (``all`` or
            ``by_start_sequence``).
        opt_start_seq: Resume sequence for ``by_start_sequence``.

    """

    durable_name: str
    filter_subject: str
    ack_policy: str
    ack_wait: float
    max_deliver: int
    idle_heartbeat: float
    replay_policy: str
    deliver_policy: str
    opt_start_seq: int | None


def resolve_bindings(config: NATSConfig) -> tuple[ConsumerBinding, ...]:
    """Resolve one durable binding per configured subject.

    A single subject uses the base durable name; multiple subjects use
    indexed names. No two subscriptions share one durable consumer.
    """
    subjects = list(config.subjects or ["hi.tasks.>"])
    base = config.durable_name
    return tuple(
        ConsumerBinding(
            subject=subject,
            durable=base if len(subjects) == 1 else f"{base}-{index}",
        )
        for index, subject in enumerate(subjects)
    )


def explicit_consumer_config(
    binding: ConsumerBinding,
    *,
    config: NATSConfig,
    deliver_policy: str = DELIVER_ALL,
    opt_start_seq: int | None = None,
) -> ExplicitConsumerConfig:
    """Build the explicit pull-consumer configuration for one binding.

    Raises:
        ValueError: If ``deliver_policy`` is ``by_start_sequence`` without
            ``opt_start_seq``.

    """
    if deliver_policy == DELIVER_BY_START_SEQUENCE and opt_start_seq is None:
        raise ValueError("opt_start_seq is required with deliver_policy='by_start_sequence'")
    return ExplicitConsumerConfig(
        durable_name=binding.durable,
        filter_subject=binding.subject,
        ack_policy="explicit",
        ack_wait=config.ack_wait_seconds,
        max_deliver=config.max_deliveries,
        idle_heartbeat=config.heartbeat_interval_seconds,
        replay_policy="instant",
        deliver_policy=deliver_policy,
        opt_start_seq=opt_start_seq,
    )


@dataclass(frozen=True)
class ConsumerInfoView:
    """Bounded view of an existing durable consumer.

    Attributes:
        durable_name: Durable consumer name.
        ack_policy: Live acknowledgement policy token.
        push_bound: ``True`` while a push subscriber is attached.
        ack_floor_seq: ``ack_floor.stream_seq`` (0 when unknown).
        ack_wait: Live ``ack_wait`` seconds.
        max_deliver: Live delivery budget.
        idle_heartbeat: Live inactivity heartbeat seconds.
        filter_subject: Live filter subject.
        snapshot: Complete raw configuration for transparent restore.

    """

    durable_name: str
    ack_policy: str
    push_bound: bool
    ack_floor_seq: int
    ack_wait: float
    max_deliver: int
    idle_heartbeat: float
    filter_subject: str
    snapshot: dict[str, Any]


class ConsumerAdmin(Protocol):
    """Async admin seam over JetStream consumer management."""

    async def consumer_info(self, stream: str, durable: str) -> ConsumerInfoView | None:
        """Return the live consumer view, or ``None`` when absent."""
        ...  # pragma: no cover - protocol declaration

    async def add_consumer(self, stream: str, config: ExplicitConsumerConfig) -> None:
        """Create or update one consumer from an explicit configuration."""
        ...  # pragma: no cover - protocol declaration

    async def delete_consumer(self, stream: str, durable: str) -> None:
        """Delete one durable consumer."""
        ...  # pragma: no cover - protocol declaration

    async def restore_consumer(self, stream: str, snapshot: dict[str, Any]) -> None:
        """Restore one consumer from its raw configuration snapshot."""
        ...  # pragma: no cover - protocol declaration


class ConsumerManager:
    """Validate, create, or migrate the explicit pull consumers."""

    def __init__(self, admin: ConsumerAdmin, config: NATSConfig) -> None:
        """Bind the manager to an admin seam and the NATS configuration."""
        self._admin = admin
        self._config = config

    async def ensure(self, binding: ConsumerBinding) -> None:
        """Ensure one binding has an explicit compatible consumer.

        Raises:
            ConsumerMigrationRequiredError: The consumer is incompatible
                and migration is disabled, or the consumer is still bound.

        """
        desired = explicit_consumer_config(binding, config=self._config)
        info = await self._admin.consumer_info(self._config.stream, binding.durable)
        if info is None:
            logger.info(
                "creating explicit pull consumer %s on stream %s",
                binding.durable,
                self._config.stream,
            )
            await self._admin.add_consumer(self._config.stream, desired)
            return
        if self._is_compatible(desired, info):
            if self._tunables_differ(desired, info):
                logger.info("updating tunables for compatible consumer %s", binding.durable)
                await self._admin.add_consumer(self._config.stream, desired)
            return
        await self._migrate(binding, info)

    def _is_compatible(self, desired: ExplicitConsumerConfig, info: ConsumerInfoView) -> bool:
        """Return True for an explicit consumer on the right subject."""
        return info.ack_policy == "explicit" and info.filter_subject == desired.filter_subject

    def _tunables_differ(self, desired: ExplicitConsumerConfig, info: ConsumerInfoView) -> bool:
        """Detect ack_wait/max_deliver/idle_heartbeat drift (editable)."""
        return (
            desired.ack_wait != info.ack_wait
            or desired.max_deliver != info.max_deliver
            or desired.idle_heartbeat != info.idle_heartbeat
        )

    async def _migrate(self, binding: ConsumerBinding, info: ConsumerInfoView) -> None:
        """Perform the opt-in snapshot/delete/recreate migration.

        The consumer must be unbound. Recreation resumes at
        ``ack_floor.stream_seq + 1``; a recreate failure restores the
        original configuration snapshot.

        Raises:
            ConsumerMigrationRequiredError: Migration is disabled or the
                consumer is still bound.

        """
        durable = binding.durable
        if not self._config.migrate_incompatible_consumers:
            raise ConsumerMigrationRequiredError(
                f"durable consumer {durable!r} has an incompatible acknowledgement "
                "policy; set migrate_incompatible_consumers=True for the opt-in "
                "migration"
            )
        if info.push_bound:
            raise ConsumerMigrationRequiredError(
                f"durable consumer {durable!r} is still bound; stop all subscribers first"
            )
        resume_at = info.ack_floor_seq + 1
        logger.info(
            "migrating consumer %s to explicit ack policy, resume at %d",
            durable,
            resume_at,
        )
        await self._admin.delete_consumer(self._config.stream, durable)
        recreate = explicit_consumer_config(
            binding,
            config=self._config,
            deliver_policy=DELIVER_BY_START_SEQUENCE,
            opt_start_seq=resume_at,
        )
        try:
            await self._admin.add_consumer(self._config.stream, recreate)
        except Exception:
            logger.warning("consumer recreation failed for %s; restoring the original", durable)
            await self._admin.restore_consumer(self._config.stream, info.snapshot)
            raise


class _HeartbeatMsg(Protocol):
    """Narrow message seam for the delivery heartbeat."""

    async def in_progress(self) -> None:
        """Signal that work is in progress, resetting the ack deadline."""
        ...  # pragma: no cover - protocol declaration


class _LeaseRenewer(Protocol):
    """Narrow lease-renewal seam for the delivery heartbeat."""

    async def renew_lease(self, claim: DeliveryClaim) -> bool:
        """Renew the delivery-ledger lease for one claim."""
        ...  # pragma: no cover - protocol declaration


class DeliveryHeartbeat:
    """Bounded async-context heartbeat for one in-flight delivery.

    Shields ``msg.in_progress()`` from outer cancellation and renews the
    owner-token ledger lease on every tick until ``max_duration_seconds``
    (the configured ``ack_wait``) bounds the delivery. Lease-renewal
    failures are logged; the completion CAS stays the authoritative gate.
    """

    def __init__(
        self,
        msg: _HeartbeatMsg,
        ledger: _LeaseRenewer,
        claim: DeliveryClaim,
        *,
        interval_seconds: float,
        max_duration_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the heartbeat; injectable clock seams for tests.

        Args:
            msg: Message with an ``in_progress`` signal.
            ledger: Lease renewer owning the claim.
            claim: The owned processing claim.
            interval_seconds: Heartbeat interval.
            max_duration_seconds: Bounding duration (``ack_wait``).
            sleep: Async sleep; injectable for tests.
            monotonic: Monotonic clock; injectable for tests.

        """
        self._msg = msg
        self._ledger = ledger
        self._claim = claim
        self._interval = interval_seconds
        self._max_duration = max_duration_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> DeliveryHeartbeat:
        """Start the background heartbeat task."""
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Cancel the heartbeat; shielded in-progress calls complete."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        """Tick the heartbeat until the bounding duration elapses."""
        start = self._monotonic()
        while self._monotonic() - start < self._max_duration:
            await asyncio.shield(self._msg.in_progress())
            try:
                if not await self._ledger.renew_lease(self._claim):
                    logger.warning(
                        "delivery-ledger lease renewal was refused; the "
                        "completion CAS remains the authoritative gate"
                    )
            except Exception as exc:
                logger.warning("delivery-ledger lease renewal failed: %s", exc)
            await self._sleep(self._interval)
