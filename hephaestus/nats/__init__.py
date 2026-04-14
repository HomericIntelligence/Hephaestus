"""Generic NATS JetStream subscriber infrastructure.

Provides a configurable, pluggable subscriber that connects to a NATS server,
subscribes to JetStream subjects, and dispatches messages to caller-supplied
handler callbacks.  No subject hierarchy or project names are hardcoded.

Requires the optional ``nats`` extra::

    pip install 'HomericIntelligence-Hephaestus[nats]'

Usage::

    from hephaestus.nats import NATSConfig, NATSEvent, EventRouter, NATSSubscriberThread

    config = NATSConfig(enabled=True, url="nats://localhost:4222", subjects=["my.subject.>"])
    router = EventRouter()
    router.register("created", lambda event: print(event.subject))

    thread = NATSSubscriberThread(config=config, handler=router.dispatch)
    thread.start()
    # ...
    thread.stop()
"""

from hephaestus.nats.config import NATSConfig as NATSConfig
from hephaestus.nats.config import load_nats_config as load_nats_config
from hephaestus.nats.events import NATSEvent as NATSEvent
from hephaestus.nats.events import SubjectParts as SubjectParts
from hephaestus.nats.events import parse_subject as parse_subject
from hephaestus.nats.handlers import EventRouter as EventRouter
from hephaestus.nats.handlers import create_default_router as create_default_router
from hephaestus.nats.subscriber import NATSSubscriberThread as NATSSubscriberThread

__all__ = [
    "EventRouter",
    "NATSConfig",
    "NATSEvent",
    "NATSSubscriberThread",
    "SubjectParts",
    "create_default_router",
    "load_nats_config",
    "parse_subject",
]
