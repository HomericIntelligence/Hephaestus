"""NATS event routing and handler registration.

Provides :class:`EventRouter` that dispatches :class:`~hephaestus.nats.events.NATSEvent`
messages to verb-specific handler callbacks.

Usage::

    from hephaestus.nats.handlers import EventRouter

    router = EventRouter()
    router.register("created", lambda event: print("created:", event.subject))
    router.dispatch(event)
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from hephaestus.nats.events import NATSEvent, parse_subject

logger = logging.getLogger(__name__)


class EventRouter:
    """Dispatch NATS events to registered verb-specific handlers.

    Example::

        router = EventRouter()
        router.register("created", handle_task_created)
        router.dispatch(event)

    """

    def __init__(self) -> None:
        """Initialize the router with an empty handler registry."""
        self._handlers: dict[str, Callable[[NATSEvent], None]] = {}

    def register(self, verb: str, handler: Callable[[NATSEvent], None]) -> None:
        """Register a handler for a specific verb.

        Args:
            verb: The action verb to handle (e.g., ``created``).
            handler: Callback invoked when an event with this verb arrives.

        """
        self._handlers[verb] = handler

    def dispatch(self, event: NATSEvent) -> None:
        """Parse the event subject and route to the registered handler.

        If the subject cannot be parsed or no handler is registered for the
        verb, a warning is logged.  Handler exceptions are caught and logged
        so that one failing handler does not crash the router.

        Args:
            event: The incoming NATS event.

        """
        try:
            parts = parse_subject(event.subject)
        except ValueError:
            logger.warning("Unparseable subject: %s", event.subject)
            return

        handler = self._handlers.get(parts.verb)
        if handler is None:
            logger.debug("No handler registered for verb: %s", parts.verb)
            return

        try:
            handler(event)
        except Exception:
            logger.exception(
                "Handler for verb %r raised an exception on event seq=%d",
                parts.verb,
                event.sequence,
            )
