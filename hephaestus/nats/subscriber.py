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

    config = NATSConfig(enabled=True, url="tls://nats.example.com:4222", subjects=["my.>"])
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
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit, urlunsplit

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.events import NATSEvent
from hephaestus.resilience import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerState,
)

if TYPE_CHECKING:
    from hephaestus.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)

DEFAULT_JOIN_TIMEOUT: float = 5.0
"""Default join timeout (seconds) for :meth:`NATSSubscriberThread.stop`."""

NATS_CIRCUIT_BREAKER_NAME = "nats-subscriber"
"""Circuit breaker name used by :class:`NATSSubscriberThread`."""

NATS_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
"""Consecutive connection failures before the subscriber enters ``ERROR``.

The value matches :class:`hephaestus.resilience.CircuitBreaker`'s default so
the NATS subscriber adopts the shared resilience behavior without adding a
NATS-specific tuning surface.
"""

_ErrorKind = Literal[
    "connection_error",
    "handler_error",
    "circuit_breaker_open",
    "terminal_error",
    "shutdown_timeout",
]


def _diagnostic_nats_url(url: str) -> str:
    """Return a credential-free NATS URL suitable for health and logs."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid-nats-url>"

    if not parsed.scheme or hostname is None:
        return "<invalid-nats-url>"

    display_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = display_host if port is None else f"{display_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


class SubscriberState(enum.Enum):
    """Lifecycle states for :class:`NATSSubscriberThread`.

    Transitions::

        INITIALIZING → CONNECTED    (successful NATS connect)
        CONNECTED    → DISCONNECTED (connection error / drain)
        DISCONNECTED → CONNECTED    (successful reconnect)
        CONNECTED    → STOPPING     (stop() called while connected)
        DISCONNECTED → STOPPING     (stop() called while in backoff)
        STOPPING     → STOPPED      (thread join completes)
        any          → ERROR        (unhandled exception, open circuit breaker,
                                     or shutdown join timeout)

    """

    INITIALIZING = "initializing"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


_NATS_ERROR_KIND_LABELS = frozenset({"connection", "terminal", "handler", "decode"})
_NATS_SUBSCRIBER_STATE_LABELS = frozenset(state.value for state in SubscriberState)
_NATS_BREAKER_STATE_LABELS = frozenset(state.value for state in CircuitBreakerState)


class NATSSubscriberThread(threading.Thread):
    """Daemon thread that subscribes to NATS JetStream and dispatches events.

    The thread creates an isolated asyncio event loop internally.  The NATS
    connection and JetStream subscription live entirely within that loop.
    Reconnection attempts are guarded by a circuit breaker; sustained
    connection failures transition the subscriber to :attr:`SubscriberState.ERROR`.

    Health observability
    --------------------
    Three read-only attributes and one method expose internal state for
    monitoring and /health endpoints:

    - :attr:`state` — current :class:`SubscriberState` (enum, thread-safe).
    - :attr:`last_error` — last raw exception or ``None`` (thread-safe).
    - :attr:`last_message_at` — Unix timestamp of the most recent successfully
      dispatched message, or ``None`` if no message has been processed yet.
    - :meth:`health_dict()` — JSON-serialisable snapshot of the above plus the
      configured URL, stream, circuit-breaker state, and uptime approximation.

    Delivery semantics
    ------------------
    This subscriber provides best-effort, *at-most-once* handling for
    non-critical events. Malformed UTF-8 or JSON payloads are warning-logged
    and acked without invoking the handler. Handler exceptions are retained in
    the in-process :attr:`last_error` property, classified in health responses,
    and logged without exception text or tracebacks. They are acked without
    updating :attr:`last_message_at`.

    There is no built-in retry, replay store, or DLQ; neither failure class is
    durably retained after acknowledgement. Workflows requiring durable or
    auditable processing must use a consumer that persists failures before
    acknowledging them. Acking failures here prevents poison-message
    redelivery loops.

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
        *,
        metrics_registry: MetricsRegistry | None = None,
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
            metrics_registry: Optional caller-owned registry that receives
                subscriber state, message, and error metrics. The subscriber
                does not create a global registry or start an HTTP server.

        """
        super().__init__(daemon=True, name="NATSSubscriberThread")
        self._config = config
        self._handler = handler
        self._join_timeout = join_timeout
        self._metrics_registry = metrics_registry
        self._stop_event = threading.Event()
        self._circuit_breaker = CircuitBreaker(
            NATS_CIRCUIT_BREAKER_NAME,
            failure_threshold=NATS_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout=self._config.max_backoff_seconds,
        )

        # --- health / observability state (guarded by _state_lock) ---
        self._state_lock = threading.Lock()
        self._state: SubscriberState = SubscriberState.INITIALIZING
        self._last_error: BaseException | None = None
        self._last_error_kind: _ErrorKind | None = None
        self._last_message_at: float | None = None
        self._started_at: float = time.monotonic()
        self._emit_metrics()

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
        """Most recent raw exception, or ``None`` (thread-safe, read-only).

        This in-process property intentionally retains the exception object for
        callers that need detailed diagnostics. Use :meth:`health_dict` for
        externally visible, classified diagnostics.
        """
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
            - ``"last_error"`` (:class:`str` or ``None``): bounded failure
              category, or ``None``. Exception text is never published.
            - ``"last_message_at"`` (:class:`float` or ``None``): Unix
              timestamp of last dispatched message, or ``None``.
            - ``"url"`` (:class:`str`): diagnostic NATS URL with userinfo,
              query parameters, and fragments removed.
            - ``"stream"`` (:class:`str`): configured stream name.
            - ``"circuit_breaker_state"`` (:class:`str`): current circuit
              breaker state, e.g. ``"closed"`` or ``"open"``.
            - ``"uptime_seconds"`` (:class:`float`): seconds since thread was
              constructed.

        """
        with self._state_lock:
            state_name = self._state.value
            error_kind = self._last_error_kind
            last_msg = self._last_message_at
        return {
            "state": state_name,
            "last_error": error_kind,
            "last_message_at": last_msg,
            "url": _diagnostic_nats_url(self._config.url),
            "stream": self._config.stream,
            "circuit_breaker_state": self._circuit_breaker.state.value,
            "uptime_seconds": time.monotonic() - self._started_at,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_state(self, new_state: SubscriberState) -> None:
        """Transition to *new_state* under the state lock."""
        with self._state_lock:
            self._state = new_state
        self._emit_metrics()

    def _record_error(self, exc: BaseException) -> None:
        """Record a connection failure and transition to DISCONNECTED."""
        with self._state_lock:
            self._last_error = exc
            self._last_error_kind = "connection_error"
            self._state = SubscriberState.DISCONNECTED
        self._increment_error_metric("connection")
        self._emit_metrics()

    def _record_terminal_error(
        self,
        exc: BaseException,
        *,
        kind: _ErrorKind = "terminal_error",
    ) -> None:
        """Record a classified terminal failure and transition to ERROR."""
        with self._state_lock:
            self._last_error = exc
            self._last_error_kind = kind
            self._state = SubscriberState.ERROR
        self._increment_error_metric("terminal")
        self._emit_metrics()

    def _record_message(self) -> None:
        """Update ``last_message_at`` to the current time."""
        with self._state_lock:
            self._last_message_at = time.time()
        registry = self._metrics_registry
        if registry is not None:
            registry.counter(
                "hephaestus_nats_subscriber_messages_total",
                "NATS messages dispatched successfully by this subscriber.",
                allowed_labels={},
                series_cap=1,
            ).inc()
        self._emit_metrics()

    def _record_handler_error(self, exc: BaseException) -> None:
        """Record a handler failure while preserving at-most-once delivery."""
        with self._state_lock:
            self._last_error = exc
            self._last_error_kind = "handler_error"
        self._increment_error_metric("handler")
        self._emit_metrics()

    def _record_decode_error(self) -> None:
        """Record a malformed incoming NATS message without exposing its body."""
        self._increment_error_metric("decode")
        self._emit_metrics()

    def _increment_error_metric(self, kind: str) -> None:
        """Increment a bounded error-kind counter when metrics are injected."""
        registry = self._metrics_registry
        if registry is not None:
            registry.counter(
                "hephaestus_nats_subscriber_errors_total",
                "NATS subscriber errors by bounded lifecycle kind.",
                allowed_labels={"kind": _NATS_ERROR_KIND_LABELS},
                series_cap=len(_NATS_ERROR_KIND_LABELS),
            ).inc(labels={"kind": kind})

    def _emit_metrics(self) -> None:
        """Update caller-owned metrics from the thread-safe lifecycle snapshot."""
        registry = self._metrics_registry
        if registry is None:
            return
        with self._state_lock:
            state = self._state
            last_message_at = self._last_message_at
        state_gauge = registry.gauge(
            "hephaestus_nats_subscriber_state",
            "NATS subscriber lifecycle state (one active state has value 1).",
            allowed_labels={"state": _NATS_SUBSCRIBER_STATE_LABELS},
            series_cap=len(_NATS_SUBSCRIBER_STATE_LABELS),
        )
        for subscriber_state in SubscriberState:
            state_gauge.set(
                int(subscriber_state is state), labels={"state": subscriber_state.value}
            )
        breaker_state = self._circuit_breaker.state
        breaker_gauge = registry.gauge(
            "hephaestus_nats_subscriber_circuit_breaker_state",
            "NATS subscriber circuit-breaker state (one active state has value 1).",
            allowed_labels={"state": _NATS_BREAKER_STATE_LABELS},
            series_cap=len(_NATS_BREAKER_STATE_LABELS),
        )
        for breaker_state_candidate in CircuitBreakerState:
            breaker_gauge.set(
                int(breaker_state_candidate is breaker_state),
                labels={"state": breaker_state_candidate.value},
            )
        registry.gauge(
            "hephaestus_nats_subscriber_last_message_timestamp_seconds",
            "Unix timestamp of the last successfully dispatched NATS message (zero if none).",
            allowed_labels={},
            series_cap=1,
        ).set(last_message_at or 0.0)

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the subscriber loop with exponential-backoff reconnection."""
        logger.info(
            "NATSSubscriberThread started (url=%s, stream=%s, durable=%s)",
            _diagnostic_nats_url(self._config.url),
            self._config.stream,
            self._config.durable_name,
        )

        backoff = self._config.initial_backoff_seconds

        try:
            while not self._stop_event.is_set():
                try:
                    loop = asyncio.new_event_loop()
                    try:

                        def _run_subscribe_once(
                            event_loop: asyncio.AbstractEventLoop = loop,
                        ) -> None:
                            event_loop.run_until_complete(self._subscribe_loop())

                        self._circuit_breaker.call(_run_subscribe_once)
                    except CircuitBreakerOpenError as exc:
                        self._record_terminal_error(exc, kind="circuit_breaker_open")
                        logger.error(
                            "NATS circuit breaker is open; subscriber entering ERROR "
                            "state (kind=circuit_breaker_open)"
                        )
                        return
                    finally:
                        loop.close()
                    backoff = self._config.initial_backoff_seconds
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    self._record_error(exc)
                    if self._circuit_breaker.state is CircuitBreakerState.OPEN:
                        self._record_terminal_error(exc, kind="connection_error")
                        logger.error(
                            "NATS circuit breaker opened after sustained connection "
                            "failures; subscriber entering ERROR state "
                            "(kind=connection_error)"
                        )
                        return
                    logger.error(
                        "NATS connection error, retrying in %.1fs (kind=connection_error)",
                        backoff,
                    )
                    self._stop_event.wait(timeout=backoff)
                    backoff = min(
                        backoff * self._config.backoff_multiplier,
                        self._config.max_backoff_seconds,
                    )
        except Exception as exc:
            self._record_terminal_error(exc)
            logger.error(
                "NATSSubscriberThread terminated with unhandled error (kind=terminal_error)"
            )
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

        nc = await nats_client.connect(self._config.url, **self._config.connect_options())
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
                        msg = await sub.next_msg(timeout=0.5)
                    except TimeoutError:
                        # nats-py's next_msg raises nats.errors.TimeoutError (a
                        # subclass of the builtin TimeoutError) on its own timeout,
                        # but the underlying asyncio.wait_for can surface a bare
                        # asyncio.TimeoutError too. On Python 3.10 these are TWO
                        # DISTINCT classes (unified only in 3.11), so a single-name
                        # except would let one alias escape and crash the poll loop
                        # on the project's minimum version. Catch both to stay
                        # correct across 3.10-3.13 (#753).
                        continue

                    try:
                        data: dict[str, Any] = json.loads(msg.data.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._record_decode_error()
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
                        self._record_handler_error(exc)
                        logger.error(
                            "Handler raised on subject %s (seq=%d, kind=handler_error)",
                            event.subject,
                            event.sequence,
                        )
                    else:
                        self._record_message()
                    # Ack unconditionally — even when the handler raised. This is
                    # AT-MOST-ONCE delivery by design: a handler that fails on a
                    # given message will fail again on redelivery, so re-queuing it
                    # would wedge the poll loop on a poison message forever. The
                    # failure is NOT lost — it is recorded in `last_error` and
                    # surfaced via the classified health/log diagnostics above. Do
                    # NOT move this ack into
                    # the `else:` branch (that switches to at-least-once and
                    # reintroduces poison-message redelivery loops). (#1551)
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
            already finished). ``False`` if the thread remains alive after the
            timeout; in that case the subscriber enters ``ERROR`` state and
            exposes the raw :class:`TimeoutError` through ``last_error`` and
            the ``shutdown_timeout`` category through :meth:`health_dict`.

        """
        effective_timeout = self._join_timeout if timeout is None else timeout
        self._set_state(SubscriberState.STOPPING)
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=effective_timeout)
            if self.is_alive():
                error = TimeoutError(
                    "NATS subscriber thread did not stop within "
                    f"{effective_timeout:g}s — still running"
                )
                self._record_terminal_error(error, kind="shutdown_timeout")
                logger.error(
                    "NATS subscriber shutdown timed out after %gs (kind=shutdown_timeout)",
                    effective_timeout,
                )
                return False
        return True
