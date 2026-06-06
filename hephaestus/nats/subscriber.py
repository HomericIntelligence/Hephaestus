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
    # Check health at any time:
    print(thread.health_dict())
    thread.stop()
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import threading
import time
import warnings
from collections.abc import Callable
from typing import Any

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.events import NATSEvent

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0
_BACKOFF_MULTIPLIER = 2.0

DEFAULT_JOIN_TIMEOUT: float = 5.0
"""Default join timeout (seconds) for :meth:`NATSSubscriberThread.stop`."""


class SubscriberState(enum.Enum):
    """Lifecycle states for :class:`NATSSubscriberThread`.

    Transitions::

        INITIALIZING → CONNECTED    (successful NATS connect)
        CONNECTED    → DISCONNECTED (connection error / drain)
        DISCONNECTED → CONNECTED    (successful reconnect)
        CONNECTED    → STOPPING     (stop() called while connected)
        DISCONNECTED → STOPPING     (stop() called while in backoff)
        STOPPING     → STOPPED      (thread join completes)
        any          → ERROR        (unhandled exception propagates out of run())

    """

    INITIALIZING = "initializing"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class NATSSubscriberThread(threading.Thread):
    """Daemon thread that subscribes to NATS JetStream and dispatches events.

    The thread creates an isolated asyncio event loop internally.  The NATS
    connection and JetStream subscription live entirely within that loop.

    Health observability
    --------------------
    Three read-only attributes and one method expose internal state for
    monitoring and /health endpoints:

    - :attr:`state` — current :class:`SubscriberState` (enum, thread-safe).
    - :attr:`last_error` — last exception or ``None`` (thread-safe).
    - :attr:`last_message_at` — Unix timestamp of the most recent successfully
      dispatched message, or ``None`` if no message has been processed yet.
    - :meth:`health_dict()` — JSON-serialisable snapshot of the above plus the
      configured URL, stream, and uptime approximation.

    Configurable stop timeout
    -------------------------
    Pass ``join_timeout`` to the constructor to change the default; or supply a
    per-call override via ``stop(timeout=…)``.

    Example::

        from hephaestus.nats.config import NATSConfig
        subscriber = NATSSubscriberThread(
            config=NATSConfig(enabled=True, subjects=["my.subject.>"]),
            handler=lambda event: print(event.subject),
            join_timeout=10.0,
        )
        subscriber.start()
        # ... do work ...
        print(subscriber.state)          # SubscriberState.CONNECTED
        print(subscriber.health_dict())  # {'state': 'connected', ...}
        subscriber.stop(timeout=2.0)     # override per-call

    """

    def __init__(
        self,
        config: NATSConfig,
        handler: Callable[[NATSEvent], None],
        join_timeout: float = DEFAULT_JOIN_TIMEOUT,
    ) -> None:
        """Initialize the subscriber thread.

        Args:
            config: NATS connection configuration.
            handler: Callback invoked for each received
                :class:`~hephaestus.nats.events.NATSEvent`.
            join_timeout: Default timeout in seconds for the :meth:`stop` call's
                internal ``thread.join()``.  Defaults to
                :data:`DEFAULT_JOIN_TIMEOUT` (5.0 s).  May be overridden per
                call via ``stop(timeout=…)``.

        """
        super().__init__(daemon=True, name="NATSSubscriberThread")
        self._config = config
        self._handler = handler
        self._join_timeout = join_timeout
        self._stop_event = threading.Event()

        # --- health / observability state (guarded by _state_lock) ---
        self._state_lock = threading.Lock()
        self._state: SubscriberState = SubscriberState.INITIALIZING
        self._last_error: BaseException | None = None
        self._last_message_at: float | None = None
        self._started_at: float = time.monotonic()

    # ------------------------------------------------------------------
    # Public health surface
    # ------------------------------------------------------------------

    @property
    def state(self) -> SubscriberState:
        """Current lifecycle state (thread-safe, read-only)."""
        with self._state_lock:
            return self._state

    @property
    def last_error(self) -> BaseException | None:
        """Most recent exception observed, or ``None`` (thread-safe, read-only)."""
        with self._state_lock:
            return self._last_error

    @property
    def last_message_at(self) -> float | None:
        """Unix timestamp of the last successfully dispatched message, or ``None``.

        Updated after :attr:`handler` returns without raising.  Thread-safe.
        """
        with self._state_lock:
            return self._last_message_at

    def health_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable health snapshot.

        The returned dict is suitable for embedding in an HTTP ``/health``
        response.  All values are primitive types; no :class:`enum.Enum`
        instances are included.

        Returns:
            A dict with the following keys:

            - ``"state"`` (:class:`str`): current state name, e.g.
              ``"connected"``.
            - ``"last_error"`` (:class:`str` or ``None``): string
              representation of the last exception, or ``None``.
            - ``"last_message_at"`` (:class:`float` or ``None``): Unix
              timestamp of last dispatched message, or ``None``.
            - ``"url"`` (:class:`str`): configured NATS URL.
            - ``"stream"`` (:class:`str`): configured stream name.
            - ``"uptime_seconds"`` (:class:`float`): seconds since thread was
              constructed.

        """
        with self._state_lock:
            state_name = self._state.value
            error_str = str(self._last_error) if self._last_error is not None else None
            last_msg = self._last_message_at
        return {
            "state": state_name,
            "last_error": error_str,
            "last_message_at": last_msg,
            "url": self._config.url,
            "stream": self._config.stream,
            "uptime_seconds": time.monotonic() - self._started_at,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_state(self, new_state: SubscriberState) -> None:
        """Transition to *new_state* under the state lock."""
        with self._state_lock:
            self._state = new_state

    def _record_error(self, exc: BaseException) -> None:
        """Record *exc* as the latest error and transition to DISCONNECTED."""
        with self._state_lock:
            self._last_error = exc
            self._state = SubscriberState.DISCONNECTED

    def _record_message(self) -> None:
        """Update ``last_message_at`` to the current time."""
        with self._state_lock:
            self._last_message_at = time.time()

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the subscriber loop with exponential-backoff reconnection."""
        logger.info(
            "NATSSubscriberThread started (url=%s, stream=%s, durable=%s)",
            self._config.url,
            self._config.stream,
            self._config.durable_name,
        )

        backoff = _INITIAL_BACKOFF_SECONDS

        try:
            while not self._stop_event.is_set():
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(self._subscribe_loop())
                    finally:
                        loop.close()
                    backoff = _INITIAL_BACKOFF_SECONDS
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    self._record_error(exc)
                    logger.exception(
                        "NATS connection error, retrying in %.1fs",
                        backoff,
                    )
                    self._stop_event.wait(timeout=backoff)
                    backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_SECONDS)
        except Exception as exc:
            with self._state_lock:
                self._last_error = exc
                self._state = SubscriberState.ERROR
            logger.exception("NATSSubscriberThread terminated with unhandled error")
            return

        self._set_state(SubscriberState.STOPPED)
        logger.info("NATSSubscriberThread stopped")

    async def _subscribe_loop(self) -> None:
        """Connect to NATS JetStream and process messages until stop is requested."""
        try:
            # nats-py calls asyncio.iscoroutinefunction, which raises a
            # DeprecationWarning on Python 3.12+. Scope the suppression to just
            # the import so we do not mutate the process-wide warnings filter
            # chain (issue #798).
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=".*asyncio.iscoroutinefunction.*",
                    category=DeprecationWarning,
                    module="nats",
                )
                import nats as nats_client
                from nats.js.api import DeliverPolicy
        except ImportError:
            logger.error(
                "nats-py is not installed. "
                "Install with: pip install 'HomericIntelligence-Hephaestus[nats]'"
            )
            self._stop_event.set()
            return

        nc = await nats_client.connect(self._config.url)
        self._set_state(SubscriberState.CONNECTED)
        try:
            js = nc.jetstream()
            subjects = self._config.subjects or ["hi.tasks.>"]
            # The config carries deliver_policy as a plain string (e.g. "new")
            # for YAML/env ergonomics; js.subscribe expects the DeliverPolicy
            # enum. DeliverPolicy is a str-valued enum whose values match the
            # config strings, so construct it directly.
            deliver_policy = DeliverPolicy(self._config.deliver_policy)
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
                    deliver_policy=deliver_policy,
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

                    try:
                        self._handler(event)
                    except Exception as exc:
                        with self._state_lock:
                            self._last_error = exc
                        logger.exception(
                            "Handler raised on subject %s (seq=%d)",
                            event.subject,
                            event.sequence,
                        )
                    else:
                        self._record_message()
                    await msg.ack()

        finally:
            self._set_state(SubscriberState.DISCONNECTED)
            await nc.drain()

    def stop(self, timeout: float | None = None) -> bool:
        """Signal the subscriber to stop and wait for the thread to finish.

        Args:
            timeout: How long to wait (seconds) for the thread to join.
                When ``None`` (the default), uses the ``join_timeout``
                value supplied to the constructor (default 5.0 s).

        Returns:
            ``True`` if the thread joined cleanly within the timeout (or had
            already finished); ``False`` if the join timed out and the thread
            is still running. A ``False`` return also logs a warning so the
            condition is visible in logs even when the caller ignores it.

        """
        effective_timeout = self._join_timeout if timeout is None else timeout
        self._set_state(SubscriberState.STOPPING)
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=effective_timeout)
            if self.is_alive():
                logger.warning(
                    "NATS subscriber thread did not stop within %.1fs — still running",
                    effective_timeout,
                )
                return False
        return True
