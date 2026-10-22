"""Generic NATS JetStream subscriber infrastructure.

Provides a configurable, pluggable subscriber that connects to a NATS server,
subscribes to JetStream subjects, and dispatches messages to caller-supplied
handler callbacks.  No subject hierarchy or project names are hardcoded.

Requires the optional ``nats`` extra::

    pip install 'HomericIntelligence-Hephaestus[nats]'

Usage::

    from hephaestus.nats import NATSConfig, NATSEvent, EventRouter, NATSSubscriberThread

    config = NATSConfig(
        enabled=True,
        url="tls://nats.example.com:4222",
        subjects=["my.subject.>"],
    )
    router = EventRouter()
    router.register("created", lambda event: print(event.subject))

    thread = NATSSubscriberThread(config=config, handler=router.dispatch)
    thread.start()
    # ...
    thread.stop()
"""

from hephaestus.nats.config import NATSConfig as NATSConfig, load_nats_config as load_nats_config
from hephaestus.nats.events import (
    NATSEvent as NATSEvent,
    SubjectParts as SubjectParts,
    parse_subject as parse_subject,
)
from hephaestus.nats.failure_models import FailureEnvelope as FailureEnvelope
from hephaestus.nats.handlers import EventRouter as EventRouter
from hephaestus.nats.replay import (
    FailureReplayer as FailureReplayer,
    ReplayOutcome as ReplayOutcome,
)
from hephaestus.nats.subscriber import (
    DEFAULT_JOIN_TIMEOUT as DEFAULT_JOIN_TIMEOUT,
    NATSSubscriberThread as NATSSubscriberThread,
    SubscriberState as SubscriberState,
)

__all__ = [
    "DEFAULT_JOIN_TIMEOUT",
    "EventRouter",
    "FailureEnvelope",
    "FailureReplayer",
    "NATSConfig",
    "NATSEvent",
    "NATSSubscriberThread",
    "ReplayOutcome",
    "SubjectParts",
    "SubscriberState",
    "load_nats_config",
    "parse_subject",
]
