"""Tests for the opt-in loopback-only metrics HTTP server."""

from __future__ import annotations

import importlib
import json
import socket
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from hephaestus.observability.metrics import MetricsRegistry


def _loopback_sockets_supported() -> bool:
    """Return True when the sandbox permits binding a local loopback socket."""
    try:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
    except OSError:
        return False
    return True


_requires_loopback_socket = pytest.mark.skipif(
    not _loopback_sockets_supported(),
    reason="loopback sockets unavailable in this environment",
)


@_requires_loopback_socket
def test_server_serves_metrics_and_health_then_releases_port() -> None:
    """The server has a bounded lifecycle and serves live registry state."""
    server_module = importlib.import_module("hephaestus.observability.server")
    registry = MetricsRegistry()
    registry.gauge("hephaestus_queue_depth", "Queue depth").set(3, labels={"stage": "repo"})
    server = server_module.MetricsHTTPServer(
        registry, health_provider=lambda: {"status": "ok", "queue_depths": {"repo": 3}}
    )

    server.start()
    bound_port = server.bound_port
    try:
        with urlopen(f"http://127.0.0.1:{server.bound_port}/metrics", timeout=2) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/plain")
            assert 'hephaestus_queue_depth{stage="repo"} 3' in response.read().decode()
        with urlopen(f"http://127.0.0.1:{server.bound_port}/health", timeout=2) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"queue_depths": {"repo": 3}, "status": "ok"}
    finally:
        server.stop()

    assert server.bound_port == 0
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", bound_port))


@pytest.mark.parametrize("port", [-1, 65536])
def test_server_rejects_out_of_range_ports(port: int) -> None:
    """Invalid ports fail before any socket is opened."""
    server_module = importlib.import_module("hephaestus.observability.server")

    with pytest.raises(ValueError, match="port"):
        server_module.MetricsHTTPServer(MetricsRegistry(), port=port)


def test_server_rejects_non_loopback_host() -> None:
    """The scrape server may never bind an externally reachable address."""
    server_module = importlib.import_module("hephaestus.observability.server")

    with pytest.raises(ValueError, match="loopback"):
        server_module.MetricsHTTPServer(MetricsRegistry(), host="0.0.0.0")


@_requires_loopback_socket
def test_health_provider_failure_returns_bounded_service_unavailable() -> None:
    """A bad health provider cannot crash the server thread or leak its error."""
    server_module = importlib.import_module("hephaestus.observability.server")
    server = server_module.MetricsHTTPServer(
        MetricsRegistry(), health_provider=lambda: (_ for _ in ()).throw(RuntimeError("secret"))
    )
    server.start()
    try:
        with pytest.raises(HTTPError) as raised:
            urlopen(f"http://127.0.0.1:{server.bound_port}/health", timeout=2)
        assert raised.value.code == 503
        assert json.loads(raised.value.read()) == {"status": "error"}
    finally:
        server.stop()
