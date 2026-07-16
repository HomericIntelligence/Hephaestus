"""An explicit, loopback-only HTTP endpoint for metrics and health."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from hephaestus.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)


def _validate_loopback_host(host: str) -> None:
    """Reject hostnames and non-loopback addresses without a DNS lookup."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("metrics server host must be a literal loopback address") from exc
    if not address.is_loopback:
        raise ValueError("metrics server host must be a loopback address")


def _validate_port(port: int) -> None:
    """Validate a TCP port before opening a socket."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("metrics server port must be an integer in 0..65535")


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """A quiet threaded server whose worker threads cannot block process exit."""

    daemon_threads = True
    allow_reuse_address = True


class MetricsHTTPServer:
    """Serve a :class:`MetricsRegistry` on an explicitly started local endpoint.

    Construction performs validation only; socket binding and the daemon thread
    begin in :meth:`start`.  The server is deliberately restricted to literal
    loopback addresses so the unauthenticated diagnostic endpoint cannot be
    exposed accidentally.
    """

    def __init__(
        self,
        registry: MetricsRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        health_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        """Store endpoint configuration without starting I/O."""
        _validate_loopback_host(host)
        _validate_port(port)
        self._registry = registry
        self._host = host
        self._port = port
        self._health_provider = health_provider
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_port = 0

    @property
    def bound_port(self) -> int:
        """The active port, or zero while the server is stopped."""
        with self._lock:
            return self._bound_port

    def start(self) -> None:
        """Bind the configured loopback port and start serving in a daemon thread."""
        with self._lock:
            if self._server is not None:
                return
            handler = self._handler_type()
            server_type: type[ThreadingHTTPServer] = _LoopbackHTTPServer
            if ipaddress.ip_address(self._host).version == 6:
                server_type = type(
                    "_IPv6LoopbackHTTPServer",
                    (_LoopbackHTTPServer,),
                    {"address_family": socket.AF_INET6},
                )
            server = server_type((self._host, self._port), handler)
            thread = threading.Thread(
                target=server.serve_forever,
                name="HephaestusMetricsHTTPServer",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            self._bound_port = int(server.server_address[1])
            thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop serving and close the listening socket; safe to call repeatedly."""
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._bound_port = 0
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("metrics HTTP server did not stop within %.1fs", timeout)

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        """Build a handler closed over this server's registry and health callback."""
        registry = self._registry
        health_provider = self._health_provider

        class Handler(BaseHTTPRequestHandler):
            """Serve only diagnostic GET routes without request logging."""

            def do_GET(self) -> None:
                if self.path == "/metrics":
                    self._write(
                        HTTPStatus.OK,
                        registry.render_prometheus().encode(),
                        "text/plain; version=0.0.4",
                    )
                    return
                if self.path == "/health":
                    try:
                        payload = (
                            {"status": "ok"} if health_provider is None else dict(health_provider())
                        )
                        body = json.dumps(payload, sort_keys=True).encode()
                    except Exception:
                        logger.exception("metrics health provider failed")
                        self._write(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            b'{"status": "error"}',
                            "application/json",
                        )
                        return
                    self._write(HTTPStatus.OK, body, "application/json")
                    return
                self._write(HTTPStatus.NOT_FOUND, b'{"status": "not_found"}', "application/json")

            def do_HEAD(self) -> None:
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

            def _write(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                """Suppress routine unauthenticated scrape request logs."""
                del format, args

        return Handler
