"""Threaded HTTP server exposing ``/metrics`` and ``/health`` endpoints.

Stdlib-only (``http.server``/``socketserver``), following the same
daemon-thread pattern already used by
:class:`hephaestus.nats.subscriber.NATSSubscriberThread`.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from hephaestus.observability.metrics import MetricsRegistry, render_prometheus_text

logger = logging.getLogger(__name__)

DEFAULT_SHUTDOWN_TIMEOUT: float = 5.0
"""Default timeout (seconds) for :meth:`MetricsHTTPServer.stop`."""


def _make_handler(
    registry: MetricsRegistry,
    health_provider: Callable[[], dict[str, Any]] | None,
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("%s - %s", self.address_string(), format % args)

        def do_GET(self) -> None:
            if self.path == "/metrics":
                body = render_prometheus_text(registry).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health":
                payload = health_provider() if health_provider is not None else {"status": "ok"}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    return _Handler


class MetricsHTTPServer:
    """A daemon-threaded HTTP server serving ``/metrics`` and ``/health``.

    Args:
        registry: The :class:`MetricsRegistry` to expose at ``/metrics``.
        host: Bind address. Defaults to loopback-only.
        port: Bind port. ``0`` asks the OS for an ephemeral free port —
            use :attr:`bound_port` after :meth:`start` to discover it.
        health_provider: Optional callable returning a JSON-serialisable
            health snapshot for ``/health``. Defaults to ``{"status": "ok"}``.

    """

    def __init__(
        self,
        registry: MetricsRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        health_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """Configure the server; call :meth:`start` to actually bind and serve."""
        self._registry = registry
        self._host = host
        self._port = port
        self._health_provider = health_provider
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the port and start serving on a daemon thread.

        Raises:
            RuntimeError: If the server is already started.

        """
        if self._httpd is not None:
            raise RuntimeError("MetricsHTTPServer is already started")
        handler_cls = _make_handler(self._registry, self._health_provider)
        httpd = ThreadingHTTPServer((self._host, self._port), handler_cls)
        self._httpd = httpd
        thread = threading.Thread(
            target=httpd.serve_forever,
            name="metrics-http-server",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        logger.info("metrics HTTP server listening on %s:%d", self._host, self.bound_port)

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """Stop serving and release the bound port.

        Safe to call even if :meth:`start` was never called.

        Args:
            timeout: Maximum seconds to wait for the server thread to join.

        """
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._httpd = None
        self._thread = None

    @property
    def bound_port(self) -> int:
        """The actual bound port (resolved OS-assigned port when ``port=0``).

        Raises:
            RuntimeError: If the server has not been started.

        """
        if self._httpd is None:
            raise RuntimeError("MetricsHTTPServer has not been started")
        return int(self._httpd.server_address[1])
