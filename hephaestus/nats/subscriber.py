"""NATS JetStream subscriber thread.

Provides :class:`NATSSubscriberThread`, a daemon thread that connects to
NATS, subscribes to JetStream subjects via a durable consumer, and dispatches
incoming messages to a caller-supplied handler callback.

The thread runs an isolated asyncio event loop internally and reconnects with
exponential backoff on connection errors.

Requires the optional ``nats`` extra::

    pip install 'HomericIntelligence-Hephaestus[nats]'

Usage::

    from hephaestus.nats import NATSConfig, NATSSubscriberThread

    config = NATSConfig(enabled=True, url="nats://localhost:4222", subjects=["my.>"])
    thread = NATSSubscriberThread(config=config, handler=lambda event: print(event))
    thread.start()
    thread.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import warnings
from collections.abc import Callable
from typing import Any

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.events import NATSEvent

# Suppress nats-py DeprecationWarning for asyncio.iscoroutinefunction (Python 3.11+)
warnings.filterwarnings(
    "ignore",
    message=".*asyncio.iscoroutinefunction.*",
    category=DeprecationWarning,
    module="nats",
)

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0
_BACKOFF_MULTIPLIER = 2.0


class NATSSubscriberThread(threading.Thread):
    """Daemon thread that subscribes to NATS JetStream and dispatches events.

    The thread creates an isolated asyncio event loop internally.  The NATS
    connection and JetStream subscription live entirely within that loop.

    Example::

        from hephaestus.nats.config import NATSConfig
        subscriber = NATSSubscriberThread(
            config=NATSConfig(enabled=True, subjects=["my.subject.>"]),
            handler=lambda event: print(event.subject),
        )
        subscriber.start()
        # ... do work ...
        subscriber.stop()

    """

    def __init__(
        self,
        config: NATSConfig,
        handler: Callable[[NATSEvent], None],
    ) -> None:
        """Initialize the subscriber thread.

        Args:
            config: NATS connection configuration.
            handler: Callback invoked for each received :class:`~hephaestus.nats.events.NATSEvent`.

        """
        super().__init__(daemon=True, name="NATSSubscriberThread")
        self._config = config
        self._handler = handler
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Run the subscriber loop with exponential-backoff reconnection."""
        logger.info(
            "NATSSubscriberThread started (url=%s, stream=%s, durable=%s)",
            self._config.url,
            self._config.stream,
            self._config.durable_name,
        )

        backoff = _INITIAL_BACKOFF_SECONDS

        while not self._stop_event.is_set():
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._subscribe_loop())
                finally:
                    loop.close()
                backoff = _INITIAL_BACKOFF_SECONDS
            except Exception:
                if self._stop_event.is_set():
                    break
                logger.exception(
                    "NATS connection error, retrying in %.1fs",
                    backoff,
                )
                self._stop_event.wait(timeout=backoff)
                backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_SECONDS)

        logger.info("NATSSubscriberThread stopped")

    async def _subscribe_loop(self) -> None:
        """Connect to NATS JetStream and process messages until stop is requested."""
        try:
            import nats as nats_client
        except ImportError:
            logger.error(
                "nats-py is not installed. "
                "Install with: pip install 'HomericIntelligence-Hephaestus[nats]'"
            )
            self._stop_event.set()
            return

        nc = await nats_client.connect(self._config.url)
        try:
            js = nc.jetstream()
            subjects = self._config.subjects or ["hi.tasks.>"]
            subscriptions = []
            for i, subject in enumerate(subjects):
                durable = (
                    self._config.durable_name
                    if len(subjects) == 1
                    else f"{self._config.durable_name}-{i}"
                )
                sub = await js.subscribe(
                    subject=subject,
                    durable=durable,
                    stream=self._config.stream,
                    deliver_policy=self._config.deliver_policy,
                )
                subscriptions.append(sub)

            logger.info(
                "Subscribed to %d NATS JetStream subject(s) on stream=%s: %s",
                len(subscriptions),
                self._config.stream,
                subjects,
            )

            while not self._stop_event.is_set():
                for sub in subscriptions:
                    try:
                        msg = await asyncio.wait_for(
                            sub.next_msg(timeout=0.5),
                            timeout=1.0,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        continue

                    try:
                        data: dict[str, Any] = json.loads(msg.data.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        logger.warning(
                            "Failed to decode message on %s (seq=%d)",
                            msg.subject,
                            msg.metadata.sequence.stream if msg.metadata else 0,
                        )
                        await msg.ack()
                        continue

                    event = NATSEvent(
                        subject=msg.subject,
                        data=data,
                        timestamp=(msg.headers.get("Nats-Time-Stamp", "") if msg.headers else ""),
                        sequence=msg.metadata.sequence.stream if msg.metadata else 0,
                    )

                    self._handler(event)
                    await msg.ack()

        finally:
            await nc.drain()

    def stop(self) -> None:
        """Signal the subscriber to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=5.0)
