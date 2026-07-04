"""NATS publish helper for mesh workers (ADR-013).

``hephaestus.nats`` is intentionally subscribe-only; publishing is a mesh
concern and lives here. Auth follows Odysseus ADR-008/009: token via
``NATS_CLIENT_TOKEN``, CA-verified TLS via ``NATS_CA_FILE``.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from typing import Any

logger = logging.getLogger(__name__)


def connect_kwargs(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Build nats-py connect kwargs from the ADR-008/009 environment."""
    env = os.environ if environ is None else environ
    kwargs: dict[str, Any] = {}
    token = env.get("NATS_CLIENT_TOKEN")
    if token:
        kwargs["token"] = token
    ca_file = env.get("NATS_CA_FILE")
    if ca_file:
        kwargs["tls"] = ssl.create_default_context(cafile=ca_file)
    return kwargs


class MeshPublisher:
    """Thin async wrapper around one nats-py connection.

    The connection is shared with the worker's JetStream subscription; the
    publisher only adds JSON encoding and flush-on-publish so state events
    are on the wire before the message is acked.
    """

    def __init__(self, url: str, *, connect: Any | None = None) -> None:
        """Store the server *url*; *connect* overrides ``nats.connect`` in tests."""
        self._url = url
        self._connect = connect
        self.nc: Any | None = None

    async def connect(self) -> Any:
        """Connect (idempotent) and return the underlying nats connection."""
        if self.nc is None or self.nc.is_closed:
            connect = self._connect
            if connect is None:  # pragma: no cover - exercised in integration
                import nats

                connect = nats.connect
            self.nc = await connect(self._url, **connect_kwargs())
        return self.nc

    async def publish(self, subject: str, payload: dict[str, Any]) -> None:
        """Publish *payload* as JSON on *subject* and flush."""
        nc = await self.connect()
        await nc.publish(subject, json.dumps(payload).encode())
        await nc.flush()
        logger.debug("published %s", subject)

    async def close(self) -> None:
        """Drain and close the connection (no-op when never connected)."""
        if self.nc is not None and not self.nc.is_closed:
            await self.nc.drain()
        self.nc = None
