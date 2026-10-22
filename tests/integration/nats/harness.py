"""Shared async harness helpers for the real-broker NATS integration tests (#2395).

These helpers run nats-py client operations from synchronous pytest tests.
The subscriber thread under test always runs with its production
``admin_factory`` default, so the real nats-py adapters are exercised end
to end against the real broker.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.delivery_ledger import ledger_subject
from hephaestus.nats.failure_models import EventIdentity, failure_id_for_event
from hephaestus.nats.failure_store import failure_subject


#: Small timing profile so real-broker retries finish quickly.
def subscriber_config(
    url: str,
    stream: str,
    subjects: list[str],
    *,
    durable: str = "hephaestus-subscriber",
    enabled: bool = True,
    **overrides: Any,
) -> NATSConfig:
    """Build a bounded subscriber config for one real-broker binding.

    The defaults satisfy every cross-field validation bound while keeping
    redelivery fast enough for integration runs.
    """
    config: dict[str, Any] = {
        "enabled": enabled,
        "url": url,
        "tls": False,
        "stream": stream,
        "subjects": subjects,
        "durable_name": durable,
        "ack_wait_seconds": 3.0,
        "heartbeat_interval_seconds": 1.0,
        "ledger_lease_seconds": 2.0,
        "redelivery_backoff_seconds": (0.2, 0.5),
        "max_processing_attempts": 3,
        "max_deliveries": 6,
    }
    config.update(overrides)
    return NATSConfig(**config)


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run one coroutine to completion from a synchronous test."""
    return asyncio.run(coro)


def wait_for(predicate: Callable[[], bool], *, timeout: float = 20.0) -> bool:
    """Poll *predicate* until it holds or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


async def connect(url: str) -> Any:
    """Open one real nats-py connection to the test broker."""
    import nats

    return await nats.connect(url)


async def create_source_stream(nc: Any, stream: str, subjects: list[str]) -> None:
    """Create the source stream for one test on an open connection."""
    from nats.js.api import RetentionPolicy, StreamConfig

    await nc.jetstream().add_stream(
        StreamConfig(
            name=stream,
            subjects=subjects,
            retention=RetentionPolicy.LIMITS,
        )
    )


async def publish(nc: Any, subject: str, payload: bytes) -> Any:
    """Publish one payload to the source stream and return the PubAck."""
    return await nc.jetstream().publish(subject, payload)


async def wait_last_msg(nc: Any, stream: str, subject: str, timeout: float = 20.0) -> Any | None:
    """Poll ``get_last_msg`` until the subject has a record or time runs out."""
    from nats.js.errors import APIError

    js = nc.jetstream()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return await js.get_last_msg(stream, subject)
        except APIError:
            await asyncio.sleep(0.2)
    return None


def event_failure_subject(stream: str, durable: str, stream_sequence: int) -> str:
    """Return the deterministic dead-letter subject for one source message."""
    event_id = EventIdentity(
        stream=stream,
        durable=durable,
        stream_sequence=stream_sequence,
    ).event_id
    return failure_subject(failure_id_for_event(event_id))


def event_ledger_subject(stream: str, durable: str, stream_sequence: int) -> str:
    """Return the delivery-ledger subject for one source message."""
    event_id = EventIdentity(
        stream=stream,
        durable=durable,
        stream_sequence=stream_sequence,
    ).event_id
    return ledger_subject(event_id)
